# Improvement line delivery (2026-10-29)

Scope ruled by the user on 2026-10-29: after the db phase-3 Jev
judgment, improve slaredis in pure code; completion is judged by
Jev from design/code evidence; **no performance numbers are
claimed by this line** — every numeric comparison against Redis
is deferred to the post-Colab-recovery re-bench. Base: M1 tip
`a191878`. Two steps, two commits:

1. `479c1b3` — cherry-pick of the application-layer perf line's
   `3f48f34` (F-perf-3/4/5). The picked tree is byte-identical
   to the source commit's tree (verified with `git diff`).
2. This commit — the event loop adopts the F-perf-1 readiness
   face (`sa_std_net_poll_wait`), replacing the 1 ms
   sleep-polling loop.

## Step 1: what was picked (recap)

- F-perf-3: `Entry.lhead` head offset; LPUSH is an amortized
  O(1) head insert; materialize/clone normalize `lhead` to 0 so
  the AOF serializer and listpack thresholds see identical
  bytes.
- F-perf-4: journal records are staged per exec pass as chunk
  Vecs in `AofState.pending` and flushed by `aof_flush_pending`
  before the pass's replies go out; fsync=always syncs once per
  pass. Records are deliberately NOT accumulated into one
  growing byte buffer (the value-threaded resp writers cost
  quadratically in the accumulated length in this stack — the
  perf line's byte-buffer version collapsed ~10x and an
  unbounded accumulator wedged at 26 GB RSS). Chunk staging +
  per-chunk writes avoid the quadratic term; this line kept
  that shape untouched.
- F-perf-5: `store_find_slot` answers live-index + first
  tombstone in one scan; inserts go through `store_insert_at`.

## Step 2: poll adoption design

The M0/M1 loop scanned every socket each round and slept 1 ms
when a round made no progress. The new loop (all in
`src/server.sla`):

- Each round opens with `poll_wait_round`: the listener plus
  every **open** connection are staged into fixed arrays of
  POLL_CHUNK=128 handles per `sa_std_net_poll_wait` call. 128
  is far under the face's 4096-handle cap, so no call is ever
  oversized and **no connection is ever excluded from a
  round's wait**, however large the table grows. The first
  chunk carries the round's timeout; later chunks are sampled
  with timeout 0 (the face is level-triggered, so anything
  that became ready during the first call is still reported).
  Results are scattered back into a new `Conn.ready` field by
  re-walking the exact sequence the staging walk produced
  (single thread; nothing mutates the table in between).
- Closed connections are never staged: their handles are
  already closed, and a closed handle would fail the whole
  wait loudly with INVALID_HANDLE (2).
- The wait budget (`poll_wait_budget`) is the time until the
  next periodic deadline — the ~100 ms expiry sweep, or, when
  the AOF everysec policy holds unsynced bytes, the
  `last_sync_ms + 1000` fsync deadline — clamped to [0, 100]
  ms. An idle round therefore wakes exactly at its next
  deadline; neither the sweep nor the everysec fsync can be
  starved by the poll.
- Work gating after the wait (watched rounds only):
  - accept drain runs iff the listener reported READ;
  - a connection read runs iff its bits carry READ or ERROR
    (a hangup reports READ|ERROR; the read then returns EOF,
    the pre-poll termination path);
  - execute runs iff the read brought bytes, or the input
    buffer still holds unprocessed bytes (`inoff < len`), or
    the round is unwatched;
  - flush is attempted whenever output is pending, as before
    (a nonblocking write to an unwritable socket is a cheap
    error return, and output produced by this round's exec was
    not in the wait set's interests);
  - the drained/reap checks, the ≥256-slot compaction, the
    sweep and `aof_tick` run every round as before.
- Connections accepted in the current round were not in its
  wait set (`polled_len` pins the covered length); they are
  read unconditionally in their accept round, exactly as the
  pre-poll loop read them.
- Error path, explicit and documented in the server.sla
  header: if any chunk's wait returns a non-OK status, the
  round is *unwatched* — the scan runs ungated (pre-poll
  behavior) and the round ends with the pre-poll 1 ms
  no-progress sleep. There is no silent fallback: the poll
  path is the only idle wait on success, and the error path
  exists so a broken readiness face degrades latency, never
  correctness or liveness. (On this platform the wait cannot
  fail by construction — handles are ours and sets are
  capped — so the path is a guard, not an expectation.)

### Semantics mapping (pre-poll → poll)

| Aspect | Before | After |
|---|---|---|
| Idle wait | scan all + `THREAD_SLEEP_MS(1)` | block in `poll_wait` until readiness or next timer deadline |
| Accept | attempted every round | gated on listener READ (watched rounds) |
| Read | attempted every round per conn | gated on READ\|ERROR bits (watched rounds) |
| Execute | every round per conn | on new bytes, on buffered leftover, or unwatched rounds |
| Flush | every round when pending | unchanged |
| Expiry sweep | checked every round (~100 ms) | unchanged cadence; its deadline bounds the wait |
| AOF everysec | `aof_tick` every round | unchanged; its deadline bounds the wait when unsynced > 0 |
| AOF always | sync per exec pass (F-perf-4) | unchanged (flush happens inside exec, before replies) |
| Close/reap | drained && (eof \|\| close_after_flush) | unchanged |
| New conn first read | in accept round | in accept round (beyond `polled_len`) |

Toolchain-shape notes: the staging arrays live in the helper
with their pointers hoisted into lets once — several inline
`PTR_BYTE_ADD(&field, 0)` expressions in one call are a known
consumed-register trap in this toolchain (F-perf-1 probe
record, same family as slanginx D5). All staged values are
primitive copies; no owned value is moved conditionally
anywhere in the new code (M1-1 minefield not entered). No
`x = f(x, …)` accumulation idiom (the quadratic-append shape)
was introduced; journal/reply buffers keep their direct-push
forms.

## Local verification (this machine, 2026-10-29)

### Environment findings (read before reusing)

- This VM instance has **no `libLLVM-14.so.1` and no
  `libz3.so.4`** system-wide (only LLVM 20), so the stock
  `sa 0.1.8` could not start. Both libraries were fetched as
  Debian bookworm .debs (libllvm14 14.0.6-12, libz3-4
  4.8.10-1) and extracted — not installed — under
  `~/workspace/local-tools/llvm14/`; `sa` runs with
  `LD_LIBRARY_PATH` pointing there. No system state changed.
- The local std snapshot used for both builds is the sci
  integration tree (poll commit `e88c0d04` + the F-perf-2
  `sa_mem_free` pair), snapshotted at
  `~/workspace/sci-pollbuild-local/`; its `sa_std/` also
  carries `libsa_std.a` at its root because `sa`'s
  `saStdArchivePath` prefers `$SA_STD_DIR/libsa_std.a`.
  Before this snapshot, the locally installed archive had
  **zero** `sa_std_net_poll_wait` symbols (checked with nm on
  both `artifacts/sa_std/libsa_std.a` and `zig-out/lib/` of
  the previously installed tree). With the snapshot archive,
  the poll build links the symbol from the archive — the
  final binary is stripped, so the evidence is the successful
  link of code that calls the extern plus the runtime
  behavior below.
- The sa-driven `zig cc` link omits ICU; the std archive
  needs ICU 74 (present system-wide). A PATH shim
  (`~/workspace/slaredis-build-shim/zig`) appends
  `-licuuc -licudata -licui18n` to `zig cc` calls only. The
  full recipe is `~/workspace/slaredis-build-shim/build.sh`.
- Dead ends recorded so they are not retried: the
  `-Dllvm=false` sa build compiles SLA → SA but its
  build-exe/test paths demand the disabled LLVM-C backend;
  `sa build-c` output traps on the `sa_std_net_tcp_listen`
  extern (the C backend's arena pointer model has no host
  bridge for it); the SAB interpreter (`sa run`) has no FFI
  bridge for `sa_std_close`. The native LLVM path above is
  the only local route and is the README's own build command.
- All builds ran with `.sla-cache` cleared and
  `SLA_NO_FRESHNESS_CACHE=1` (M1-2).

### Gates

Step-1 tree (`479c1b3`), full acceptance
(`tests/run_acceptance.sh`), locally rebuilt and re-run:

- M0 resp_client: **137/137, failures 0**
- listpack_client: **49/49**
- auth_client: **13/13** with password, **3/3** without
- AOF suite: **all phases pass** — restart replay byte-for-byte
  equal to the pre-kill dump, expired key absent, absolute
  expiry preserved, post-rewrite appends replayed, truncated
  tail tolerated, corrupt mid-file exits 69, fsync=no data
  survives SIGKILL+restart.

Poll tree (this commit), same harness: **identical results**,
and the sorted ok-name set is byte-identical to the step-1
run (209 names, `diff` empty — saved as
`~/workspace/slaredis-build-shim/names_perf_baseline.txt` and
`names_poll.txt`).

Poll-specific functional evidence (`tests/poll_stress.py`,
13/13 pass):

- 200 concurrent connections (wait set spans two chunks):
  PING answered on conns 0, 1, 126, 127, 128, 150, 199 —
  both sides of the 128-handle chunk boundary; SET via conn
  150 read back via conn 3.
- A `PX 300` key expires while the loop idles in poll (the
  sweep deadline wakes the wait; no traffic needed).
- After 190 connections close en masse, a fresh connection
  is served and the data is intact (reap-compaction era).
- Kernel-level: an idle poll server sits in
  `poll_schedule_timeout` (`/proc/<pid>/wchan`) with ~119
  voluntary context switches over ~30 s idle (a 1 ms
  sleep-loop would show ~1000 switches/s) — the loop is
  genuinely blocked in poll(2), not on the error path.
- AOF everysec (the default mode) end-to-end under poll:
  SET + LPUSH, 2 s idle, SIGKILL, restart — both values
  replayed from the AOF.

### Not verified / deferred (stated plainly)

- **All performance numbers**: none measured, none claimed.
  The LPUSH/AOF plateau attribution from M1 stands as the
  hypothesis; the poll adoption's effect (idle cadence,
  p99 quantum) is exactly what the post-recovery re-bench
  must measure with `scripts/bench/` on the 8-core box.
- fsync *timing* under everysec was not syscall-traced
  (ptrace attach is denied on this instance); what is proven
  is the end-to-end everysec flow above and that the wait
  budget contains the fsync deadline by construction.
- >4096 simultaneous connections: correct by construction
  (chunked waits cover every open connection every round)
  but not load-tested locally; the stress run covered 200.
- The build route (extracted .deb libraries, ICU shim,
  std snapshot) is this-instance scaffolding, not a repo
  artifact; on the box the stock toolchain builds the same
  sources with the README command.
