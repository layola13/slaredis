# Goroutine model — delivery record

Stage-2 slaredis line (PLAN-GOROUTINE charter, including the
2026-10-30 12:48 annex registering face 4 as this line's
mandatory closure). Base: slaredis main
`957eaa0311aa1e833c872f4c559945c7ce3d754b`. Toolchain for all
measurements below: sci master `e7c18568`, sa_plugin_sla
`47ec56a`, box (Colab) host, `SA_PLUGIN_DEV=1`,
`SA_STD_DIR=<sci>/sa_std`, `SLA_NO_FRESHNESS_CACHE=1`,
`.sla-cache` cleared before every verification build.

## 0. Pre-flight drift scan (PLAN §0) — zero drift

INTEGRATION_PREP §② re-run against the new master before any
code: import surface 12/12 files byte-identical between the
poll-build tree (sci `220dabe7`) and sci master `e7c18568`
(core/slice.sa, env.sai, fmt.sai, fs.sai, net.sai, net.sla,
ptr.sa, status.sla, string.sa, thread.sa, time.sai, vec.sa);
25/25 extern declarations present in the new `.sai` files;
implementations in `src/runtime/sa_std.zig` byte-identical
(sha256) for all 25 plus helpers `ensureSocketHandle` /
`lenAsUsize`. The 25: net — poll_wait, tcp_accept,
tcp_listen, tcp_listener_set_nonblocking,
tcp_stream_read, tcp_stream_set_nonblocking,
tcp_stream_write; fs — sa_fs_file_close, sa_fs_file_sync_data,
sa_fs_rename, sa_std_fs_exists,
sa_std_fs_file_read_exact_at, sa_std_fs_file_write,
sa_std_fs_len, sa_std_fs_open_options, sa_std_fs_open_read,
sa_std_fs_open_write, sa_std_fs_remove; env — sa_env_get,
sa_env_buffer_data/len/free; fmt — sa_fmt_f64_into; time —
sa_time_unix_ms; sa_std_close. The sci gate for the new
tasking surface was also re-run: `zig build sa-std-static` +
nm shows all 15 `sa_task_*` symbols in
`artifacts/sa_std/libsa_std.a`.

## 1. Architecture as built (`src/gserver.sla`)

Charter fixed points, all preserved: the store is owned by a
**single executor goroutine** (Jev `executor_goroutine`
0.97); connection goroutines hand command batches to it as
envelopes over `cmd_chan` and wait on a per-request done
channel; expiry sweeping and AOF writing are dedicated
goroutines; Redis serial semantics hold by construction
(single FIFO executor ⇒ atomic commands in a definable
global order — the executor's receive order).

- **Executor goroutine** owns `Store` as an owned value field
  of its heap state block. Per cycle (each cycle is a called
  step function, see §3 rule C): take one request — sweep
  (store_sweep), serialize (dataset_serialize for the
  writer's auto-rewrite trigger), or a command batch
  (mirrors the poll loop's conn_exec_all minus AUTH, with
  journal staging and the BGREWRITEAOF path inline). Manual
  `sa_task_yield()` every `SLAREDIS_YIELD_EVERY` commands.
- **Connection goroutines** own all per-connection state in a
  heap job block (their own AUTH config copy, inbuf/outbuf,
  one reused CmdReq — at most one batch outstanding). They
  parse RESP locally, handle the AUTH gate and AUTH itself
  locally in order (a pending batch is flushed first, so the
  byte order matches the poll loop's single out buffer),
  deep-clone store-bound commands into their batch, and emit
  replies to the driver over `io_out`. End-of-stream is a
  marker message: the goroutine runs a final exec pass over
  buffered complete commands (poll-identical), emits, asks
  for close-after-flush, and exits. A protocol error flushes
  prior replies, appends the error, and closes the same way.
- **AOF writer goroutine** owns `AofState` as an owned value
  field. Append ops (staged record chunks from the executor)
  are written through the same `aof_flush_pending` the poll
  loop used; install ops swap in executor-serialized
  datasets through `aof_swap_in` (both functions extracted
  from aof.sla with poll behavior regression-verified
  identical). fsync=always appends wait for the writer's
  recycle acknowledgement before the batch is signalled, so
  a reply never precedes its record's sync. A 100 ms select
  tick performs the everysec sync and the auto-rewrite size
  trigger (serialize is requested from the executor, the
  store owner). A sticky `broken` state mirrors to the
  executor, which then refuses writes exactly as the poll
  model does.
- **Sweeper goroutine**: every 100 ms posts a sweep request
  to the executor (which owns the store — sweeps included).
- **Supervisor goroutine** monitors the executor, the
  writer, the sweeper, and every connection goroutine
  (fixed slot-array table, round-robin watch arm, one merged
  select per cycle — see §3 rule C). Connection exit ≠ 0:
  counted, and the driver is asked to close that stream.
  Sweeper exit: respawned. Executor or writer exit:
  `ctx.core_dead` is raised and the process exits **71** —
  a loud failure recovered by AOF replay, never a silent
  solo restart over a half-owned dataset.
- **IO driver — deviation from the plan's letter, flagged.**
  The plan's shape has each connection goroutine doing its
  own blocking socket IO. That form is impossible on the
  measured runtime (§3 rules A/B): blocking net waits in
  spawned goroutines starve each other, and the readiness
  wait called from a spawned goroutine starves channel
  delivery process-wide. So all socket IO is concentrated in
  a single driver — accepts, reads, writes for every
  connection — and the driver runs on the **main
  goroutine's frame**, the one seat where the readiness
  wait composes (it is the poll model's seat). The driver
  waits in POLL_CHUNK-sized `poll_wait` rounds staged
  exactly like the poll model, hands ≤4096-byte chunks to
  connection goroutines over per-connection inbox channels
  (backpressure at 16 queued chunks), writes out queues fed
  over `io_out`, and is woken out of its wait immediately by
  a loopback **wake socket** poked by producers. The
  deviation touches byte plumbing only; every charter fixed
  point above is unchanged. Two runtime defects behind it
  (§3 A/B) go to the compiler line with minimal repros.

## 2. Face 4 — mandatory closure (annex registration)

Face 4 was: at ≥1000 connections with bulk close, the poll
model segfaulted (rc=139) in the reap block
(server.sla:532–568 of the base: when `len(conns) >= 256`
and at least half were closed, the table was rebuilt by
moving live connections' state by value).

**Closure mechanism, verbatim**: the goroutine model has no
reap path and no by-value connection state anywhere. A
connection's state lives in heap blocks (ConnJob/DriverConn)
that never move after creation. The driver's table is a
fixed slot array of *pointers*; retiring a connection is a
swap-remove of one pointer, and the connection goroutine
exits on its own end-of-stream marker. A bulk close is
therefore n pointer swaps plus n independent goroutine
exits — there is no operation whose cost or aliasing scales
with the number of survivors.

Gate, as run on the box against the shipped binary
(`tests/smoke_1000.py`, the annex probe, unmodified logic,
in-repo): 1200 connections opened; strided PINGs (conns 0,
1, 127, 128, 555, 999, 1000, 1199) all `+PONG`; SET on
conn 0 / GET via the last connection consistent; bulk close
of connections 100..1199; fresh-connection PING and data
checks pass; **server process still alive afterwards**.
Result: `SMOKE1000: ALL PASS`. The same sequence was also
run chained (100 held connections × 10 s → goroutine_stress
→ smoke_1000 → 5000 further connections held with PING) on
one server instance: all green.

## 3. Runtime defect register (all measured on the box master toolchain)

Probes live in the line's working set; the minimal repros
named below are the ones to hand to the compiler (DEF) line.

- **A. Net-wait starvation between goroutines** (probes
  8/10/12/13/14): with ≥2 goroutines parked in blocking net
  IO, only ~one waiter is serviced; a second waiter's
  already-queued data is delivered only after the first
  waiter's goroutine exits. `SA_TASK_CARRIERS=32` does not
  change it. Kills every per-goroutine-blocking-IO design.
- **B. Readiness wait from a spawned goroutine starves
  channels** (probes 24 vs 24b vs 24c): a spawned goroutine
  looping on `sa_std_net_poll_wait` starves channel delivery
  process-wide (exit 101); the sleep-loop control exits 42;
  the same poll_wait loop on the main goroutine exits 42.
  This is why the driver sits on the main frame.
- **C. Channel operations inside a never-returning loop
  body leak the fixed stack** (probes 27/28/30/31, F-series
  bisections): send/recv/select written textually in a loop
  body stack-allocates per iteration and does not rewind
  until the function returns. Measured: a plain recv in a
  loop body overflows the 256 KiB goroutine stack within
  thousands of iterations; a select leaks in proportion to
  its arm count (a five-select supervisor loop died at ~643
  iterations; a one-select loop survived ~4250; adding one
  select to it made it die). The same operations inside a
  CALLED function rewind on return (300k helper calls,
  clean). Engineering rule applied throughout gserver.sla:
  every long-lived goroutine is a thin loop whose body is a
  single call to a step function. Same family as the
  slanginx accept-loop stack ratchet (DEF-4).
- **D. `&(*p)` borrow of a boxed value struct is unsound in
  goroutine code** (probes 25a vs 25b, 26): it behaves like a
  borrow of a shallow temporary — header-only structs
  survive by accident (shared heap backing); scalar fields
  read wrong (AUTH silently disabled in early builds:
  AuthConfig.required/password misread) and callee mutations
  land on the phantom (AofState: flushes reported success
  and wrote nothing). Sound form used everywhere: owned
  value fields in heap blocks, borrowed in place
  (`&(*job).wst`, `&(*st).store`). Related: binding a Vec
  field with `let x = (*p).field` moves it out — on the
  shared auth config this drained the password after the
  first connection's clone (later connections got
  WRONGPASS); shared fields are read in place.
- **E. Vec-field push from a goroutine loop overflows the
  stack** (probes 18f vs 18h): pushing into a Vec field of a
  heap struct inside a goroutine loop aborts with the
  fixed-stack overflow; Box creation alone is clean.
  Workaround applied: hot tables are fixed slot arrays with
  index assignment. (Possibly the same root as C; the
  precise trigger boundary is the DEF line's to map.)
- **F. SIGPIPE kills the process** (pre-existing, poll model
  equally exposed; strace evidence from the poll era): a
  client closing mid-reply makes the server's write raise
  SIGPIPE and the whole process dies. No signal API exists
  in the drift surface (§0). Registered for the DEF line;
  acceptance/stress/bench clients are well-behaved readers,
  so gates are not blocked, but this is a real production
  exposure of both models.
- **G. Listener nonblocking has its own extern**:
  `sa_std_net_tcp_stream_set_nonblocking` fails on listener
  handles (startup exit 67); the listener variant is the
  correct face (the poll model uses it).
- **H. Codegen crash, no location**: an early drain shape
  (tuple-return destructure plus a borrow inside a select
  case) produced `error.CodegenError` with only a libc
  stack trace. The restructured shape (select only collects
  messages; processing in straight-line code) compiles.
  Which construct crashed is undetermined; both recorded.
- **I. Goroutine hot loops eat the fixed stack** (probe 19
  strace: ~3800 poll/read iterations in ~0.2 s): an EOF'd
  socket left read-interested spins a loop to overflow.
  Same family as C/DEF-4. The shipped driver is immune by
  construction (read_closed gates re-reads; the 50 ms wait
  bounds idle cadence).

## 4. Acceptance evidence (box, shipped binary)

Four-link verdicts throughout (rc=0 + expected result line
+ failure NAME SET compared + stated caliber).

- `SLAREDIS_LOOP_MODEL=goroutine sh tests/run_acceptance.sh`:
  rc=0, `ACCEPTANCE ALL PASS`; 209 check names extracted and
  diffed against the poll baseline set
  (`names_poll.txt`): **identical, zero test loss**.
- Poll regression on the same binary (default model before
  the flip commit; `sh tests/run_acceptance.sh`):
  rc=0, ALL PASS, 209-name set identical.
  `tests/poll_stress.py`: 13/13 (`FAILURES: 0`).
- `tests/goroutine_stress.py` (poll_stress restated, §5):
  13/13 (`FAILURES: 0`), including in the chained sequence
  of §2.
- `tests/smoke_1000.py`: ALL PASS (§2).
- 5000 connections held simultaneously: all connects
  succeed, PING at the 5000th connection answers; resident
  memory 12,360 kB idle → 264,376 kB at 5000 connections,
  i.e. ≈ **50.4 KiB resident increment per connection**
  (touched stack pages + job blocks + runtime structs; the
  256 KiB goroutine stacks are virtual until touched).
- Long-pipeline yield A/B (`/tmp` line probe: one connection
  running 200-command SET pipelines back-to-back for 6 s,
  135 rounds completed in both positions; a second
  connection's single PINGs measured): YIELD_EVERY=128 —
  p50 0.723 ms, p99 1.076 ms, max 1.085 ms; YIELD_EVERY=0 —
  p50 0.786 ms, p99 1.150 ms, max 1.200 ms. No starvation in
  either position (batching bounds the executor's work
  quantum, so the PING waits behind at most one batch);
  the yield discipline's measured effect at this scale is
  ≈7% on p99. Reported as measured.
- AUTH behavior (acceptance suite, 13 password + 3 nopass
  names) is byte-identical to the poll model, including the
  pipelined `AUTH`+command case verified manually during
  development.
- AOF semantic names (in the 209): replay byte-equal,
  corrupt-mid exit 69, fsync=no SIGKILL survival,
  post-rewrite appends replayed — all present and passing
  under the goroutine model.

## 5. Scenario retirement registry (PLAN §4)

`tests/poll_stress.py` remains the poll model's suite and
still passes against the poll model on this binary. Its
goroutine-model counterpart is `tests/goroutine_stress.py`
(same client-visible assertions, one for one; the registry
is also in that file's header):

- `poll chunked wait:` ×9 → `goroutine fan-in:` ×9. The poll
  model's single wait set, staged in POLL_CHUNK=128 calls,
  is now the IO driver's wait set (same staging, same chunk
  size); boundary indexes 126/127/128 keep probing the chunk
  seam; every connection behind the fan-in is an independent
  goroutine whose commands serialize through the executor.
- `poll idle sweep:` ×2 → `goroutine idle sweep:` ×2. Expiry
  moved from the poll round to the dedicated sweeper
  goroutine dispatching through the executor; the
  client-visible contract (a PX key dies while the server
  idles) is unchanged.
- `poll post-close:` ×2 → `goroutine post-close:` ×2. The
  poll model compacted its table in the reap block (the
  face-4 crash site); the goroutine model has no reap —
  pointer swap-remove plus goroutine exit. This scenario is
  the small-scale form of the smoke_1000 gate.

No scenario was dropped: 13 → 13, names mapped above.

## 6. Bench (same window, same caliber)

Redis restored on the box from Ubuntu 24.04 apt:
`redis-server 5:7.0.15-1ubuntu0.24.04.5` (`Redis server
v=7.0.15`, redis-benchmark/redis-cli 7.0.15). Repo bench
(`scripts/bench/run_bench.sh`, redis-benchmark -c 50
-n 100000 -d 3, medians of 3 after 1 warmup, empty dataset
per invocation; LPUSH at the script's documented -n 10000)
run twice back-to-back in one window: once with the poll
model (default at bench start), once with
`SLAREDIS_LOOP_MODEL=goroutine`. The script's T50 verdicts
are the previous batch's gate; this line reports deltas.

Base group + types, no pipeline (-c 50 -n 100000 -d 3;
medians of 3; each run's own Redis column shown):

| scenario | redis (poll run) | redis (gor. run) | poll ops/s | goroutine ops/s | gor/poll | poll ratio vs redis | goroutine ratio vs redis |
| --- | --- | --- | --- | --- | --- | --- | --- |
| set | 79618 | 91491 | 15615 | 6057 | 0.39 | 0.196 | 0.066 |
| get | 89206 | 93110 | 16163 | 6136 | 0.38 | 0.181 | 0.066 |
| incr | 104167 | 84818 | 15790 | 6199 | 0.39 | 0.152 | 0.073 |
| hset | 90334 | 90992 | 13198 | 5895 | 0.45 | 0.146 | 0.065 |
| lpush (n=10000) | 86207 | 79365 | 15949 | 6086 | 0.38 | 0.185 | 0.077 |
| sadd | 90334 | 92251 | 13965 | 6193 | 0.44 | 0.155 | 0.067 |
| zadd | 102987 | 93023 | 12677 | 6074 | 0.48 | 0.123 | 0.065 |

Pipeline x16 (requests/s):

| scenario | poll | goroutine | gor/poll |
| --- | --- | --- | --- |
| set P=16 | 71327 | 57372 | 0.80 |
| get P=16 | 80580 | 57703 | 0.72 |
| incr P=16 | 72622 | 54289 | 0.75 |

With AOF everysec (the bench's third target): P=1 rates are
poll 8.7k–16.2k vs goroutine 3.1k–6.3k (gor/poll ≈ 0.36–0.39
except get 0.39); P=16 get is 79.2k vs 59.8k (0.76).

Diagnostic 10k-key keyspace (-r 10000, not a gate
scenario): set poll 4393 / goroutine 1870; get poll 16540
/ goroutine 6138 (goroutine figures re-measured with the
script's own method — medians of 3 measured rounds after 1
warmup, server restarted per round — after the
interference disclosed below). The linear-scan keyspace
effect dominates the SET figure in both models.

**Reading of the delta (the context-switch cost, stated as
charter requires).** At -P 1 the goroutine model is
uniformly ≈6.0–6.2k ops/s across every command — including
commands whose execution costs differ by orders of
magnitude — with p50 ≈ 8.0–8.3 ms (poll: 13–16k ops/s,
p50 2.8–3.6 ms). The uniformity is the fingerprint of a
handoff-bound pipeline, not compute: per command the
goroutine model pays four channel handoffs (inbox → conn,
cmd → executor, done → conn, io_out → driver) plus driver
rounds, where the poll model pays none. Client-count
scaling (PING, -P 1, n=20000): goroutine c1 2429 → c10 4832
→ c50 5862 ops/s (single-client RTT ≈ 0.41 ms vs poll
≈ 0.075 ms); poll c1 13263 → c50 20576. The goroutine
aggregate saturates in the delivery pipeline (driver rounds
+ wakeup chain), not in command execution — which is why
pipelining narrows the gap to 0.72–0.80×: batches amortize
the handoffs. This is the measured price of the structure
on the current runtime scheduler; the structural gains
(face-4 class eliminated, per-connection isolation,
supervision) are what this line was chartered to buy, and
the throughput gap to Redis (0.065–0.077 vs poll's
0.12–0.20) remains attributed where sala 15 §六 puts it —
std data structures and per-command overheads — plus, now,
a quantified handoff term that belongs to the runtime's
scheduler maturity (DEF line), not to command code.

AOF fsync=always targeted comparison (redis-benchmark SET
-c 50 -P 1 -d 3; Redis `--appendonly yes --appendfsync
always`). Three independent windows agree:

| window | n | redis | goroutine | poll |
| --- | --- | --- | --- | --- |
| A | 20000 | 4990 | 204 | 182 |
| B | 10000 | 4677 | 200 | 205 |
| C | 10000 ×2 rounds | 5711 / 5546 | 215 / 220 | 233 / 230 |

Both slaredis models sit at ≈200 ops/s — the host's
serialized disk sync (~5 ms) is the whole story at -P 1:
every batch waits out its own sync, neither model
group-commits, and the model delta is noise (gor/poll
0.94–1.12 across windows). Redis's ≈23× advantage here is
its group commit. All records were verified present in
the slaredis AOF files after every run (450,000 bytes at
n=10000; 900,000 at n=20000).

ERRATUM (2026-10-30, corrected after first push): the
first-pushed version of this section reported
3958/4063 (goroutine) and 3768/3643 (poll) ops/s and
discarded the ≈200 ops/s readings as a contaminated
window. That was a misreading of interleaved background
outputs by this line: the ≈200 ops/s figures are the
reproducible ones (three windows, above) and the
thousands-class figures have no surviving raw output
behind them. The correction is limited to this
fsync=always comparison; no other number in this record
is affected. (The missing goroutine diagnostic rows in
the bench script's own summary have a disclosed cause:
an early fsync probe from this line shut the bench's
Redis down on the shared port mid-run; the diagnostic
numbers were re-measured separately, next paragraph.)

## 7. Known limitations, stated plainly

- Per-connection CmdReq/ConnJob blocks are retained for the
  process lifetime (two small blocks per connection ever
  served; see the gserver.sla header). The supervisor's
  monitor table and the driver's slot table are capped at
  8192 live entries; at the cap, new connections wait in
  the listen backlog and excess registrations are dropped
  from monitoring (serving continues).
- The executor and AOF writer are single points whose exit
  is a loud process exit (71) by design (§1).
- SIGPIPE exposure (§3 F) applies to both models.
- The driver's main-frame stack is 8 MB, not 256 KiB, but
  the loop-allocation family (§3 C) is a compiler property,
  not a model property: the poll model's loop and the
  driver both inherit it with three orders of magnitude
  more headroom. Immortality claims belong to the compiler
  line's fix, not to this delivery.
