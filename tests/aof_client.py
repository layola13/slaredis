#!/usr/bin/env python3
"""slaredis M1 AOF acceptance driver.

Modes (orchestrated by tests/run_aof_test.sh):
  write1 <port>   build the phase-1 dataset, print canonical dump
  rewrite <port>  BGREWRITEAOF, then post-rewrite writes, dump
  dump <port>     print the canonical dump of the known key set
  setnk <port>    SET nk nv (fsync=no smoke)
  checknk <port>  assert GET nk == nv

The canonical dump is one line per known key:
  key|type|payload|ttlflag
with aggregate payloads in sorted order, so dumps taken before
and after a restart (AOF replay) must be byte-identical.
"""
import socket
import sys
import time

HOST = "127.0.0.1"


def encode(*args):
    out = [b"*%d\r\n" % len(args)]
    for a in args:
        if isinstance(a, int):
            a = str(a)
        if isinstance(a, str):
            a = a.encode()
        out.append(b"$%d\r\n%s\r\n" % (len(a), a))
    return b"".join(out)


class Conn:
    def __init__(self, port):
        self.sock = socket.create_connection((HOST, port), timeout=10)
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
        t, rest = line[:1], line[1:]
        if t == b"+":
            return rest.decode()
        if t == b"-":
            return ("ERR", rest.decode())
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n == -1:
                return None
            data = self._readn(n + 2)
            return data[:n]
        if t == b"*":
            n = int(rest)
            if n == -1:
                return None
            return [self.read_reply() for _ in range(n)]
        raise ValueError("bad reply type byte: %r" % line)

    def cmd(self, *args):
        self.sock.sendall(encode(*args))
        return self.read_reply()


KEYS = ["str1", "ctr", "temp", "future", "h1", "hbig", "l1", "s1",
        "z1", "doomed", "churn", "post1"]


def dump(c):
    lines = []
    for k in KEYS:
        t = c.cmd("TYPE", k)
        if t == "none":
            lines.append("%s|none||-" % k)
            continue
        if t == "string":
            payload = c.cmd("GET", k).decode()
        elif t == "hash":
            r = c.cmd("HGETALL", k)
            prs = sorted((r[i].decode(), r[i + 1].decode())
                         for i in range(0, len(r), 2))
            payload = ",".join("%s=%s" % p for p in prs)
        elif t == "list":
            payload = ",".join(x.decode() for x in c.cmd("LRANGE", k, 0, -1))
        elif t == "set":
            payload = ",".join(sorted(x.decode() for x in c.cmd("SMEMBERS", k)))
        elif t == "zset":
            r = c.cmd("ZRANGE", k, 0, -1, "WITHSCORES")
            payload = ",".join("%s:%s" % (r[i].decode(), r[i + 1].decode())
                               for i in range(0, len(r), 2))
        else:
            payload = "?"
        pttl = c.cmd("PTTL", k)
        ttl = "TTL" if pttl > 0 else str(pttl)
        lines.append("%s|%s|%s|%s" % (k, t, payload, ttl))
    return "\n".join(lines) + "\n"


def need(cond, msg):
    if not cond:
        print("AOF DRIVER FAILURE: %s" % msg)
        sys.exit(1)


def main():
    mode = sys.argv[1]
    port = int(sys.argv[2])
    c = Conn(port)
    if mode == "write1":
        need(c.cmd("SET", "str1", "hello") == "OK", "set str1")
        for _ in range(5):
            c.cmd("INCR", "ctr")
        need(c.cmd("SET", "temp", "gone", "PX", "300") == "OK", "set temp")
        future_ms = int(time.time() * 1000) + 3600000
        need(c.cmd("SET", "future", "stays") == "OK", "set future")
        need(c.cmd("PEXPIREAT", "future", str(future_ms)) == 1, "pexpireat future")
        need(c.cmd("HSET", "h1", "a", "1", "b", "two", "c", "3.5") == 3, "hset h1")
        args = ["HSET", "hbig"]
        for i in range(130):
            args += ["f%03d" % i, "val%d" % i]
        c.cmd(*args)
        need(c.cmd("RPUSH", "l1", "a", "b", "c") == 3, "rpush l1")
        need(c.cmd("LPOP", "l1") == b"a", "lpop l1")
        need(c.cmd("SADD", "s1", "x", "y", "z") == 3, "sadd s1")
        need(c.cmd("SREM", "s1", "y") == 1, "srem s1")
        need(c.cmd("ZADD", "z1", "1.5", "a", "2.5", "b") == 2, "zadd z1")
        need(c.cmd("SET", "doomed", "v") == "OK", "set doomed")
        need(c.cmd("DEL", "doomed") == 1, "del doomed")
        need(c.cmd("EXPIRE", "str1", "100000") == 1, "expire str1 (absolutized in AOF)")
        need(c.cmd("PERSIST", "str1") == 1, "persist str1")
        for i in range(200):
            c.cmd("SET", "churn", "v%d" % i)
        sys.stdout.write(dump(c))
    elif mode == "rewrite":
        v = c.cmd("BGREWRITEAOF")
        need(v == "AOF rewrite completed", "bgrewriteaof reply %r" % (v,))
        need(c.cmd("SET", "post1", "after-rewrite") == "OK", "set post1")
        need(c.cmd("INCR", "ctr") == 6, "incr ctr after rewrite")
        sys.stdout.write(dump(c))
    elif mode == "dump":
        sys.stdout.write(dump(c))
    elif mode == "setnk":
        need(c.cmd("SET", "nk", "nv") == "OK", "set nk")
    elif mode == "checknk":
        v = c.cmd("GET", "nk")
        need(v == b"nv", "get nk got %r" % (v,))
        print("ok   AOF fsync=no written data survives kill+restart")
    else:
        print("unknown mode %s" % mode)
        return 1
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
