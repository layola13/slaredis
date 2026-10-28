#!/usr/bin/env python3
"""Probe client for probe_core.sla: connects to 127.0.0.1:7799, sends
PING\r\n in two fragments, expects +PONG\r\n, half-closes its write
side, then expects the server to close (recv -> b'')."""
import socket
import sys
import time

HOST, PORT = "127.0.0.1", 7799

def main() -> int:
    deadline = time.time() + 60
    sock = None
    while time.time() < deadline:
        try:
            sock = socket.create_connection((HOST, PORT), timeout=5)
            break
        except OSError:
            time.sleep(0.25)
    if sock is None:
        print("PROBE_CLIENT: connect failed")
        return 1
    with sock:
        sock.sendall(b"PIN")
        time.sleep(0.1)
        sock.sendall(b"G\r\n")
        data = b""
        while len(data) < 7:
            chunk = sock.recv(64)
            if not chunk:
                print(f"PROBE_CLIENT: early EOF, got {data!r}")
                return 2
            data += chunk
        if data != b"+PONG\r\n":
            print(f"PROBE_CLIENT: bad reply {data!r}")
            return 3
        sock.shutdown(socket.SHUT_WR)
        tail = sock.recv(64)
        if tail != b"":
            print(f"PROBE_CLIENT: expected EOF after server close, got {tail!r}")
            return 4
    print("PROBE_CLIENT: OK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
