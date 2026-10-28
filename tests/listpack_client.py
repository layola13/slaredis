#!/usr/bin/env python3
"""slaredis M1 listpack acceptance client.

Protocol-level round-trip proof for the compact representation:
small hash / set / zset keys must report OBJECT ENCODING
"listpack", read back byte-exact through every integer-encoding
width and both string encodings, and convert one-way to the plain
representation (hashtable / skiplist) exactly at the documented
thresholds (129th logical element, or an element over 64 bytes) --
with the full contents intact after conversion.

Self-contained raw-socket RESP2 driver (same shape as
tests/resp_client.py). Run: python3 tests/listpack_client.py [port]
Exits 0 when every check passes, 1 otherwise.
"""
import socket
import sys

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 16401

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


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("ok   %s" % name)
    else:
        print("FAIL %s  %s" % (name, detail))
        FAILURES.append(name)


def expect(name, got, want):
    check(name, got == want, "got %r want %r" % (got, want))


def pairs(reply):
    return {reply[i]: reply[i + 1] for i in range(0, len(reply), 2)}


def main():
    c = Conn()

    # -- representation observability basics --
    expect("LP object missing key is nil", c.cmd("OBJECT", "ENCODING", "nope"), None)
    c.cmd("SET", "s1", "v")
    expect("LP string encoding raw", c.cmd("OBJECT", "ENCODING", "s1"), b"raw")
    c.cmd("RPUSH", "l1", "a")
    expect("LP list encoding linkedlist", c.cmd("OBJECT", "ENCODING", "l1"), b"linkedlist")
    v = c.cmd("OBJECT", "FOO", "s1")
    check("LP object bad subcommand errors",
          isinstance(v, tuple) and v[0] == "ERR" and "unsupported OBJECT subcommand" in v[1],
          "got %r" % (v,))
    v = c.cmd("OBJECT", "ENCODING")
    check("LP object arity errors",
          isinstance(v, tuple) and v[0] == "ERR" and "wrong number of arguments" in v[1],
          "got %r" % (v,))

    # -- small hash: packed, exact round-trip --
    c.cmd("DEL", "h1")
    c.cmd("HSET", "h1", "a", "1", "b", "two", "c", "3.5")
    expect("LP small hash is listpack", c.cmd("OBJECT", "ENCODING", "h1"), b"listpack")
    expect("LP small hash round-trip", pairs(c.cmd("HGETALL", "h1")),
           {b"a": b"1", b"b": b"two", b"c": b"3.5"})

    # -- integer-encoding stress: every int width + string traps --
    stress = {
        "v0": "0", "v127": "127", "v128": "128", "vm1": "-1",
        "vm4096": "-4096", "v4095": "4095", "v4096": "4096",
        "v32767": "32767", "vm32768": "-32768",
        "v8388607": "8388607", "vm8388608": "-8388608",
        "v2^31m1": "2147483647", "vm2^31": "-2147483648",
        "vmax": "9223372036854775807", "vmin": "-9223372036854775808",
        "lead": "007", "negzero": "-0", "frac": "3.14",
        "s63": "x" * 63, "s64": "y" * 64,
    }
    args = ["HSET", "hstress"]
    for k, val in stress.items():
        args += [k, val]
    c.cmd(*args)
    expect("LP stress hash stays listpack", c.cmd("OBJECT", "ENCODING", "hstress"), b"listpack")
    got = pairs(c.cmd("HGETALL", "hstress"))
    want = {k.encode(): v.encode() for k, v in stress.items()}
    expect("LP stress values byte-exact", got, want)
    expect("LP stress single read", c.cmd("HGET", "hstress", "vmax"), b"9223372036854775807")
    expect("LP stress string trap 007", c.cmd("HGET", "hstress", "lead"), b"007")

    # -- packed replace + delete splices --
    c.cmd("HSET", "h1", "b", "TWO-two-TWO")
    expect("LP packed replace value", c.cmd("HGET", "h1", "b"), b"TWO-two-TWO")
    expect("LP packed replace keeps pack", c.cmd("OBJECT", "ENCODING", "h1"), b"listpack")
    c.cmd("HSET", "h1", "b", "z")
    expect("LP packed replace shorter", c.cmd("HGET", "h1", "b"), b"z")
    expect("LP packed hdel count", c.cmd("HDEL", "h1", "a", "nosuch"), 1)
    expect("LP packed hdel round-trip", pairs(c.cmd("HGETALL", "h1")),
           {b"b": b"z", b"c": b"3.5"})
    expect("LP packed hdel to empty deletes key", c.cmd("HDEL", "h1", "b", "c"), 2)
    expect("LP packed emptied key type", c.cmd("TYPE", "h1"), "none")

    # -- hash element-count conversion at 129 --
    c.cmd("DEL", "hbig")
    args = ["HSET", "hbig"]
    for i in range(128):
        args += ["f%03d" % i, "val%d" % i]
    c.cmd(*args)
    expect("LP hash at 128 still listpack", c.cmd("OBJECT", "ENCODING", "hbig"), b"listpack")
    c.cmd("HSET", "hbig", "f128", "val128")
    expect("LP hash at 129 converts", c.cmd("OBJECT", "ENCODING", "hbig"), b"hashtable")
    got = pairs(c.cmd("HGETALL", "hbig"))
    want = {("f%03d" % i).encode(): ("val%d" % i).encode() for i in range(129)}
    expect("LP hash contents intact after conversion", got, want)
    # one-way: shrinking back stays plain
    for i in range(127):
        c.cmd("HDEL", "hbig", "f%03d" % i)
    expect("LP hash conversion is one-way", c.cmd("OBJECT", "ENCODING", "hbig"), b"hashtable")

    # -- hash element-size conversion at 65 bytes --
    c.cmd("DEL", "hsize")
    c.cmd("HSET", "hsize", "small", "v")
    expect("LP hash pre-size listpack", c.cmd("OBJECT", "ENCODING", "hsize"), b"listpack")
    big = "B" * 65
    c.cmd("HSET", "hsize", "bigfield", big)
    expect("LP hash 65B value converts", c.cmd("OBJECT", "ENCODING", "hsize"), b"hashtable")
    expect("LP hash big value exact", c.cmd("HGET", "hsize", "bigfield"), big.encode())
    c.cmd("DEL", "hsize2")
    c.cmd("HSET", "hsize2", "f", "v")
    c.cmd("HSET", "hsize2", "F" * 65, "v2")
    expect("LP hash 65B field converts", c.cmd("OBJECT", "ENCODING", "hsize2"), b"hashtable")

    # -- set: packed round-trip + conversions --
    c.cmd("DEL", "set1")
    c.cmd("SADD", "set1", "alpha", "42", "007", "beta")
    expect("LP small set is listpack", c.cmd("OBJECT", "ENCODING", "set1"), b"listpack")
    expect("LP set round-trip", set(c.cmd("SMEMBERS", "set1")),
           {b"alpha", b"42", b"007", b"beta"})
    expect("LP set sismember packed", c.cmd("SISMEMBER", "set1", "42"), 1)
    expect("LP set srem packed", c.cmd("SREM", "set1", "007", "ghost"), 1)
    expect("LP set after srem", set(c.cmd("SMEMBERS", "set1")), {b"alpha", b"42", b"beta"})
    c.cmd("DEL", "setbig")
    args = ["SADD", "setbig"] + ["m%03d" % i for i in range(128)]
    c.cmd(*args)
    expect("LP set at 128 still listpack", c.cmd("OBJECT", "ENCODING", "setbig"), b"listpack")
    c.cmd("SADD", "setbig", "m128")
    expect("LP set at 129 converts", c.cmd("OBJECT", "ENCODING", "setbig"), b"hashtable")
    expect("LP set contents intact after conversion", set(c.cmd("SMEMBERS", "setbig")),
           {("m%03d" % i).encode() for i in range(129)})
    c.cmd("DEL", "setsize")
    c.cmd("SADD", "setsize", "tiny")
    c.cmd("SADD", "setsize", "M" * 65)
    expect("LP set 65B member converts", c.cmd("OBJECT", "ENCODING", "setsize"), b"hashtable")

    # -- zset: packed round-trip, ordering, update, conversion --
    c.cmd("DEL", "z1")
    c.cmd("ZADD", "z1", "1.5", "one", "-2.25", "two", "100", "three", "0", "zero")
    expect("LP small zset is listpack", c.cmd("OBJECT", "ENCODING", "z1"), b"listpack")
    expect("LP zset zrange order", c.cmd("ZRANGE", "z1", "0", "-1"),
           [b"two", b"zero", b"one", b"three"])
    expect("LP zset zrange withscores", c.cmd("ZRANGE", "z1", "0", "-1", "WITHSCORES"),
           [b"two", b"-2.25", b"zero", b"0", b"one", b"1.5", b"three", b"100"])
    expect("LP zset zscore", c.cmd("ZSCORE", "z1", "one"), b"1.5")
    c.cmd("ZADD", "z1", "-100", "three")
    expect("LP zset packed score update", c.cmd("ZSCORE", "z1", "three"), b"-100")
    expect("LP zset order after update", c.cmd("ZRANGE", "z1", "0", "0"), [b"three"])
    expect("LP zset zrem packed", c.cmd("ZREM", "z1", "three", "ghost"), 1)
    expect("LP zset after zrem", c.cmd("ZRANGE", "z1", "0", "-1"),
           [b"two", b"zero", b"one"])
    c.cmd("ZADD", "z1", "inf", "top")
    expect("LP zset inf score", c.cmd("ZSCORE", "z1", "top"), b"inf")
    c.cmd("ZADD", "z1", "1e20", "huge")
    got = c.cmd("ZSCORE", "z1", "huge")
    check("LP zset extreme score round-trips",
          got is not None and abs(float(got) - 1e20) / 1e20 < 1e-9,
          "got %r" % (got,))
    c.cmd("DEL", "zbig")
    args = ["ZADD", "zbig"]
    for i in range(128):
        args += [str(i), "zm%03d" % i]
    c.cmd(*args)
    expect("LP zset at 128 still listpack", c.cmd("OBJECT", "ENCODING", "zbig"), b"listpack")
    c.cmd("ZADD", "zbig", "128", "zm128")
    expect("LP zset at 129 converts", c.cmd("OBJECT", "ENCODING", "zbig"), b"skiplist")
    expect("LP zset contents intact after conversion",
           c.cmd("ZRANGE", "zbig", "0", "-1"),
           [("zm%03d" % i).encode() for i in range(129)])
    expect("LP zset scores intact after conversion",
           c.cmd("ZSCORE", "zbig", "zm077"), b"77")

    c.close()
    print("----")
    print("checks: %d, failures: %d" % (CHECKS[0], len(FAILURES)))
    if FAILURES:
        print("FAILED: %s" % ", ".join(FAILURES))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
