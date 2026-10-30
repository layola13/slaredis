# slaredis

An SLA-native, Redis-class in-memory data store: a RESP2 server
written entirely in SLA (the SA language frontend) on top of the
sa_std TCP surface. No C, no second runtime — the same toolchain
that builds the rest of the SA ecosystem builds this server.

Status: **M1** — M0 plus listpack compact encodings, AUTH, and
AOF persistence. See `docs/M0_DELIVERY.md` for the M0 record,
`docs/PERSISTENCE.md` for the persistence decision and design,
and `docs/M1_DELIVERY.md` for the M1 record. The 2026-10-29
improvement line (application-layer perf integration + the
poll-based event loop) is recorded in
`docs/IMPROVE_DELIVERY.md`.

## Commands

- Generic: `PING`, `ECHO`, `SET` (with `EX` / `PX`), `GET`,
  `DEL`, `EXISTS`, `TYPE`, `INCR`, `DECR`, `EXPIRE`, `TTL`,
  `PTTL`, `PERSIST`, `PEXPIREAT`
- Hash: `HSET`, `HGET`, `HDEL`, `HGETALL`
- List: `LPUSH`, `RPUSH`, `LPOP`, `RPOP`, `LRANGE`, `LLEN`
- Set: `SADD`, `SREM`, `SMEMBERS`, `SISMEMBER`
- Sorted set: `ZADD`, `ZSCORE`, `ZRANGE` (with `WITHSCORES`),
  `ZREM`
- Server: `AUTH`, `OBJECT ENCODING` (representation reporting;
  other OBJECT subcommands are an explicit error),
  `BGREWRITEAOF` (synchronous — see `docs/PERSISTENCE.md`)

Type errors return the Redis `WRONGTYPE` form; unknown commands
and arity errors return the Redis error forms as well.

## Build and run

Prerequisites: the SA toolchain (`sa` 0.1.8) with the SLA
language plugin installed in dev mode, built from the repo root:

```sh
SA_PLUGIN_DEV=1 sa build-exe src/main.sla   # produces src/main
./src/main                                  # 127.0.0.1:6379
```

Configuration is environment-only:

| Variable            | Default     | Notes                                             |
| ------------------- | ----------- | ------------------------------------------------- |
| `SLAREDIS_HOST`     | `127.0.0.1` | listen address (loopback by default)              |
| `SLAREDIS_PORT`     | `6379`      | listen port                                       |
| `SLAREDIS_PASSWORD` | _(unset)_   | when set non-empty, connections must `AUTH`       |
| `SLAREDIS_AOF_PATH` | _(unset)_   | when set non-empty, AOF persistence at this path  |
| `SLAREDIS_AOF_FSYNC`| `everysec`  | `always` \| `everysec` \| `no` (see PERSISTENCE)  |
| `SLAREDIS_LOOP_MODEL`| `goroutine` | `goroutine` \| `poll` -- concurrency model (see below) |
| `SLAREDIS_YIELD_EVERY`| `128`      | goroutine model: manual yield cadence of its CPU loops, in commands (0 disables) |

Exit codes: 64 invalid port, 65 invalid/over-long host,
66 listen failure, 67 listener nonblocking setup failure,
68 invalid persistence config, 69 AOF startup failure,
70 invalid `SLAREDIS_LOOP_MODEL`, 71 a core goroutine died
under the supervisor (goroutine model only).

## Concurrency models

Two serving models are compiled in, selected by
`SLAREDIS_LOOP_MODEL`:

- `goroutine` (default): one goroutine per connection, a
  single executor goroutine owning the store, dedicated AOF
  writer and expiry sweeper goroutines, a supervisor, and
  all socket IO concentrated in a driver on the main frame.
  Design and measured rationale:
  `docs/GOROUTINE_DELIVERY.md`; implementation:
  `src/gserver.sla`.
- `poll`: the original single-thread readiness loop
  (`src/server.sla`), kept selectable as the rollback shape.

## Acceptance test

The test client is a self-contained raw-socket RESP2 driver
(no redis-py, no redis-cli dependency):

```sh
SLAREDIS_PORT=16399 ./src/main &
python3 tests/resp_client.py 16399
```

It covers every command family byte-for-byte, the WRONGTYPE
matrix, inline commands, fragmented and pipelined delivery,
expiry timing, and parallel clients (137 checks). The full
acceptance — M0 suite unmodified, plus the M1 listpack, AUTH
and AOF suites — runs as one command:

```sh
sh tests/run_acceptance.sh
```

Performance is gated against native Redis on the same host
(gate T50; method and parameters fixed in the script):

```sh
sh scripts/bench/run_bench.sh
```

## Concurrency model

Single process, single thread: a round-robin event loop over
nonblocking sockets — the same shape as Redis itself. Commands
execute atomically by construction, and the active expiry
sweeper runs inside the loop (~100 ms cadence). The model was
chosen on measured evidence (recorded in
`docs/M0_DELIVERY.md`): sa_std exposes no poll/epoll surface
over TCP, and the SLA `Mutex<T>` constructor only accepts
scalar payloads, so a thread-per-connection design with a
locked shared store cannot hold the store today.

## Representation — stated honestly

- Keyspace: one `Vec<Entry>` with a linear scan, an FNV-1a hash
  pre-filter before byte compares, tombstone slots, and a
  deep-copy compaction when tombstones dominate. No hash table
  yet.
- Entries are "fat": every payload (string, hash field/value
  vectors, list items, set members, sorted-set rows) is stored
  inline in the entry.
- **Compact encoding (M1):** hash / set / zset keys start in
  the **listpack** representation (the real Redis listpack
  byte format, `src/listpack.sla`) and convert one-way to the
  plain vectors when a write would exceed 128 logical elements
  or insert an element longer than 64 bytes — Redis's
  thresholds. `OBJECT ENCODING` reports which representation a
  key is in (`listpack` vs `hashtable` / `skiplist`), so the
  conversion is observable, never silent. intset / skiplist /
  rax proper remain future work; lists are plain vectors.
- Hash / set / sorted set (plain form) are insertion-ordered
  vectors with linear member lookup. `ZRANGE` sorts a scratch
  index by (score, member) on each call; scores are stored as
  `f64` fields of a row struct.
- `LPUSH` rebuilds the item vector per element.
- Expiry: lazy on access plus a periodic active sweep inside
  the event loop.

## Known limits

- **Persistence is AOF-only** (M1, ruled design — see
  `docs/PERSISTENCE.md`). Snapshots are a reserved interface,
  not implemented; Redis-style fork+COW snapshotting is not
  available (the sa_std process surface has no fork primitive)
  and was explicitly not chosen.
- **No TLS.** AUTH exists (M1), but credentials cross the wire
  in clear text, as in Redis without TLS; the loopback default
  bind remains the mitigation for untrusted networks.
- Inline commands are whitespace-split without quote handling
  (documented deviation; multibulk is the primary form).
- Score parsing is a hand-written decimal parser (last-ulp
  differences from a correctly rounded strtod are possible);
  score formatting approximates Redis's `%.17g` (integral
  values print as integers, fractions up to 17 digits trimmed,
  extreme magnitudes go through the std formatter).
- The unknown-command error omits Redis's "with args beginning
  with" suffix.
- The server writes nothing to stdout (the std print surface
  traps on stack-array pointers; see the delivery doc).

## Layout

- `src/` — SLA sources, named by function/domain:
  `main.sla` (config + entry), `server.sla` (event loop),
  `resp.sla` (RESP2 parser + reply writers), `commands.sla`
  (dispatch + all commands), `store.sla` (keyspace + expiry),
  `bytes.sla` (byte/number utilities), `listpack.sla` (compact
  encoding), `auth.sla` (AUTH + gate), `journal.sla` (AOF
  journal hook), `aof.sla` (AOF file, replay, rewrite,
  serializer).
- `tests/` — the acceptance drivers: `resp_client.py` (M0,
  137 checks), `listpack_client.py`, `auth_client.py`,
  `aof_client.py` + `run_aof_test.sh`, all tied together by
  `run_acceptance.sh`.
- `scripts/bench/` — the native-Redis performance comparison
  (fixed method, gate T50).
- `probes/` — the design probes the architecture was decided
  on (Mutex shape, event-loop feasibility, referee shapes).
- `docs/` — the delivery records (M0, M1), the persistence
  decision record, the full acceptance log, and minimal
  repros for every registered toolchain finding.
