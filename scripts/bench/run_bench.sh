#!/bin/sh
# run_bench.sh -- slaredis vs native Redis, same host, redis-benchmark.
#
# Gate (user ruling 2026-10-28, JEV threshold T50, p=0.60):
# per scenario, throughput >= 50% of Redis AND p99 latency <= 2x
# Redis, else the scenario fails. Medians of 3 measured rounds
# (1 warmup round first, discarded).
#
# Method, fixed here so runs are comparable:
#   - Same machine, serial: one server under test at a time is
#     not possible with redis-benchmark's model (it benchmarks a
#     live server), so all three servers run but only the target
#     receives load; the other two idle.
#   - Every benchmark invocation starts from an EMPTY dataset:
#     Redis is FLUSHALLed, the slaredis processes are restarted
#     (slaredis has no FLUSHALL; the AOF instance also gets a
#     fresh file). Without this, LPUSH-style tests would measure
#     leftover data from earlier rounds.
#   - redis-benchmark defaults are used for key selection (no
#     -r): every command in a test hits the same key, the
#     canonical published configuration, identical for both
#     servers. A separate DIAGNOSTIC group at the end re-runs
#     SET/GET with -r 10000 (10k-key keyspace) and is reported
#     in the same table, marked diagnostic: it quantifies the
#     known linear-scan keyspace, it is not part of the gate
#     scenario set the ruling prescribed.
#   - Targets: native Redis on 16379, slaredis on 16380,
#     slaredis+AOF (fsync=everysec, the shipped default) on
#     16381. The AOF column exists because write throughput
#     with persistence on vs off was required separately.
#   - Clients: -c 50 -n 100000 -d 3 (small values), tests listed
#     below; pipeline group repeats the base trio with -P 16.
#     ONE documented exception: lpush runs at -n 10000 for both
#     servers. At -n 100000 the slaredis side cannot complete
#     (measured quadratic: 10k pushes 20.4 s single-client,
#     projecting ~2000 s at 100k) because the M0 list
#     representation deep-clones on prepend -- that measurement
#     is itself a reported finding; the scenario still runs and
#     is judged at the reduced n, identically for Redis.
#
# Env knobs: SLAREDIS_BIN (default: src/main next to the repo
# root), OUT (default: scripts/bench/results/<timestamp>).
# Run from the repo root. Requires redis-server, redis-benchmark
# and redis-cli on PATH (apt: redis-server redis-tools).

set -eu
cd "$(dirname "$0")/../.."
ROOT=$(pwd)
BIN=${SLAREDIS_BIN:-"$ROOT/src/main"}
TS=$(date +%Y%m%d-%H%M%S)
OUT=${OUT:-"$ROOT/scripts/bench/results/$TS"}
mkdir -p "$OUT/raw"

RPORT=16379
SPORT=16380
APORT=16381
AOF_FILE=/tmp/slaredis-bench.aof
N=100000
CLIENTS=50

SLA_PID=""
AOF_PID=""

wait_port() {
    # $1=port -- poll until the server answers PING (max ~10 s).
    # A fixed sleep after (re)start proved racy on a loaded host:
    # one run started benchmarking against a server that had not
    # bound yet, and redis-benchmark spins instead of exiting.
    I=0
    while [ $I -lt 50 ]; do
        if python3 -c "
import socket, sys
try:
    s = socket.create_connection(('127.0.0.1', $1), timeout=1)
    s.sendall(b'PING\r\n')
    s.recv(16)
    s.close()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
            return 0
        fi
        I=$((I + 1))
        sleep 0.2
    done
    echo "server on port $1 never became ready" >&2
    return 1
}

stop_sla() {
    [ -n "$SLA_PID" ] && kill "$SLA_PID" 2>/dev/null || true
    [ -n "$AOF_PID" ] && kill "$AOF_PID" 2>/dev/null || true
    # Reap them: the port is not free until the process is gone,
    # and a successor binding too early exits with listen failure.
    [ -n "$SLA_PID" ] && wait "$SLA_PID" 2>/dev/null || true
    [ -n "$AOF_PID" ] && wait "$AOF_PID" 2>/dev/null || true
    SLA_PID=""; AOF_PID=""
    sleep 0.3
}

cleanup() {
    stop_sla
    redis-cli -p $RPORT shutdown nosave >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

start_servers() {
    stop_sla
    SLAREDIS_PORT=$SPORT "$BIN" & SLA_PID=$!
    rm -f "$AOF_FILE"
    SLAREDIS_PORT=$APORT SLAREDIS_AOF_PATH="$AOF_FILE" SLAREDIS_AOF_FSYNC=everysec "$BIN" & AOF_PID=$!
    wait_port $SPORT
    wait_port $APORT
}

reset_target() {
    # $1 = redis|sla|slaaof -- empty dataset for the next invocation
    case "$1" in
        redis) redis-cli -p $RPORT flushall >/dev/null ;;
        sla)
            [ -n "$SLA_PID" ] && kill "$SLA_PID" 2>/dev/null || true
            [ -n "$SLA_PID" ] && wait "$SLA_PID" 2>/dev/null || true
            SLAREDIS_PORT=$SPORT "$BIN" & SLA_PID=$!
            wait_port $SPORT
            ;;
        slaaof)
            [ -n "$AOF_PID" ] && kill "$AOF_PID" 2>/dev/null || true
            [ -n "$AOF_PID" ] && wait "$AOF_PID" 2>/dev/null || true
            rm -f "$AOF_FILE"
            SLAREDIS_PORT=$APORT SLAREDIS_AOF_PATH="$AOF_FILE" SLAREDIS_AOF_FSYNC=everysec "$BIN" & AOF_PID=$!
            wait_port $APORT
            ;;
    esac
}

# Native Redis up once (flushall resets it per invocation).
redis-server --port $RPORT --save '' --appendonly no \
    --protected-mode no --daemonize yes --dir /tmp \
    --logfile "$OUT/redis-server.log"
sleep 0.5
start_servers

bench() {
    # $1=target-name $2=port $3=test $4=pipeline $5=round $6=extra
    # A timed-out or failed invocation must not kill the whole
    # run: the raw file records it and the row reports NO DATA.
    local n=$N
    [ "$3" = "lpush" ] && n=10000
    timeout 600 redis-benchmark -h 127.0.0.1 -p "$2" -c $CLIENTS \
        -n $n -d 3 -P "$4" $6 -t "$3" > "$OUT/raw/$1-$3-p$4-r$5.txt" 2>&1 \
        || echo "BENCH_ERROR rc=$?" >> "$OUT/raw/$1-$3-p$4-r$5.txt"
}

# parse_raw FILE -> "rps p50 p99" from one raw output
parse_raw() {
    awk '
        /^Throughput summary:/ { }
        /throughput summary:/ { rps = $3 }
        /Latency by percentile distribution:/ { lat = 1; next }
        lat && NF >= 6 && $1 ~ /^[0-9.]+$/ { p50 = $3; p99 = $5; lat = 0 }
        END { print rps, p50, p99 }
    ' "$1"
}

median3() {
    printf '%s\n' "$@" | sort -n | sed -n '2p'
}

verdict() {
    # $1=sla_rps $2=redis_rps $3=sla_p99 $4=redis_p99 -> PASS/FAIL
    awk -v s="$1" -v r="$2" -v sp="$3" -v rp="$4" 'BEGIN {
        if (r > 0 && s / r >= 0.5 && sp <= 2 * rp) print "PASS"; else print "FAIL"
    }'
}

ratio() {
    awk -v a="$1" -v b="$2" 'BEGIN { if (b == 0) print "n/a"; else printf "%.3f", a / b }'
}

SUMMARY="$OUT/summary.md"
{
    echo "# bench results $TS"
    echo
    echo "Host: $(uname -a)"
    echo "Redis: $(redis-server --version)"
    echo "slaredis binary: $BIN"
    echo "Load: -c $CLIENTS -n $N -d 3, empty dataset per invocation,"
    echo "medians of 3 measured rounds after 1 warmup. Gate T50:"
    echo "throughput ratio >= 0.5 and p99 ratio <= 2 per scenario."
    echo
} > "$SUMMARY"

run_group() {
    # $1=pipeline $2=label $3=r-range("" for gate grid) ; rest=tests
    PIPE=$1; LABEL=$2; RRANGE=$3; shift 3
    {
        echo "## $LABEL"
        echo
        echo "| scenario | redis ops/s | sla ops/s | ratio | redis p50 | sla p50 | redis p99 | sla p99 | p99 ratio | sla verdict | sla+AOF ops/s | AOF ratio | AOF p99 | AOF verdict |"
        echo "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    } >> "$SUMMARY"
    for T in "$@"; do
        R_RPS=""; R_P50=""; R_P99=""
        S_RPS=""; S_P50=""; S_P99=""
        A_RPS=""; A_P50=""; A_P99=""
        for R in 0 1 2 3; do
            EXTRA=""
            [ -n "$RRANGE" ] && EXTRA="-r $RRANGE"
            reset_target redis;  bench redis  $RPORT "$T" "$PIPE" $R "$EXTRA"
            reset_target sla;    bench sla    $SPORT "$T" "$PIPE" $R "$EXTRA"
            if [ -z "$RRANGE" ]; then
                reset_target slaaof; bench slaaof $APORT "$T" "$PIPE" $R "$EXTRA"
            fi
            if [ $R -gt 0 ]; then
                V=$(parse_raw "$OUT/raw/redis-$T-p$PIPE-r$R.txt")
                R_RPS="$R_RPS ${V%% *}"; REST=${V#* }; R_P50="$R_P50 ${REST%% *}"; R_P99="$R_P99 ${REST##* }"
                V=$(parse_raw "$OUT/raw/sla-$T-p$PIPE-r$R.txt")
                S_RPS="$S_RPS ${V%% *}"; REST=${V#* }; S_P50="$S_P50 ${REST%% *}"; S_P99="$S_P99 ${REST##* }"
                if [ -z "$RRANGE" ]; then
                    V=$(parse_raw "$OUT/raw/slaaof-$T-p$PIPE-r$R.txt")
                    A_RPS="$A_RPS ${V%% *}"; REST=${V#* }; A_P50="$A_P50 ${REST%% *}"; A_P99="$A_P99 ${REST##* }"
                fi
            fi
        done
        r_rps=$(median3 $R_RPS); r_p50=$(median3 $R_P50); r_p99=$(median3 $R_P99)
        s_rps=$(median3 $S_RPS); s_p50=$(median3 $S_P50); s_p99=$(median3 $S_P99)
        if [ -z "$s_rps" ] || [ -z "$r_rps" ]; then
            echo "| $T P=$PIPE | $r_rps | NO DATA | - | $r_p50 | - | $r_p99 | - | - | FAIL (no data) | - | - | - | - |" >> "$SUMMARY"
            echo "done: $LABEL $T (NO DATA)" >&2
            continue
        fi
        V1=$(verdict "$s_rps" "$r_rps" "$s_p99" "$r_p99")
        if [ -z "$RRANGE" ]; then
            a_rps=$(median3 $A_RPS); a_p99=$(median3 $A_P99)
            V2=$(verdict "$a_rps" "$r_rps" "$a_p99" "$r_p99")
            echo "| $T P=$PIPE | $r_rps | $s_rps | $(ratio "$s_rps" "$r_rps") | $r_p50 | $s_p50 | $r_p99 | $s_p99 | $(ratio "$s_p99" "$r_p99") | $V1 | $a_rps | $(ratio "$a_rps" "$r_rps") | $a_p99 | $V2 |" >> "$SUMMARY"
        else
            echo "| $T P=$PIPE (diagnostic, -r $RRANGE) | $r_rps | $s_rps | $(ratio "$s_rps" "$r_rps") | $r_p50 | $s_p50 | $r_p99 | $s_p99 | $(ratio "$s_p99" "$r_p99") | $V1 | - | - | - | - |" >> "$SUMMARY"
        fi
        echo "done: $LABEL $T" >&2
    done
    echo >> "$SUMMARY"
}

run_group 1 "base + types, no pipeline" "" set get incr hset lpush sadd zadd
run_group 16 "pipeline x16" "" set get incr
run_group 1 "diagnostic: 10k-key keyspace (not a gate scenario)" "10000" set get

echo "BENCH_DONE $OUT"
cat "$SUMMARY"
