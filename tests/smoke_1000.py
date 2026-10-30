# smoke_1000.py -- >=1000 connections + bulk close acceptance
# gate for the goroutine loop model (face-4 mandatory closure,
# PLAN-GOROUTINE annex 2026-10-30 12:48).
#
# Provenance: promoted unchanged (assertions identical) from
# the E-batch evidence probe
#   ~/workspace/chain-local/evidence/form3-probes/smoke_1000.py
# where it first exposed face 4 against the poll model: 1200
# connections open/PING/SET/GET all passed, then the bulk
# close segfaulted the server (rc=139) in the poll reap block
# (server.sla:532-568; threshold K=300 survived, K=700/1000
# died). In the goroutine model there is no reap path (see
# goroutine_stress.py's registry): this test must print
# SMOKE1000: ALL PASS with the server still alive at the end.
#
#   python3 tests/smoke_1000.py [port]

#!/usr/bin/env python3
"""1000+ connection smoke: open 1200 concurrent connections to the
server on PORT, PING a strided sample, SET on conn 0 / GET on the
last conn, then close half en masse and confirm service continues.
Prints SMOKE1000: ALL PASS and exits 0 on success."""
import socket
import sys

PORT = int(sys.argv[1])
N = 1200


def cmd(s, *args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    s.sendall(out)


def readline(s, buf):
    while b"\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise IOError("eof")
        buf += chunk
    line, buf = buf.split(b"\r\n", 1)
    return line, buf


def reply(s, buf=b""):
    line, buf = readline(s, buf)
    if line.startswith(b"$"):
        n = int(line[1:])
        if n == -1:
            return None, buf
        while len(buf) < n + 2:
            chunk = s.recv(4096)
            if not chunk:
                raise IOError("eof")
            buf += chunk
        return buf[:n], buf[n + 2:]
    return line[1:], buf


fails = []


def check(name, cond, detail=b""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else " %r" % (detail,)))
    if not cond:
        fails.append(name)


conns = []
try:
    for i in range(N):
        conns.append(socket.create_connection(("127.0.0.1", PORT), 10))
    check("opened %d conns" % N, len(conns) == N)
    for idx in (0, 1, 127, 128, 555, 999, 1000, N - 1):
        cmd(conns[idx], "PING")
        r, _ = reply(conns[idx])
        check("PING on conn %d" % idx, r == b"PONG", r)
    cmd(conns[0], "SET", "smoke", "v1200")
    r, _ = reply(conns[0])
    check("SET on conn 0", r == b"OK", r)
    cmd(conns[N - 1], "GET", "smoke")
    r, _ = reply(conns[N - 1])
    check("GET via last conn", r == b"v1200", r)
    for c in conns[100:]:
        c.close()
    import time
    time.sleep(0.5)
    fresh = socket.create_connection(("127.0.0.1", PORT), 5)
    cmd(fresh, "PING")
    r, _ = reply(fresh)
    check("post-mass-close fresh PING", r == b"PONG", r)
    cmd(fresh, "GET", "smoke")
    r, _ = reply(fresh)
    check("post-mass-close data intact", r == b"v1200", r)
    fresh.close()
finally:
    for c in conns[:100]:
        try:
            c.close()
        except OSError:
            pass
print("FAILURES: %d" % len(fails))
print("SMOKE1000: " + ("ALL PASS" if not fails else "FAILED"))
sys.exit(1 if fails else 0)
