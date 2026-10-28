#!/bin/sh
# probe_attribution.sh -- follow-up probes for a bench run, used
# to attribute a throughput/latency gap to a layer. Not part of
# the gate; run manually after run_bench.sh.
#
#   1. Single-client GET/SET (-c 1): isolates per-request RTT
#      from concurrency effects. If single-client RTT is already
#      ~ms, the cost is in the request path / loop cadence, not
#      in lock contention (there is none: one thread).
#   2. strace -c on the slaredis server during a -c 50 GET burst:
#      the syscall census separates "time is spent waiting"
#      (many nanosleep/clock_nanosleep calls, few read/write)
#      from "time is spent in syscalls" (read/write dominated)
#      from "time is spent in userspace" (few syscalls, high
#      wall time -> allocation/copy churn in generated code).
#
# Usage: sh scripts/bench/probe_attribution.sh [outdir]
# Expects: redis-server/redis-benchmark/redis-cli on PATH,
# strace optional (probe 2 is skipped without it), slaredis
# binary at src/main (or $SLAREDIS_BIN).

set -eu
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
BIN=${SLAREDIS_BIN:-"$ROOT/src/main"}
OUT=${1:-"$ROOT/scripts/bench/results/attribution-$(date +%Y%m%d-%H%M%S)"}
mkdir -p "$OUT"
RPORT=16379
SPORT=16380

SLA_PID=""
cleanup() {
    [ -n "$SLA_PID" ] && kill "$SLA_PID" 2>/dev/null || true
    redis-cli -p $RPORT shutdown nosave >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

redis-server --port $RPORT --save '' --appendonly no \
    --protected-mode no --daemonize yes --dir /tmp \
    --logfile "$OUT/redis-server.log"
SLAREDIS_PORT=$SPORT "$BIN" & SLA_PID=$!
sleep 1

{
    echo "== single client, -n 20000 -d 3 =="
    for T in redis sla; do
        if [ $T = redis ]; then P=$RPORT; else P=$SPORT; fi
        for C in get set; do
            echo "-- $T $C c1"
            timeout 120 redis-benchmark -h 127.0.0.1 -p $P -c 1 \
                -n 20000 -d 3 -P 1 -t $C 2>&1 | grep -E "throughput summary|p50|p99" || true
            timeout 120 redis-benchmark -h 127.0.0.1 -p $P -c 1 \
                -n 20000 -d 3 -P 1 -t $C 2>&1 | \
                awk '/Latency by percentile/{l=1;next} l&&NF>=6&&$1~/^[0-9.]+$/{print "p50="$3" p99="$5; exit}'
        done
    done
} > "$OUT/single_client.txt" 2>&1

if command -v strace >/dev/null 2>&1; then
    timeout 120 redis-benchmark -h 127.0.0.1 -p $SPORT -c 50 \
        -n 1000000 -d 3 -P 1 -t get > "$OUT/burst_get.txt" 2>&1 &
    BPID=$!
    sleep 2
    timeout 12 strace -c -p "$SLA_PID" -o "$OUT/strace_sla_get.txt" || true
    kill $BPID 2>/dev/null || true
    wait $BPID 2>/dev/null || true
fi

echo "PROBES_DONE $OUT"
cat "$OUT/single_client.txt"
