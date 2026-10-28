# slaredis M0 — delivery record

Date: 2026-10-28. Built by the BN line for main-session gate
review; not pushed anywhere by this line. Local master commit
and box commit are recorded in the line's final report (this
document intentionally does not self-reference a hash).

Toolchain measured on the build host (Colab box):
`sa 0.1.8`, sci HEAD `14d9fbc8`, sa_plugin_sla HEAD `59cbcfa8`.
The box has **no redis-cli** (`which redis-cli` is empty), so
acceptance ran entirely on the self-written Python client, as
required.

## Verdict per deliverable

| # | Deliverable | Verdict |
| - | ----------- | ------- |
| 1 | Repo scaffold, README, `src/` SLA sources named by domain | Done |
| 2 | RESP2 server on sa_std TCP: inline + multibulk parsing, sequential pipelining, atomic commands | Done — event-loop model, declared below |
| 3 | Command set (31 commands: the 30 required + `PTTL`) with Redis-form errors | Done |
| 4 | Expiry: lazy on access + periodic active sweep, TTL seconds semantics | Done |
| 5 | Real acceptance on the box with a self-written raw-socket client | Done — **137 checks, 0 failures** (`docs/acceptance_log.txt`) |

## Concurrency model — declaration and evidence

**Built model: single-process, single-threaded round-robin
event loop over nonblocking sockets** (Redis's own shape).
Commands are atomic by construction; the expiry sweeper runs
inside the loop on a ~100 ms cadence; an idle round sleeps 1 ms.

Evidence the choice was made on (files in `probes/`):

- `probes/probe_core.sla`: `Arc::new(Mutex::new(store))` fails
  type-check with `error.TypeMismatch`, even with an explicit
  type annotation — the SLA `Mutex<T>` constructor only accepts
  scalar payloads (the CI-proven shape is `Mutex<i32>`). A
  thread-per-connection design therefore cannot hold the store
  behind a Mutex today.
- No poll/epoll surface over TCP exists in sa_std (only fs
  watch / http-client / quic polls), so the loop polls by
  nonblocking accept/read/write rounds.
- `probes/probe_loop.sla` + `probes/probe2_client.py`: a full
  pass (`A C N1=6 H0=0 R0=6 N0=0 T D`) — interleaved fragments
  from two connections, per-line replies, EOF close, and the
  crucial status semantics: empty nonblocking accept/read
  return status 6 (`SA_STD_ERR_IO`) with the handle untouched,
  while EOF is `SA_STD_OK` with `out_read == 0`, so would-block
  and EOF are distinguishable.

## Acceptance evidence (measured, this build)

Full log: `docs/acceptance_log.txt` — 137 checks, ALL PASS,
against the server running on 127.0.0.1:16399. Highlights:

- Byte-exact replies where the wire form matters: `+PONG\r\n`
  for PING, `$1\r\n1\r\n` for GET, `$-1\r\n` for a missing key,
  `-WRONGTYPE ...` prefix + CRLF for type errors.
- Error forms: arity (`ERR wrong number of arguments for
  'get' command`), unknown command, not-an-integer, INCR
  overflow, invalid expire time, syntax errors, `ZADD` bad
  score with **no partial application** verified afterwards.
- Expiry timing: `SET EX 1` alive immediately, gone after
  1.4 s (lazy path); `SET PX 400` gone after 0.7 s; a key
  expiring while other traffic flows (PING loop for 0.9 s,
  >10 round trips) is collected with the loop healthy; TTL /
  PTTL ranges, `PERSIST` transitions, `SET` clearing a TTL,
  `INCR` preserving a TTL.
- Pipelining: 5 commands in one write produce the 5 ordered
  replies `["OK", 2, b"2", "PONG", "string"]`; a multibulk
  command split across two TCP segments completes correctly;
  inline commands (including padded whitespace) work.
- Protocol error: `$-5` yields `-ERR Protocol error` and the
  server then closes the connection (client observed EOF).
- Concurrency: 8 worker clients × 200 INCR on private counters
  + 50 SET/GET rounds each, and 4 clients × 250 INCR on one
  shared counter, all in parallel: no errors, shared counter
  ends at exactly **1000**, every private counter at **200**.
- WRONGTYPE matrix: 16 cross-type command/key combinations,
  all returning the Redis WRONGTYPE text.
- Default configuration: with no environment set the server
  binds 127.0.0.1:6379 and answers PING; an invalid port exits
  64; an over-long host exits 65.

## Command semantics notes (deliberate M0 choices)

- `SET` clears any old TTL unless `EX`/`PX` is given (Redis
  semantics; KEEPTTL is not in the M0 set). Duplicate or
  conflicting `EX`+`PX` is a syntax error; non-positive or
  non-integer expire values return `ERR invalid expire time
  in 'set' command`. Deadlines use saturating arithmetic.
- `INCR`/`DECR` use Redis's strict integer rules (no leading
  zeros, `-0` rejected, range-checked); overflow returns the
  Redis overflow error; an existing TTL is preserved.
- `EXPIRE` with seconds <= 0 deletes immediately and returns
  1. `TTL` rounds with the +500 ms Redis convention.
- Aggregates delete their key when they become empty
  (hash/list/set/zset), matching Redis.
- `LPOP`/`RPOP` count form: negative count returns `ERR value
  is out of range, must be positive`; count 0 returns an empty
  array (Redis >= 6.2 behavior).
- `ZADD` validates **all** scores before applying any pair.
  `inf`/`+inf`/`-inf` scores are accepted; `nan` is rejected
  with `ERR value is not a valid float`.
- `ZRANGE` ordering is (score asc, member bytes asc).
- Errors not in the required set (protocol errors) close the
  connection after the reply, as Redis does.

## Registered toolchain findings (repros in `docs/repros/`)

This line did not edit sci or sa_plugin_sla. Each finding has
a minimal repro and the app-layer workaround used.

1. **`Mutex<T>` constructor is scalar-only.**
   `Arc::new(Mutex::new(store))` → `error.TypeMismatch` even
   annotated. Repro: `probes/probe_core.sla`. Impact: decided
   the concurrency model (event loop). Workaround: none needed
   under that model.
2. **Pushing into a bare `&Vec<u8>` parameter inside a loop →
   `PhiStateConflict`** (register `out`: expected Active,
   actual Composite). Repro: `probes/probe_shapes.sla` (case
   S1; S3–S6 and the field-path shapes pass). Workaround:
   all buffer plumbing is value-threaded (`fn(out: Vec<u8>,
   ...) -> Vec<u8>`), the sla_ecs idiom.
3. **`let mut` is rejected by the parser**
   (`error.UnexpectedToken` at `mut`). SLA variables are
   mutable by default; sa_std's one `let mut` occurrence
   (async.sla) apparently is not compiled by this path.
   Repro: `docs/repros/repro_let_mut.sla`. Workaround: plain
   `let` everywhere.
4. **Unsuffixed integer literal > i64::MAX →
   `error.InvalidCharacter`**, misreported through @import as
   "Import Error: failed to expand @import SLA sources".
   Repro: `docs/repros/repro_big_literal.sla`. Workaround:
   `u64` suffixes (FNV offset basis, i64-limit expressions).
5. **Bare `Vec<f64>` element reads return 0.0** (push 2.5,
   read back → 0). `Vec<i64>` is fine; an f64 **struct field**
   inside a Vec element reads back correctly. Repro:
   `docs/repros/repro_vec_f64.sla`. Workaround: zset scores
   live in a `ZRow { member, score }` struct; no `Vec<f64>`
   anywhere in the codebase; `ZADD` re-parses scores in its
   apply pass instead of staging them in a `Vec<f64>`.
6. **A Vec reassigned from a value-threaded call
   (`tmp = f(tmp, ...)`) loses its elements when later passed
   by reference with its length into another call** — the
   receiving loop observes length 0. Controls: a push-built
   Vec copies fine; a fresh-bound call result copies fine;
   reading `len(tmp)` directly is correct. Repro:
   `docs/repros/repro_staging_copy.sla`. This silently
   emptied `fmt_f64`'s integer part during development
   (ZSCORE returned `""` / `".5"`). Workaround: `fmt_f64`
   appends directly onto its out buffer; the codebase was
   audited and contains no remaining instance of the shape.
7. **`&&` / `||` do not short-circuit — a bounds check cannot
   guard an index across them.** `p < n && a[p] == 46` with
   `p == n` traps at runtime (`PANIC: code=32`, exit 160).
   Registered as a language-semantics gotcha rather than a
   defect claim, but it is a hard trap for Redis-style guard
   code. Repro: `docs/repros/repro_and_guard_trap.sla`.
   Workaround: nested ifs at every guarded-index site
   (audited across `src/`).
8. **`sa_print_bytes` traps `CapabilityMismatch` against a
   stack-array pointer** (observed in `probes/probe_loop.sla`
   development). The M0 server consequently writes nothing to
   stdout; failures are reported via exit codes.
9. **Would-block shares status 6 (`SA_STD_ERR_IO`) with
   generic IO errors** on the net surface. The event loop
   treats any non-OK read/accept as "no data this round";
   EOF is separately detectable (OK + 0 bytes). Noted so a
   future poll surface can tighten this.

Investigated and **not** registered: a tuple return type
containing a generic type (`-> (bool, Vec<u8>)`) was suspected
of a parse failure during development; the isolated repro
compiles and runs correctly, so no defect is claimed.
(`main.sla` nevertheless returns a small struct from that
helper, which reads better anyway.)

## Notes for the main session before pushing

- The box scratch trees `/content/slaredis_fresh/` (parser /
  runtime bisection roots) are **not** part of the repo; the
  repros that matter were distilled into `docs/repros/`.
- Build byproducts (`src/main`, `src/main.sa`, `src/main.sa.bc`,
  logs, `.sla-cache/`) are covered by `.gitignore` and are not
  in either commit.
- The acceptance log in `docs/acceptance_log.txt` is from the
  final committed sources (comment-only differences excluded
  by re-running after the last code change — see the line's
  final report for the exact final run).
- Suggested follow-ups beyond M0: hash-table keyspace,
  compact encodings (listpack first), M1 persistence design
  (blocked on the absent fork primitive), AUTH, and upstream
  fixes for findings 2/5/6, after which the workarounds can
  be relaxed.
