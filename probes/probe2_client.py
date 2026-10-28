#!/usr/bin/env python3
"""Probe client #2 for probe_loop.sla (single-threaded nonblocking
round-robin): two connections, interleaved fragments.
  A connects; B connects ~0.3s later.
  A sends 'PI' (fragment), B sends full 'PING\r\n' -> B must get +PONG.
  A sends 'NG\r\n' -> A must get +PONG.
  Both half-close; server must observe EOF and close; clients see EOF.
"""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 7799

def connect() -> socket.socket:
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            return socket.create_connection((HOST, PORT), timeout=10)
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("connect failed")

def recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(64)
        if not chunk:
            raise RuntimeError(f"early EOF, got {data!r}")
        data += chunk
    return data

def main() -> int:
    a = connect()
    time.sleep(0.3)
    b = connect()
    with a, b:
        a.sendall(b"PI")
        b.sendall(b"PING\r\n")
        rb = recv_exact(b, 7)
        if rb != b"+PONG\r\n":
            print(f"PROBE2: B bad reply {rb!r}")
            return 2
        a.sendall(b"NG\r\n")
        ra = recv_exact(a, 7)
        if ra != b"+PONG\r\n":
            print(f"PROBE2: A bad reply {ra!r}")
            return 3
        a.shutdown(socket.SHUT_WR)
        b.shutdown(socket.SHUT_WR)
        if a.recv(64) != b"" or b.recv(64) != b"":
            print("PROBE2: expected EOF after server close")
            return 4
    # collect the probe's report line on the report port
    rep = connect_report()
    print(f"PROBE2 REPORT: {rep!r}")
    print("PROBE2: OK")
    return 0

def connect_report() -> bytes:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            s = socket.create_connection((HOST, 7798), timeout=10)
            break
        except OSError:
            time.sleep(0.25)
    else:
        raise RuntimeError("report connect failed")
    with s:
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(256)
            if not chunk:
                break
            data += chunk
    return data

if __name__ == "__main__":
    sys.exit(main())
