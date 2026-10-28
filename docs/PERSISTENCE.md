# slaredis persistence — decision record and implemented design

Status: **decided and implemented (M1)**. This document is the
final version of the persistence design: the options considered,
the ruling, and the exact form that shipped.

## The decision

Three models were presented to the user with a JEV consult
(jev-1.14.0, 2026-10-28):

| Model | Sketch | JEV probability |
| ----- | ------ | --------------- |
| **A — AOF only** | Journal every executed write command to an append-only file; replay at startup; periodic rewrite compacts the file. | **0.98** |
| B — Thread snapshot (RDB-style) | Serialize the whole dataset to a snapshot file on a schedule / on demand, from the event-loop thread (no fork exists). | 0.02 |
| C — Add a fork primitive to sa_std | Redis-faithful fork+COW snapshotting, at the cost of new std surface and a sci change. | 0.00 |

**Ruling: model A.** Approved by the user 2026-10-28 19:17
under the standing "JEV decides by default" rule. Model B is
kept as a reserved interface only (see below); model C is
explicitly not pursued.

Why A fits this codebase, measured rather than asserted:

- The server is one process, one thread, one event loop (M0
  decision, taken on probe evidence). AOF appends are plain
  file-handle writes from that same loop — no new concurrency,
  no shared state, no ownership-model friction: every journal
  record is a value-threaded `Vec<u8>` built from the command's
  own arguments, the idiom the whole codebase already uses.
- B's snapshot has to serialize the dataset from inside the
  loop, stalling all clients for the full serialization (no
  fork, no COW). AOF spreads the same total work across the
  writes themselves; the rewrite reuses the dataset serializer
  and pays the stall once per compaction, at a size trigger.
- C contradicts the layering rule (no sci changes from app
  lines) and buys its fidelity with process-model risk the
  single-thread design deliberately avoided.

## Implemented form (model A)

Configuration is environment-only, like the rest of slaredis:

| Variable | Meaning |
| -------- | ------- |
| `SLAREDIS_AOF_PATH` | Set non-empty → AOF enabled at this path (≤ 255 bytes, else exit 68). Unset/empty → persistence off (M0 behavior). |
| `SLAREDIS_AOF_FSYNC` | `always` \| `everysec` \| `no`. Default **everysec** (rationale below). Anything else → exit 68. |

### Journal hook

After a command executes, if it is a write command and its
reply was not an error, its record is appended before the reply
is flushed. A record is the command in RESP multibulk form,
with one determinism rewrite borrowed from Redis: relative
expiries are journaled as absolute deadlines — `SET k v EX n`
becomes `SET k v` + `PEXPIREAT k <deadline>`, `EXPIRE k n`
becomes `PEXPIREAT k <deadline>` — so replaying the file at any
later time reproduces the original deadlines instead of
re-extending them. `PEXPIREAT` exists as a command precisely to
be this canonical form.

### fsync policy and the default

- `always`: fsync (fdatasync) after every record. Strongest
  guarantee; every write pays disk latency inside the event
  loop.
- `everysec` (default): records are written with `write()`
  immediately; fsync runs at most once per second from the
  loop tick. Worst case ≈ 1 s of acknowledged writes lost on
  an OS crash — the process itself crashing loses nothing that
  was written.
- `no`: never fsync; the OS decides. Fastest; loss window is
  the OS's, unbounded.

The default is **everysec** because it is Redis's own default
under the same trade-off, and it matches this server's shape:
the single-threaded loop must never block per command on disk
latency by default, while a one-second bound is an explicit,
documented loss window — not a silent one. Operators who need
the stronger or faster ends have named settings for them.

### Startup replay

Replay runs before the listener exists. The file is read whole
and executed record by record against the store. Replay is
stricter than the wire parser: records must be multibulk (the
only form the journal writes).

- Missing or empty file → start empty.
- File ends mid-record (writer crashed during an append) →
  the incomplete tail is ignored, everything before it is
  applied. This is the crash-tail tolerance Redis also has.
- A corrupt record anywhere else → **startup refuses, exit
  69**. Serving a silently half-loaded dataset is not an
  option.

### Rewrite (compaction)

`BGREWRITEAOF` serializes the full live dataset (the dataset
serializer: one canonical command per key, expiries as
PEXPIREAT), writes it to `<path>.rewrite.tmp`, fsyncs it, then
atomically renames it over the live AOF and reopens the append
handle. An automatic rewrite also fires from the loop tick
when the file has grown by ≥ 1 MiB since the last rewrite AND
at least doubled (Redis's percentage rule with a small-system
minimum).

Deviations from Redis's BGREWRITEAOF, stated plainly:

- **Synchronous.** Redis forks a child; there is no fork here,
  and the single-thread model makes the rewrite a loop stall
  proportional to dataset size. The reply therefore reports
  completion (`+AOF rewrite completed`), not Redis's
  "background started" form — the reply text does not claim
  work is happening somewhere it is not.
- Writes that arrive during a Redis background rewrite are
  buffered and appended to the new file; here there are no
  "during" writes, because the loop is the only writer and it
  is busy rewriting.
- The serializer assembles its output in memory before
  writing (a streaming serializer is future work, recorded in
  the M1 delivery notes).

### Failure posture

An append or fsync failure marks the AOF **broken**: the
in-flight command's reply stands (it executed), and from then
on write commands are refused with
`-ERR AOF write failed: persistence is broken, writes are refused`
while reads keep working. Refusing loudly beats Redis's
log-and-continue here, because this server has no log channel
(stdout is unusable under the current toolchain — M0 finding
8): a silent continuation would be invisible, a refused write
is not.

## Reserved / not implemented

- **Model B (snapshots):** `snapshot_save` in `src/aof.sla` is
  an interface reservation only — declared, documented, wired
  to nothing, returning `SNAPSHOT_NOT_IMPLEMENTED`. If a future
  ruling wants snapshots, the dataset serializer is the payload
  writer and this is the entry point.
- **Model C (fork in sa_std):** not pursued, per the ruling.
