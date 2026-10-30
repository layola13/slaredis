# bench results 20261030-071450

Host: Linux 1ab1452dc2e8 6.6.159+ #1 SMP Thu Oct 22 03:07:54 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux
Redis: Redis server v=7.0.15 sha=00000000:0 malloc=jemalloc-5.3.0 bits=64 build=d064f9f48c901a92
slaredis binary: /content/geo-slaredis/src/main
Load: -c 50 -n 100000 -d 3, empty dataset per invocation,
medians of 3 measured rounds after 1 warmup. Gate T50:
throughput ratio >= 0.5 and p99 ratio <= 2 per scenario.

## base + types, no pipeline

| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| set P=1 | 79617.83 | 15615.24 | 0.196 | 0.311 | 2.879 | 0.967 | 4.983 | 5.153 | FAIL | 10162.60 | 0.128 | 9.007 | FAIL |
| get P=1 | 89206.06 | 16162.92 | 0.181 | 0.351 | 2.847 | 0.871 | 4.871 | 5.592 | FAIL | 16244.31 | 0.182 | 5.359 | FAIL |
| incr P=1 | 104166.67 | 15790.30 | 0.152 | 0.271 | 2.895 | 0.607 | 4.759 | 7.840 | FAIL | 10227.04 | 0.098 | 8.167 | FAIL |
| hset P=1 | 90334.23 | 13197.83 | 0.146 | 0.279 | 3.455 | 0.695 | 7.023 | 10.105 | FAIL | 8680.56 | 0.096 | 10.287 | FAIL |
| lpush P=1 | 86206.90 | 15948.96 | 0.185 | 0.295 | 2.959 | 0.655 | 4.919 | 7.510 | FAIL | 10869.57 | 0.126 | 7.687 | FAIL |
| sadd P=1 | 90334.23 | 13964.53 | 0.155 | 0.287 | 3.319 | 0.807 | 5.639 | 6.988 | FAIL | 9397.61 | 0.104 | 9.303 | FAIL |
| zadd P=1 | 102986.61 | 12677.48 | 0.123 | 0.263 | 3.583 | 0.631 | 6.263 | 9.926 | FAIL | 9113.28 | 0.088 | 9.479 | FAIL |

## pipeline x16

| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| set P=16 | 854700.88 | 71326.68 | 0.083 | 0.767 | 10.655 | 1.447 | 13.711 | 9.475 | FAIL | 25458.25 | 0.030 | 58.815 | FAIL |
| get P=16 | 1063829.88 | 80580.17 | 0.076 | 0.519 | 9.143 | 1.255 | 13.543 | 10.791 | FAIL | 79176.56 | 0.074 | 13.911 | FAIL |
| incr P=16 | 900900.88 | 72621.64 | 0.081 | 0.695 | 10.575 | 1.495 | 20.303 | 13.581 | FAIL | 24673.08 | 0.027 | 59.679 | FAIL |

## diagnostic: 10k-key keyspace (not a gate scenario)

| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| set P=1 (diagnostic, -r 10000) | 88888.89 | 4393.29 | 0.049 | 0.287 | 11.159 | 0.839 | 20.767 | 24.752 | FAIL | - | - | - | - |
| get P=1 (diagnostic, -r 10000) | 89847.26 | 16539.86 | 0.184 | 0.287 | 2.815 | 0.703 | 4.919 | 6.997 | FAIL | - | - | - | - |

