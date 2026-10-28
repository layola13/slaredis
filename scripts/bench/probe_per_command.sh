#!/bin/sh
# probe_per_command.sh -- second attribution probe pair:
#   1. GET at -c 1 -P 16 for both servers: pipelining without
#      concurrency. If slaredis lands at the same ops/s as its
#      -c 50 -P 16 figure, the residual cost is per-command
#      userspace work, independent of connection count.
#   2. strace -c on the AOF server during a SET burst: counts
#      file writes per journaled command (record granularity)
#      and shows whether fsync appears at everysec cadence.
#
# Usage: sh scripts/bench/probe_per_command.sh [outdir]

set -eu
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
BIN=${SLAREDIS_BIN:-"$ROOT/src/main"}
OUT=${1:-"$ROOT/scripts/bench/results/per-command-$(date +%Y%m%d-%H%M%S)"}
mkdir -p "$OUT"
RPORT=16379
SPORT=16380
APORT=16381
AOF_FILE=/tmp/slaredis-probe.aof

SLA_PID=""; AOF_PID=""
cleanup() {
    [ -n "$SLA_PID" ] && kill "$SLA_PID" 2>/dev/null || true
    [ -n "$AOF_PID" ] && kill "$AOF_PID" 2>/dev/null || true
    redis-cli -p $RPORT shutdown nosave >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

redis-server --port $RPORT --save '' --appendonly no \
    --protected-mode no --daemonize yes --dir /tmp \
    --logfile "$OUT/redis-server.log"
SLAREDIS_PORT=$SPORT "$BIN" & SLA_PID=$!
rm -f "$AOF_FILE"
SLAREDIS_PORT=$APORT SLAREDIS_AOF_PATH="$AOF_FILE" SLAREDIS_AOF_FSYNC=everysec "$BIN" & AOF_PID=$!
sleep 1

{
    echo "== GET c1 P16 =="
    echo "-- sla"
    timeout 60 redis-benchmark -h 127.0.0.1 -p $SPORT -c 1 -n 50000 -d 3 -P 16 -t get 2>&1 | grep "throughput summary"
    echo "-- redis"
    timeout 60 redis-benchmark -h 127.0.0.1 -p $RPORT -c 1 -n 100000 -d 3 -P 16 -t get 2>&1 | grep "throughput summary"
} > "$OUT/c1_p16.txt" 2>&1

if command -v strace >/dev/null 2>&1; then
    timeout 60 redis-benchmark -h 127.0.0.1 -p $APORT -c 10 \
        -n 30000 -d 3 -P 1 -t set > "$OUT/burst_set_aof.txt" 2>&1 &
    BPID=$!
    sleep 1
    timeout 8 strace -c -p "$AOF_PID" -o "$OUT/strace_aof_set.txt" || true
    wait $BPID 2>/dev/null || true
fi

echo "PROBE_DONE $OUT"
cat "$OUT/c1_p16.txt"
