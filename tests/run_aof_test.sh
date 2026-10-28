#!/bin/sh
# slaredis M1 AOF acceptance orchestration.
#
# Phases:
#   1. fsync=always instance: write dataset -> BGREWRITEAOF
#      (file must shrink: 200 churn SETs collapse to one record)
#      -> post-rewrite writes -> kill -> restart -> replay dump
#      must equal the pre-kill dump byte-for-byte.
#   2. Truncated tail: garbage partial record appended to the
#      AOF; restart must still serve the same dataset.
#   3. Corrupt mid-file record: startup must refuse (exit 69).
#   4. fsync=no instance: write -> SIGKILL -> restart -> the
#      written key must be present (writes are syscalls; only
#      power loss could drop them).
#
# Usage: sh tests/run_aof_test.sh [server-binary]
# Exits non-zero on the first failed phase assertion.
set -u
BIN="${1:-./src/main}"
D=/tmp/slaredis_aof_test
PORT=16411
PORT2=16412
FAIL=0

say() { echo "== $*"; }
bad() { echo "FAIL $*"; FAIL=1; }

start_server() {
    # $1=port $2=aofpath $3=fsync
    SLAREDIS_PORT=$1 SLAREDIS_AOF_PATH=$2 SLAREDIS_AOF_FSYNC=$3 \
        "$BIN" & echo $! > "$D/pid"
    i=0
    while [ $i -lt 50 ]; do
        if python3 -c "import socket; socket.create_connection(('127.0.0.1', $1), 0.2).close()" 2>/dev/null; then
            return 0
        fi
        sleep 0.1; i=$((i + 1))
    done
    return 1
}

stop_server() {
    kill "$(cat "$D/pid")" 2>/dev/null
    wait "$(cat "$D/pid")" 2>/dev/null
}

rm -rf "$D"
mkdir -p "$D"

say "phase 1: write / rewrite / restart-replay (fsync=always)"
start_server $PORT "$D/test.aof" always || { bad "server start (phase 1)"; exit 1; }
python3 tests/aof_client.py write1 $PORT > "$D/dump1.txt" || bad "write1 driver"
SIZE1=$(stat -c %s "$D/test.aof")
python3 tests/aof_client.py rewrite $PORT > "$D/dump2.txt" || bad "rewrite driver"
SIZE2=$(stat -c %s "$D/test.aof")
echo "aof size before rewrite: $SIZE1, after: $SIZE2"
[ "$SIZE2" -lt "$SIZE1" ] || bad "rewrite did not shrink the AOF ($SIZE2 !< $SIZE1)"
stop_server
start_server $PORT "$D/test.aof" always || { bad "server restart (phase 1)"; exit 1; }
python3 tests/aof_client.py dump $PORT > "$D/dump3.txt" || bad "dump driver"
if diff -u "$D/dump2.txt" "$D/dump3.txt" > "$D/diff.txt"; then
    echo "ok   AOF restart replay matches pre-kill dump byte-for-byte"
else
    bad "restart replay dump differs"; cat "$D/diff.txt"
fi
grep -q '^temp|none' "$D/dump3.txt" && echo "ok   AOF expired key absent after replay" \
    || bad "temp key should be expired after replay"
grep -q '^future|string|stays|TTL' "$D/dump3.txt" && echo "ok   AOF absolute expiry preserved" \
    || bad "future key lost its TTL through replay"
grep -q '^ctr|string|6|' "$D/dump3.txt" && echo "ok   AOF post-rewrite appends replayed" \
    || bad "ctr should be 6 after replay"
stop_server

say "phase 2: truncated tail tolerated"
printf '*3\r\n$4\r\nCELL' >> "$D/test.aof"
start_server $PORT "$D/test.aof" always || { bad "server start (truncated tail)"; exit 1; }
python3 tests/aof_client.py dump $PORT > "$D/dump4.txt" || bad "dump driver (truncated)"
if diff -q "$D/dump3.txt" "$D/dump4.txt" > /dev/null; then
    echo "ok   AOF truncated tail ignored, dataset intact"
else
    bad "dataset changed after truncated-tail restart"
fi
stop_server

say "phase 3: corrupt mid-file record refuses startup (exit 69)"
python3 - "$D/bad.aof" <<'EOF'
import sys
good = b"*3\r\n$3\r\nSET\r\n$2\r\nk1\r\n$2\r\nv1\r\n"
with open(sys.argv[1], "wb") as f:
    f.write(good)
    f.write(b"XXXXgarbage\r\n")
    f.write(good)
EOF
SLAREDIS_PORT=$PORT SLAREDIS_AOF_PATH="$D/bad.aof" timeout 5 "$BIN"
RC=$?
[ "$RC" -eq 69 ] && echo "ok   AOF corrupt file exits 69" || bad "corrupt AOF exit code $RC != 69"

say "phase 4: fsync=no write survives SIGKILL + restart"
start_server $PORT2 "$D/no.aof" no || { bad "server start (fsync=no)"; exit 1; }
python3 tests/aof_client.py setnk $PORT2 || bad "setnk driver"
kill -9 "$(cat "$D/pid")" 2>/dev/null
wait "$(cat "$D/pid")" 2>/dev/null
start_server $PORT2 "$D/no.aof" no || { bad "server restart (fsync=no)"; exit 1; }
python3 tests/aof_client.py checknk $PORT2 || bad "checknk driver"
stop_server

if [ "$FAIL" -ne 0 ]; then
    echo "AOF TESTS FAILED"
    exit 1
fi
echo "AOF TESTS ALL PASS"
