# slaredis

An SLA-native, Redis-class in-memory data store: a RESP2 server
written entirely in SLA (the SA language frontend) on top of the
sa_std TCP surface. No C, no second runtime — the same toolchain
that builds the rest of the SA ecosystem builds this server.

Status: **M0** — core five types + RESP + expiry. See
`docs/M0_DELIVERY.md` for the delivery record, measured
acceptance evidence, and the registered toolchain findings.

## Commands (M0)

- Generic: `PING`, `ECHO`, `SET` (with `EX` / `PX`), `GET`,
  `DEL`, `EXISTS`, `TYPE`, `INCR`, `DECR`, `EXPIRE`, `TTL`,
  `PTTL`, `PERSIST`
- Hash: `HSET`, `HGET`, `HDEL`, `HGETALL`
- List: `LPUSH`, `RPUSH`, `LPOP`, `RPOP`, `LRANGE`, `LLEN`
- Set: `SADD`, `SREM`, `SMEMBERS`, `SISMEMBER`
- Sorted set: `ZADD`, `ZSCORE`, `ZRANGE` (with `WITHSCORES`),
  `ZREM`

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

| Variable        | Default     | Notes                                  |
| --------------- | ----------- | -------------------------------------- |
| `SLAREDIS_HOST` | `127.0.0.1` | listen address (loopback by default)   |
| `SLAREDIS_PORT` | `6379`      | listen port                            |

Exit codes: 64 invalid port, 65 invalid/over-long host,
66 listen failure, 67 listener nonblocking setup failure.

## Acceptance test

The test client is a self-contained raw-socket RESP2 driver
(no redis-py, no redis-cli dependency):

```sh
SLAREDIS_PORT=16399 ./src/main &
python3 tests/resp_client.py 16399
```

It covers every command family byte-for-byte, the WRONGTYPE
matrix, inline commands, fragmented and pipelined delivery,
expiry timing, and parallel clients (137 checks).

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

## M0 representation — stated honestly

M0 uses standard representations, **not** Redis's compact
encodings. The compact encodings (listpack / intset / skiplist
/ rax) are the long-term roadmap, not M0 claims:

- Keyspace: one `Vec<Entry>` with a linear scan, an FNV-1a hash
  pre-filter before byte compares, tombstone slots, and a
  deep-copy compaction when tombstones dominate. No hash table
  yet.
- Entries are "fat": every payload (string, hash field/value
  vectors, list items, set members, sorted-set rows) is stored
  inline in the entry.
- Hash / set / sorted set are insertion-ordered vectors with
  linear member lookup. `ZRANGE` sorts a scratch index by
  (score, member) on each call; scores are stored as `f64`
  fields of a row struct.
- `LPUSH` rebuilds the item vector per element.
- Expiry: lazy on access plus a periodic active sweep inside
  the event loop.

## Known M0 limits

- **No persistence** (planned M1). The sa_std process surface
  has no fork primitive (exec/pidfd/kill only), so Redis-style
  fork+COW snapshotting is not available; M1 needs a different
  design.
- **No AUTH and no TLS.** The loopback default bind is the M0
  mitigation; do not expose the port beyond a trusted host.
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
  `bytes.sla` (byte/number utilities).
- `tests/resp_client.py` — the acceptance driver.
- `probes/` — the design probes the architecture was decided
  on (Mutex shape, event-loop feasibility, referee shapes).
- `docs/` — the M0 delivery record, the full acceptance log,
  and minimal repros for every registered toolchain finding.
