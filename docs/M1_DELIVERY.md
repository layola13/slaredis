# slaredis M1 — delivery record

Date: 2026-10-28. Built on the M0 tree (local master
`d853c6c` = pushed M0 `195971b`; trees verified byte-identical
by content sync + clean `git status` on the box). Toolchain:
`sa` from the sci DCE-verification build (SCI tip `28fd2ceb`
lineage), SLA plugin as installed on the box, local commits
only — nothing pushed by this line.

## Verdict per deliverable

| # | Deliverable | Verdict |
| - | ----------- | ------- |
| 1 | listpack compact encoding (hash/set/zset), threshold conversion, round-trip proof | Done — 49/49 protocol checks |
| 2 | AUTH (config password, AUTH command, NOAUTH gate, Redis error forms) | Done — 16/16 checks across both server modes |
| 3 | Persistence common parts: journal hook + full-dataset serializer with round-trip proof | Done — the AOF restart-replay dump comparison is the round-trip proof |
| 4 | Persistence model A (AOF only, per ruling): append + fsync policy, replay, rewrite | Done — 7/7 AOF phase assertions |
| 5 | `docs/PERSISTENCE.md` final decision record | Done |
| 6 | M0 regression: 137/137 with an identical failure-name set (empty) | Done — see Gates |
| 7 | Native-Redis performance gate T50 | **NOT PASSED — 0/10 gate scenarios** (both slaredis columns); milestone not accepted, see the bench section. Per the user's 2026-10-28 20:04 clarification this blocks milestone closure, not delivery of the work |

## 1. listpack

`src/listpack.sla` implements the real Redis listpack byte
format: 6-byte header (total bytes u32 LE, count u16 LE),
entries in the nine Redis encodings (7/13/16/24/32/64-bit
integers, 6/12/32-bit strings), backlen trailer per entry, 0xFF
end marker. Elements that parse as strict Redis integers
(string2l rules) take the smallest integer encoding; everything
else is a string — so `"007"` round-trips as a string while
`"42"` round-trips through the integer path, exactly Redis's
contract.

Store integration: hash/set/zset entries carry `enc`
(plain/packed) + `pack`; new aggregate keys start packed.
Conversion is one-way, at Redis's thresholds: a write converts
the key first when it would insert an element longer than 64
bytes or push the logical element count past 128. Mutations of
packed entries are value-threaded rebuilds through
append/insert/replace/delete splices (the referee forbids the
borrowed-Vec mutation shape; see finding M1-1). `OBJECT
ENCODING` reports `listpack` / `hashtable` / `skiplist` /
`raw` / `linkedlist`, so representation changes are observable
at the protocol level, never silent. Only the ENCODING
subcommand exists; other OBJECT subcommands are an explicit
error.

Round-trip evidence (`tests/listpack_client.py`, 49 checks,
all against the live server):

- Every integer encoding width exercised in one hash (0, 127,
  128, -1, ±4096, 4095/4096, ±32768, ±8388608, 2^31 edges,
  i64 min/max) plus the string traps (`007`, `-0`, `3.14`,
  63-byte and 64-byte strings) — HGETALL compared byte-exact.
- Replace (shorter and longer) and delete splices inside packs
  read back exact.
- Conversions: hash/set/zset each at the 129th element
  (`listpack` at 128, plain at 129, full contents compared
  after), and at a 65-byte field/value/member; conversion is
  one-way (shrinking a converted key keeps it plain).
- Zset ordering, WITHSCORES text, score updates and removals
  on packs; `inf` and 1e20 (std-formatter path) scores
  round-trip.

The stress hash caught two real defects, both fixed in-line:

- **32-bit integer decode lacked sign extension** (assembled
  u64 cast straight to i64): -2147483648 read back as
  2147483648. Fixed in `lp_decode`.
- **`append_i64` printed a bare `-` for i64::MIN** (negation
  overflows to itself) — a latent M0 bug in `bytes.sla` that
  also affected RESP integer replies. Digits are now extracted
  from the negative value directly.

## 2. AUTH

- Config: `SLAREDIS_PASSWORD` (env-only, like all slaredis
  config). Set non-empty → authentication required.
- Gate (server dispatch): an unauthenticated connection may
  only attempt AUTH; every other command is answered
  `-NOAUTH Authentication required.` without executing.
  Gate state is per-connection (verified: a second connection
  is still gated after the first authenticates).
- AUTH forms: `AUTH pass` and `AUTH default pass` succeed with
  the configured password (`+OK` byte-exact); wrong password,
  unknown username, and default-user-with-wrong-password all
  return Redis's `WRONGPASS invalid username-password pair or
  user is disabled.`; AUTH on a passwordless server returns
  Redis's "called without any password configured for the
  default user" text (both the `<password>` and
  `<username> <password>` spellings). Arity errors use the
  standard form.
- Evidence: `tests/auth_client.py` — 13 checks against the
  password server, 3 against the passwordless server.

## 3+4. Persistence (model A — AOF only)

Decision record: `docs/PERSISTENCE.md` (three models, JEV
probabilities A 0.98 / B 0.02 / C 0.0, user approval
2026-10-28 19:17). Implementation:

- **Journal hook** (`src/journal.sla`): after a write command
  executes with a non-error reply, its RESP record is appended
  before the reply flushes. Relative expiries are absolutized
  (SET EX/PX → SET + PEXPIREAT; EXPIRE → PEXPIREAT) — replay
  reproduces deadlines, it does not re-extend them.
  `PEXPIREAT` was added as a command to be the canonical form.
- **File + fsync** (`src/aof.sla`): append handle opened
  write|create|append; `SLAREDIS_AOF_FSYNC` =
  always/everysec/no, default everysec with the rationale
  written in PERSISTENCE.md (Redis's default under the same
  trade-off; per-command disk latency never blocks the loop by
  default; the 1-second window is explicit).
- **Replay**: before listening; multibulk-strict (the wire
  parser's inline form is not accepted in the file). Truncated
  final record (crash tail) tolerated; corruption anywhere
  else refuses startup with exit 69. Invalid persistence
  config exits 68.
- **Rewrite**: `BGREWRITEAOF` + automatic trigger (file grew
  ≥ 1 MiB since last rewrite AND ≥ 2×). The full-dataset
  serializer renders live keys as canonical commands
  (expiries as PEXPIREAT) to `<path>.rewrite.tmp`, fsyncs,
  atomically renames over the live file, reopens appends.
  Synchronous — a stated deviation from Redis's forked
  rewrite; the reply says "AOF rewrite completed", not
  "background started".
- **Failure posture**: an append/fsync failure marks the AOF
  broken; subsequent writes are refused with an explicit
  error, reads continue. (Redis logs and continues; this
  server has no log channel — M0 finding 8 — so refusal is
  the visible option.)
- **Model B reservation**: `snapshot_save` in `aof.sla` is a
  declared, unwired stub returning
  `SNAPSHOT_NOT_IMPLEMENTED`. Model C (fork in sa_std) is not
  pursued, per the ruling.

Evidence (`tests/run_aof_test.sh`, all phases green):

- Write a mixed dataset (all five types, packed and converted
  aggregates, EXPIRE/PERSIST, an INCR counter, 200 overwrites
  of one key) under fsync=always → `BGREWRITEAOF` (file
  shrinks: 200 churn records collapse to one) → more writes →
  kill → restart → canonical per-key dump **byte-identical**
  to the pre-kill dump.
- Expired key absent after replay; absolute expiry (PEXPIREAT)
  preserved; post-rewrite appends replayed.
- Truncated tail appended by hand → restart serves the intact
  dataset.
- Corrupt mid-file record → startup exits 69.
- fsync=no: write → SIGKILL → restart → key present.

## Gates

- Baseline (M0 tree, before any M1 edit): build clean,
  `resp_client.py` 137/137.
- Final: `tests/run_acceptance.sh` — M0 suite 137/137 with
  the **same passing-name set as baseline** (set-compared, not
  just counted), listpack 49/49, AUTH 3/3 + 13/13, AOF suite
  all phases. The M0 client is unmodified.
- Full rebuild from a cleared `.sla-cache` before the final
  run (the cache masks source changes — it bit this line
  twice; see finding M1-1 notes).

## Bench (gate T50 vs native Redis) — NOT PASSED

Method (`scripts/bench/run_bench.sh`, parameters fixed in the
script): same box, native **Redis 7.0.15** (apt, Ubuntu 24.04)
on :16379, slaredis on :16380, slaredis+AOF (fsync=everysec,
the shipped default) on :16381; `redis-benchmark -c 50
-n 100000 -d 3`, empty dataset per invocation, 1 warmup + 3
measured rounds, medians. Gate T50 (JEV, p=0.60): per scenario
throughput ≥ 50% of Redis AND p99 ≤ 2× Redis. Full table:
`scripts/bench/results/20261028-122900/summary.md` (two earlier
runs agree within noise; verdicts identical).

| scenario | redis ops/s | sla ops/s | ratio | sla p99 ratio | sla+AOF ratio | verdict |
| -------- | ----------- | --------- | ----- | ------------- | ------------- | ------- |
| set P=1 | 94,607 | 18,556 | 0.196 | 6.1× | 0.094 | FAIL |
| get P=1 | 97,276 | 16,675 | 0.171 | 8.7× | 0.188 | FAIL |
| incr P=1 | 97,466 | 16,319 | 0.167 | 16.5× | 0.108 | FAIL |
| hset P=1 | 95,785 | 12,069 | 0.126 | 8.8× | 0.069 | FAIL |
| lpush P=1 (-n 10000) | 94,340 | 402 | 0.004 | 588.9× | 0.004 | FAIL |
| sadd P=1 | 94,429 | 15,088 | 0.160 | 10.3× | 0.093 | FAIL |
| zadd P=1 | 98,814 | 13,841 | 0.140 | 9.1× | 0.069 | FAIL |
| set P=16 | 961,538 | 54,348 | 0.057 | 14.9× | 0.016 | FAIL |
| get P=16 | 1,030,928 | 83,264 | 0.081 | 11.3× | 0.083 | FAIL |
| incr P=16 | 1,176,471 | 47,037 | 0.040 | 20.0× | 0.015 | FAIL |
| set -r 10000 (diagnostic) | 71,891 | 3,873 | 0.054 | 24.7× | — | FAIL |
| get -r 10000 (diagnostic) | 83,963 | 16,728 | 0.199 | 6.6× | — | FAIL |

**Every gate scenario fails on both criteria, in both the
plain and AOF columns. The M1 performance gate is not passed
and the milestone is not accepted.** The functional
deliverables above stand on their own evidence; per the
ruling, the gap goes back to the SA/SLA layer with the
attribution below — no application-layer workaround was
applied to these numbers, and none of the figures is softened.

### Attribution (measured, layered)

Probes: `scripts/bench/probe_attribution.sh`,
`scripts/bench/probe_per_command.sh`; outputs under
`scripts/bench/results/attribution-*` and `per-command-*`.

- **F-perf-1 — sa_std has no readiness (poll/epoll) API over
  TCP; the sleep-poll loop pays for it twice.** (a) Idle-wait:
  when a round finds no progress the loop sleeps 1 ms, and
  that quantum lands directly in latency: single-client GET
  p50 is 0.095 ms but **p99 is 1.239 ms** (Redis: 0.031 /
  0.063). A single pipelined client is capped at ~16 commands
  per sleep quantum: GET -c 1 -P 16 = **15,848/s** vs -c 50
  -P 16 = 83,264/s vs Redis -c 1 -P 16 = 448,430/s. (b) Scan:
  with no readiness signal the loop services every connection
  every round, so a synchronous client's RTT ≈ one full round
  (≈2.7 ms at 50 connections). Redis's epoll loop pays only
  per ready connection and blocks otherwise. This is the M0
  event-loop finding returning with numbers attached.
- **F-perf-2 — per-command userspace cost ≈ 10–12 µs vs
  Redis's ≈ 1 µs (SLA codegen / sa_std container economics).**
  strace census during a -c 50 GET burst: 1 read + 1 write per
  command, syscalls ≈ 4% of wall time (0.40 s of the ~10 s
  window); the other ~96% is userspace — RESP parsing clones
  every argument, dispatch works on further clones, replies
  are built by repeated Vec appends, connection buffers are
  value-threaded with full clones, and the allocator churns
  (brk ≈ 1 call per 6–12 commands). The GET -P 16 ceiling
  (83k/s) is reached with a one-key keyspace, so this is pure
  parse/exec/reply cost, not store cost. The referee's
  ownership shapes push the app code into these clone-heavy
  forms; there is no borrowed-slice fast path to take instead.
- **F-perf-3 — LPUSH is O(n) deep-clone per push
  (application representation).** Measured single-client
  curve: 1k pushes 0.61 s, 10k pushes 20.4 s (quadratic;
  ~2000 s projected at the standard -n 100000, hence the
  documented -n 10000 for this scenario, applied to both
  servers). Bench: 402/s vs Redis 94,340/s. Fix direction for
  a future app line: a chunked or reverse-storage list. Not
  an SA/SLA gap.
- **F-perf-4 — AOF journaling adds ≈ 50 µs per write command
  (application journal construction, on F-perf-2's
  constants).** SET: 18,556/s plain → 8,883/s with AOF at
  P=1; 54,348 → 15,291 at P=16. strace on the AOF server:
  exactly **1 writev per record**, fdatasync at the everysec
  cadence (8 calls in the window, 42 µs each) — neither
  syscall count nor fsync explains the cost; building the
  record (per-argument clones + RESP serialization in
  generated code) does. GET with AOF ≈ GET without (0.188 vs
  0.171) confirms the AOF instance itself is not slower.
- **F-perf-5 — the SET path costs ~4× the GET path on a
  10k-key keyspace (application store shape).** Diagnostic
  -r 10000: SET 3,873/s (258 µs/op) vs GET 16,728/s, while
  GET -r matches single-key GET (0.199 vs 0.171) — the read
  scan with its FNV prefilter is exonerated at this scale;
  the clone-heavy insert/replace path through the fat-Entry
  Vec is not. The keyspace hash table remains a known M0
  follow-up, but it is not the headline gap at ≤10k keys.

Net shape of the gap: roughly ×5–6 from F-perf-1+F-perf-2 on
simple commands (they multiply: round cadence × per-command
cost), ×250 on LPUSH (F-perf-3), an additional ×2–3.5 on
writes with AOF on (F-perf-4). Closing it belongs to the
SA/SLA improvement line (readiness API first — it is the only
item that also removes the p99 quantum), with this bench as
its acceptance harness.

## New toolchain findings (registered, not fixed here)

- **M1-1: conditional move of a loop-local value is a referee
  minefield.** A Vec materialized inside a loop and moved into
  an aggregate only on a parse-gated branch (its sibling moved
  on one branch, borrowed on the other) fails three ways in
  sequence: PhiStateConflict (1015) at the loop merge; `!name`
  release on the non-consuming path → UseAfterMove (1009);
  sink-binding instead → the compiler emits the release at a
  merge where the register is undeclared (UnknownRegister
  1007). Workaround (now the house shape): materialize only on
  the consuming path, carrying the position across the gate.
  Minimal repro: `docs/repros/repro_cond_move_loop.sla`
  (verified 2026-10-28: compiling it traps with
  PhiStateConflict on register `name` in `@sla__collect`).
  Relatedly, moving an env-value field inside a nested branch
  in `main` hit the same PhiStateConflict family (fixed by
  return-path moves in helper functions), and a struct literal
  nested in a tuple literal leaves its construction register
  live (MemoryLeak 1012; fixed by named-local binding). Both
  noted in code comments at the sites.
- **M1-2: `.sla-cache` masks source edits across builds** in
  this workflow: after editing a source file, the expanded
  output can still contain the old text (observed verbatim
  with a one-line edit) until `.sla-cache` is removed. The
  AGENTS.md rule (clear it before verifying compiler changes)
  apparently applies to app builds too when the frontend cache
  is warm. Every build in this line's final phase ran with the
  cache cleared + `SLA_NO_FRESHNESS_CACHE=1`.

## Deliberate deviations recorded

- Rewrite is synchronous (see PERSISTENCE.md).
- The dataset serializer assembles its output in memory;
  streaming is future work.
- OBJECT implements only ENCODING.
- BGREWRITEAOF without AOF enabled is an explicit error
  (Redis schedules one anyway in some versions).
