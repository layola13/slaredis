# bench results 20261028-122900

Host: Linux 2d60bdf6bf1a 6.6.159+ #1 SMP Thu Oct 22 03:07:54 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux
Redis: Redis server v=7.0.15 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64 build=d064f9f48c901a92
slaredis binary: /content/sa_all/slaredis/src/main
Load: -c 50 -n 100000 -d 3, empty dataset per invocation,
medians of 3 measured rounds after 1 warmup. Gate T50:
throughput ratio >= 0.5 and p99 ratio <= 2 per scenario.

## base + types, no pipeline

| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| set P=1 | 94607.38 | 18556.32 | 0.196 | 0.271 | 2.711 | 0.495 | 3.015 | 6.091 | FAIL | 8883.36 | 0.094 | 7.255 | FAIL |
| get P=1 | 97276.27 | 16675.00 | 0.171 | 0.263 | 2.751 | 0.439 | 3.839 | 8.745 | FAIL | 18258.17 | 0.188 | 3.767 | FAIL |
| incr P=1 | 97465.88 | 16318.54 | 0.167 | 0.263 | 2.831 | 0.407 | 6.735 | 16.548 | FAIL | 10538.52 | 0.108 | 9.351 | FAIL |
| hset P=1 | 95785.44 | 12068.55 | 0.126 | 0.271 | 3.767 | 0.647 | 5.663 | 8.753 | FAIL | 6628.22 | 0.069 | 10.919 | FAIL |
| lpush P=1 | 94339.62 | 401.74 | 0.004 | 0.287 | 125.247 | 0.431 | 253.823 | 588.916 | FAIL | 384.07 | 0.004 | 250.239 | FAIL |
| sadd P=1 | 94428.70 | 15087.51 | 0.160 | 0.271 | 3.143 | 0.455 | 4.703 | 10.336 | FAIL | 8788.12 | 0.093 | 7.847 | FAIL |
| zadd P=1 | 98814.23 | 13840.83 | 0.140 | 0.263 | 3.455 | 0.583 | 5.279 | 9.055 | FAIL | 6844.16 | 0.069 | 9.959 | FAIL |

## pipeline x16

| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| set P=16 | 961538.44 | 54347.82 | 0.057 | 0.591 | 14.375 | 1.407 | 21.007 | 14.930 | FAIL | 15290.52 | 0.016 | 73.407 | FAIL |
| get P=16 | 1030927.81 | 83263.95 | 0.081 | 0.447 | 8.879 | 1.375 | 15.527 | 11.292 | FAIL | 85178.88 | 0.083 | 11.719 | FAIL |
| incr P=16 | 1176470.62 | 47036.69 | 0.040 | 0.543 | 16.959 | 1.103 | 22.111 | 20.046 | FAIL | 18214.94 | 0.015 | 60.543 | FAIL |

## diagnostic: 10k-key keyspace (not a gate scenario)

| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| set P=1 (diagnostic, -r 10000) | 71890.73 | 3872.52 | 0.054 | 0.399 | 12.655 | 0.863 | 21.327 | 24.713 | FAIL | - | - | - | - |
| get P=1 (diagnostic, -r 10000) | 83963.05 | 16728.00 | 0.199 | 0.327 | 2.751 | 0.663 | 4.351 | 6.563 | FAIL | - | - | - | - |

