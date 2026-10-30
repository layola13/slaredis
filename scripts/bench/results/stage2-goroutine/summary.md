# bench results 20261030-072517

Host: Linux 1ab1452dc2e8 6.6.159+ #1 SMP Thu Oct 22 03:07:54 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux
Redis: Redis server v=7.0.15 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64 build=d064f9f48c901a92
slaredis binary: /content/geo-slaredis/src/main
Load: -c 50 -n 100000 -d 3, empty dataset per invocation,
medians of 3 measured rounds after 1 warmup. Gate T50:
throughput ratio >= 0.5 and p99 ratio <= 2 per scenario.

## base + types, no pipeline

| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| set P=1 | 91491.30 | 6057.30 | 0.066 | 0.295 | 8.175 | 0.775 | 11.111 | 14.337 | FAIL | 3647.37 | 0.040 | 17.791 | FAIL |
| get P=1 | 93109.87 | 6135.72 | 0.066 | 0.295 | 8.015 | 0.767 | 10.575 | 13.787 | FAIL | 6294.06 | 0.068 | 10.511 | FAIL |
| incr P=1 | 84817.64 | 6199.24 | 0.073 | 0.303 | 7.935 | 0.791 | 10.623 | 13.430 | FAIL | 3481.17 | 0.041 | 19.087 | FAIL |
| hset P=1 | 90991.81 | 5894.84 | 0.065 | 0.295 | 8.271 | 0.895 | 11.967 | 13.371 | FAIL | 3089.95 | 0.034 | 20.607 | FAIL |
| lpush P=1 | 79365.08 | 6086.43 | 0.077 | 0.319 | 8.063 | 0.735 | 12.343 | 16.793 | FAIL | 3602.31 | 0.045 | 18.095 | FAIL |
| sadd P=1 | 92250.92 | 6193.10 | 0.067 | 0.287 | 7.999 | 0.671 | 9.871 | 14.711 | FAIL | 3457.22 | 0.037 | 17.679 | FAIL |
| zadd P=1 | 93023.25 | 6074.23 | 0.065 | 0.287 | 8.151 | 0.703 | 10.727 | 15.259 | FAIL | 3270.97 | 0.035 | 19.135 | FAIL |

## pipeline x16

| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| set P=16 | 1063829.88 | 57372.34 | 0.054 | 0.607 | 13.559 | 1.167 | 22.831 | 19.564 | FAIL | 23435.67 | 0.022 | 38.463 | FAIL |
| get P=16 | 1190476.25 | 57703.40 | 0.048 | 0.495 | 13.575 | 0.927 | 22.079 | 23.818 | FAIL | 59844.41 | 0.050 | 22.303 | FAIL |
| incr P=16 | 961538.44 | 54288.82 | 0.056 | 0.631 | 13.959 | 1.407 | 23.279 | 16.545 | FAIL | 22988.51 | 0.024 | 40.351 | FAIL |

## diagnostic: 10k-key keyspace (not a gate scenario)

| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
