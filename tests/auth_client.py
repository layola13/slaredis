#!/usr/bin/env python3
"""slaredis M1 AUTH acceptance client.

Two modes:
  python3 tests/auth_client.py <port> <password>   server started
      with SLAREDIS_PASSWORD=<password>: gate + AUTH semantics.
  python3 tests/auth_client.py <port> nopass       server started
      without a password: AUTH must produce Redis's
      "called without any password configured" error.

Error texts are asserted byte-exact where Redis's form is the
contract (NOAUTH / WRONGPASS).
"""
import socket
import sys

HOST = "127.0.0.1"
PORT = int(sys.argv[1])
MODE = sys.argv[2]

FAILURES = []
CHECKS = [0]


def encode(*args):
    out = [b"*%d\r\n" % len(args)]
    for a in args:
        if isinstance(a, str):
            a = a.encode()
        out.append(b"$%d\r\n%s\r\n" % (len(a), a))
    return b"".join(out)


class Conn:
    def __init__(self):
        self.sock = socket.create_connection((HOST, PORT), timeout=10)
        self.buf = b""

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _readline(self):
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("server closed the connection")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _readn(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("server closed the connection")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def read_reply(self):
        line = self._readline()
        raw = line + b"\r\n"
        t, rest = line[:1], line[1:]
        if t == b"+":
            return rest.decode(), raw
        if t == b"-":
            return ("ERR", rest.decode()), raw
        if t == b":":
            return int(rest), raw
        if t == b"$":
            n = int(rest)
            if n == -1:
                return None, raw
            data = self._readn(n + 2)
            return data[:n], raw + data
        if t == b"*":
            n = int(rest)
            if n == -1:
                return None, raw
            items = []
            for _ in range(n):
                v, r = self.read_reply()
                items.append(v)
                raw += r
            return items, raw
        raise ValueError("bad reply type byte: %r" % line)

    def cmd(self, *args):
        self.sock.sendall(encode(*args))
        return self.read_reply()


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("ok   %s" % name)
    else:
        print("FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def expect(name, got, want):
    check(name, got == want, "got %r want %r" % (got, want))


def main():
    if MODE == "nopass":
        c = Conn()
        v, raw = c.cmd("AUTH", "anything")
        expect("AUTH nopass 1-arg form", v,
               ("ERR", "ERR AUTH <password> called without any password configured for the default user. Are you sure your configuration is correct?"))
        v, _ = c.cmd("AUTH", "someuser", "anything")
        expect("AUTH nopass 2-arg form", v,
               ("ERR", "ERR AUTH <username> <password> called without any password configured for the default user. Are you sure your configuration is correct?"))
        v, _ = c.cmd("PING")
        expect("AUTH nopass server still open", v, "PONG")
        c.close()
    else:
        pw = MODE
        c = Conn()
        v, raw = c.cmd("PING")
        expect("AUTH gate PING raw", raw, b"-NOAUTH Authentication required.\r\n")
        v, _ = c.cmd("GET", "k")
        expect("AUTH gate GET", v, ("ERR", "NOAUTH Authentication required."))
        v, _ = c.cmd("SET", "k", "v")
        expect("AUTH gate SET", v, ("ERR", "NOAUTH Authentication required."))
        v, _ = c.cmd("AUTH")
        check("AUTH arity reaches handler",
              isinstance(v, tuple) and "wrong number of arguments for 'auth' command" in v[1],
              "got %r" % (v,))
        v, _ = c.cmd("AUTH", "wrong-password")
        expect("AUTH wrong password", v,
               ("ERR", "WRONGPASS invalid username-password pair or user is disabled."))
        v, _ = c.cmd("AUTH", "alice", pw)
        expect("AUTH unknown user", v,
               ("ERR", "WRONGPASS invalid username-password pair or user is disabled."))
        v, _ = c.cmd("AUTH", "default", "wrong-password")
        expect("AUTH default user wrong pass", v,
               ("ERR", "WRONGPASS invalid username-password pair or user is disabled."))
        v, raw = c.cmd("AUTH", pw)
        expect("AUTH success raw", raw, b"+OK\r\n")
        v, _ = c.cmd("PING")
        expect("AUTH post-auth PING", v, "PONG")
        c.cmd("SET", "k", "v1")
        v, _ = c.cmd("GET", "k")
        expect("AUTH post-auth data", v, b"v1")
        v, _ = c.cmd("AUTH", "a", "b", "c")
        check("AUTH too many args arity",
              isinstance(v, tuple) and "wrong number of arguments" in v[1],
              "got %r" % (v,))
        c.close()
        # gate state is per-connection
        c2 = Conn()
        v, _ = c2.cmd("PING")
        expect("AUTH gate is per-connection", v, ("ERR", "NOAUTH Authentication required."))
        v, _ = c2.cmd("AUTH", "default", pw)
        expect("AUTH default user form works", v, "OK")
        c2.close()

    print("----")
    print("checks: %d, failures: %d" % (CHECKS[0], len(FAILURES)))
    if FAILURES:
        print("FAILED: %s" % ", ".join(FAILURES))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
