#!/bin/sh
# slaredis full acceptance: M0 protocol suite (137 checks,
# unmodified) + M1 listpack suite + M1 AUTH suite + M1 AOF suite.
#
# Usage: sh tests/run_acceptance.sh [server-binary]
# Run from the repo root. Exits non-zero if any suite fails.
set -u
BIN="${1:-./src/main}"
FAIL=0

start_on() {
    # $1=port; extra env comes from the environment of this call
    env "$@" "$BIN" & echo $! > /tmp/slaredis_acc.pid
    i=0
    while [ $i -lt 50 ]; do
        if python3 -c "import socket; socket.create_connection(('127.0.0.1', $SLAREDIS_PORT), 0.2).close()" 2>/dev/null; then
            return 0
        fi
        sleep 0.1; i=$((i + 1))
    done
    return 1
}

stop_srv() {
    kill "$(cat /tmp/slaredis_acc.pid)" 2>/dev/null
    wait "$(cat /tmp/slaredis_acc.pid)" 2>/dev/null
}

# --- plain server: M0 + listpack + AUTH-on-nopass ---
export SLAREDIS_PORT=16399
start_on || { echo "server failed to start"; exit 1; }
echo "===== M0 resp_client (16399)"
python3 tests/resp_client.py 16399 || FAIL=1
echo "===== M1 listpack_client (16399)"
python3 tests/listpack_client.py 16399 || FAIL=1
echo "===== M1 auth_client nopass (16399)"
python3 tests/auth_client.py 16399 nopass || FAIL=1
stop_srv

# --- password server: AUTH gate suite ---
export SLAREDIS_PORT=16410
export SLAREDIS_PASSWORD=m1-secret
start_on || { echo "auth server failed to start"; exit 1; }
echo "===== M1 auth_client with password (16410)"
python3 tests/auth_client.py 16410 m1-secret || FAIL=1
stop_srv
unset SLAREDIS_PASSWORD

# --- AOF suite (own orchestration) ---
echo "===== M1 AOF suite"
sh tests/run_aof_test.sh "$BIN" || FAIL=1

if [ "$FAIL" -ne 0 ]; then
    echo "ACCEPTANCE FAILED"
    exit 1
fi
echo "ACCEPTANCE ALL PASS"
