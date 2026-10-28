#!/usr/bin/env python3
"""slaredis M0 acceptance client: raw-socket RESP2 driver.

No redis-py, no redis-cli: encodes commands as multibulk arrays,
parses replies, and keeps the raw reply bytes so checks can be
byte-exact. Run: python3 tests/resp_client.py [port]
Exits 0 when every check passes, 1 otherwise.
"""
import socket
import sys
import threading
import time

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 16399

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

    def send_raw(self, data):
        self.sock.sendall(data)

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
        """Returns (value, raw_bytes). Errors are ('ERR', text)."""
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
        self.send_raw(encode(*args))
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


def expect_err(name, got, prefix):
    ok = isinstance(got, tuple) and got[0] == "ERR" and got[1].startswith(prefix)
    check(name, ok, "got %r want error starting %r" % (got, prefix))


def main():
    c = Conn()

    # -- generic / string family --
    v, raw = c.cmd("PING")
    expect("PING raw bytes", raw, b"+PONG\r\n")
    v, _ = c.cmd("PING", "hello")
    expect("PING msg", v, b"hello")
    v, _ = c.cmd("ping")
    expect("PING lowercase", v, "PONG")
    v, _ = c.cmd("ECHO", "hey")
    expect("ECHO", v, b"hey")
    v, _ = c.cmd("SET", "a", "1")
    expect("SET", v, "OK")
    v, raw = c.cmd("GET", "a")
    expect("GET raw bytes", raw, b"$1\r\n1\r\n")
    v, raw = c.cmd("GET", "missing")
    expect("GET missing raw nil", raw, b"$-1\r\n")
    v, _ = c.cmd("TYPE", "a")
    expect("TYPE string", v, "string")
    v, _ = c.cmd("TYPE", "missing")
    expect("TYPE none", v, "none")
    v, _ = c.cmd("EXISTS", "a", "a", "missing")
    expect("EXISTS dup counts", v, 2)
    v, _ = c.cmd("DEL", "a", "missing")
    expect("DEL count", v, 1)
    v, _ = c.cmd("EXISTS", "a")
    expect("EXISTS after DEL", v, 0)

    # -- arity / unknown errors, Redis wire forms --
    v, _ = c.cmd("GET")
    expect_err("GET arity", v, "ERR wrong number of arguments for 'get' command")
    v, _ = c.cmd("NOSUCHCMD", "x")
    expect_err("unknown command", v, "ERR unknown command 'NOSUCHCMD'")
    v, _ = c.cmd("SET", "onlykey")
    expect_err("SET arity", v, "ERR wrong number of arguments for 'set' command")

    # -- INCR / DECR --
    v, _ = c.cmd("INCR", "ctr")
    expect("INCR fresh", v, 1)
    v, _ = c.cmd("INCR", "ctr")
    expect("INCR again", v, 2)
    v, _ = c.cmd("DECR", "ctr")
    expect("DECR", v, 1)
    v, _ = c.cmd("SET", "s10", "10")
    v, _ = c.cmd("INCR", "s10")
    expect("INCR on 10", v, 11)
    v, _ = c.cmd("SET", "sabc", "abc")
    v, _ = c.cmd("INCR", "sabc")
    expect_err("INCR on abc", v, "ERR value is not an integer or out of range")
    v, _ = c.cmd("SET", "smax", "9223372036854775807")
    v, _ = c.cmd("INCR", "smax")
    expect_err("INCR overflow", v, "ERR increment or decrement would overflow")
    v, _ = c.cmd("SET", "sfloat", "1.5")
    v, _ = c.cmd("INCR", "sfloat")
    expect_err("INCR on float text", v, "ERR value is not an integer or out of range")
    v, _ = c.cmd("SET", "slead", "007")
    v, _ = c.cmd("INCR", "slead")
    expect_err("INCR leading zeros rejected", v, "ERR value is not an integer or out of range")
    # INCR preserves an existing TTL (Redis semantics)
    v, _ = c.cmd("SET", "cttl", "5", "EX", "100")
    v, _ = c.cmd("INCR", "cttl")
    expect("INCR value", v, 6)
    v, _ = c.cmd("TTL", "cttl")
    check("INCR preserves TTL", 98 <= v <= 100, "ttl=%r" % v)

    # -- expiry: TTL/PTTL/PERSIST semantics --
    v, _ = c.cmd("TTL", "missing")
    expect("TTL missing = -2", v, -2)
    v, _ = c.cmd("SET", "plain", "x")
    v, _ = c.cmd("TTL", "plain")
    expect("TTL no-expire = -1", v, -1)
    v, _ = c.cmd("PERSIST", "plain")
    expect("PERSIST no-expire = 0", v, 0)
    v, _ = c.cmd("EXPIRE", "plain", "100")
    expect("EXPIRE set", v, 1)
    v, _ = c.cmd("TTL", "plain")
    check("TTL ~100", 98 <= v <= 100, "ttl=%r" % v)
    v, _ = c.cmd("PTTL", "plain")
    check("PTTL ~100000", 98000 <= v <= 100000, "pttl=%r" % v)
    v, _ = c.cmd("PERSIST", "plain")
    expect("PERSIST removes", v, 1)
    v, _ = c.cmd("TTL", "plain")
    expect("TTL after PERSIST = -1", v, -1)
    v, _ = c.cmd("EXPIRE", "missing", "100")
    expect("EXPIRE missing = 0", v, 0)
    v, _ = c.cmd("EXPIRE", "plain", "abc")
    expect_err("EXPIRE non-integer", v, "ERR value is not an integer or out of range")
    v, _ = c.cmd("EXPIRE", "plain", "0")
    expect("EXPIRE 0 deletes", v, 1)
    v, _ = c.cmd("EXISTS", "plain")
    expect("EXPIRE 0 gone", v, 0)
    v, _ = c.cmd("SET", "badexp", "v", "EX", "0")
    expect_err("SET EX 0", v, "ERR invalid expire time in 'set' command")
    v, _ = c.cmd("SET", "badexp2", "v", "EX", "abc")
    expect_err("SET EX abc", v, "ERR invalid expire time in 'set' command")
    v, _ = c.cmd("SET", "badexp3", "v", "EX", "10", "PX", "10")
    expect_err("SET EX+PX conflict", v, "ERR syntax error")
    v, _ = c.cmd("SET", "badexp4", "v", "WHAT", "10")
    expect_err("SET bad option", v, "ERR syntax error")
    # SET without options clears an old TTL
    v, _ = c.cmd("SET", "re", "1", "EX", "100")
    v, _ = c.cmd("SET", "re", "2")
    v, _ = c.cmd("TTL", "re")
    expect("SET overwrite clears TTL", v, -1)

    # -- expiry: lazy path (SET EX 1, alive, then gone) --
    v, _ = c.cmd("SET", "exp1", "v", "EX", "1")
    expect("SET EX 1", v, "OK")
    v, _ = c.cmd("GET", "exp1")
    expect("EX 1 alive immediately", v, b"v")
    time.sleep(1.4)
    v, _ = c.cmd("GET", "exp1")
    expect("EX 1 gone after 1.4s (lazy)", v, None)
    v, _ = c.cmd("TYPE", "exp1")
    expect("EX 1 type none after expiry", v, "none")

    # -- expiry: PX millisecond deadline --
    v, _ = c.cmd("SET", "exppx", "v", "PX", "400")
    expect("SET PX 400", v, "OK")
    v, _ = c.cmd("GET", "exppx")
    expect("PX alive immediately", v, b"v")
    time.sleep(0.7)
    v, _ = c.cmd("GET", "exppx")
    expect("PX gone after 0.7s", v, None)

    # -- hash family --
    v, _ = c.cmd("HSET", "h", "f1", "v1", "f2", "v2")
    expect("HSET two new", v, 2)
    v, _ = c.cmd("HSET", "h", "f1", "v1b", "f3", "v3")
    expect("HSET one new one update", v, 1)
    v, _ = c.cmd("HGET", "h", "f1")
    expect("HGET updated", v, b"v1b")
    v, _ = c.cmd("HGET", "h", "fnone")
    expect("HGET missing field", v, None)
    v, _ = c.cmd("HGETALL", "h")
    expect("HGETALL", v, [b"f1", b"v1b", b"f2", b"v2", b"f3", b"v3"])
    v, _ = c.cmd("HDEL", "h", "f2", "fnone")
    expect("HDEL count", v, 1)
    v, _ = c.cmd("TYPE", "h")
    expect("TYPE hash", v, "hash")
    v, _ = c.cmd("HSET", "h", "onlyone")
    expect_err("HSET odd args", v, "ERR wrong number of arguments for 'hset' command")
    v, _ = c.cmd("HDEL", "h", "f1", "f3")
    expect("HDEL rest", v, 2)
    v, _ = c.cmd("EXISTS", "h")
    expect("hash key deleted when emptied", v, 0)
    v, _ = c.cmd("HGETALL", "h")
    expect("HGETALL missing = empty array", v, [])

    # -- list family --
    v, _ = c.cmd("RPUSH", "l", "a", "b", "c")
    expect("RPUSH len", v, 3)
    v, _ = c.cmd("LPUSH", "l", "z", "y")
    expect("LPUSH len", v, 5)
    v, _ = c.cmd("LRANGE", "l", "0", "-1")
    expect("LRANGE all", v, [b"y", b"z", b"a", b"b", b"c"])
    v, _ = c.cmd("LRANGE", "l", "-2", "-1")
    expect("LRANGE negative", v, [b"b", b"c"])
    v, _ = c.cmd("LRANGE", "l", "5", "10")
    expect("LRANGE out of range", v, [])
    v, _ = c.cmd("LRANGE", "l", "x", "1")
    expect_err("LRANGE non-integer", v, "ERR value is not an integer or out of range")
    v, _ = c.cmd("LLEN", "l")
    expect("LLEN", v, 5)
    v, _ = c.cmd("LPOP", "l")
    expect("LPOP single", v, b"y")
    v, _ = c.cmd("RPOP", "l", "2")
    expect("RPOP count", v, [b"c", b"b"])
    v, _ = c.cmd("LPOP", "l", "0")
    expect("LPOP count 0 = empty array", v, [])
    v, _ = c.cmd("LPOP", "l", "-1")
    expect_err("LPOP negative count", v, "ERR value is out of range, must be positive")
    v, _ = c.cmd("LPOP", "missinglist")
    expect("LPOP missing = nil", v, None)
    v, _ = c.cmd("RPOP", "l")
    expect("RPOP single", v, b"a")
    v, _ = c.cmd("LPOP", "l")
    expect("LPOP last", v, b"z")
    v, _ = c.cmd("EXISTS", "l")
    expect("list key deleted when emptied", v, 0)
    v, _ = c.cmd("TYPE", "l")
    expect("TYPE after empty = none", v, "none")

    # -- set family --
    v, _ = c.cmd("SADD", "st", "a", "b", "a")
    expect("SADD dup in one call", v, 2)
    v, _ = c.cmd("SADD", "st", "b", "c")
    expect("SADD one new", v, 1)
    v, _ = c.cmd("SISMEMBER", "st", "b")
    expect("SISMEMBER yes", v, 1)
    v, _ = c.cmd("SISMEMBER", "st", "zz")
    expect("SISMEMBER no", v, 0)
    v, _ = c.cmd("SMEMBERS", "st")
    check("SMEMBERS content", sorted(v) == [b"a", b"b", b"c"], "got %r" % v)
    v, _ = c.cmd("SREM", "st", "a", "zz")
    expect("SREM count", v, 1)
    v, _ = c.cmd("TYPE", "st")
    expect("TYPE set", v, "set")
    v, _ = c.cmd("SREM", "st", "b", "c")
    expect("SREM rest", v, 2)
    v, _ = c.cmd("EXISTS", "st")
    expect("set key deleted when emptied", v, 0)
    v, _ = c.cmd("SMEMBERS", "st")
    expect("SMEMBERS missing = empty array", v, [])

    # -- zset family --
    v, _ = c.cmd("ZADD", "z", "2", "two", "1", "one", "2.5", "twohalf")
    expect("ZADD three new", v, 3)
    v, _ = c.cmd("ZADD", "z", "3", "two")
    expect("ZADD update not counted", v, 0)
    v, _ = c.cmd("ZSCORE", "z", "two")
    expect("ZSCORE updated", v, b"3")
    v, _ = c.cmd("ZSCORE", "z", "twohalf")
    expect("ZSCORE fraction format", v, b"2.5")
    v, _ = c.cmd("ZSCORE", "z", "ghost")
    expect("ZSCORE missing member", v, None)
    v, _ = c.cmd("ZRANGE", "z", "0", "-1")
    expect("ZRANGE order", v, [b"one", b"twohalf", b"two"])
    v, _ = c.cmd("ZRANGE", "z", "0", "-1", "WITHSCORES")
    expect("ZRANGE withscores", v, [b"one", b"1", b"twohalf", b"2.5", b"two", b"3"])
    v, _ = c.cmd("ZADD", "z", "1", "uno")
    expect("ZADD tie member", v, 1)
    v, _ = c.cmd("ZRANGE", "z", "0", "1")
    expect("ZRANGE tie lexicographic", v, [b"one", b"uno"])
    v, _ = c.cmd("ZADD", "z", "notafloat", "x")
    expect_err("ZADD bad score", v, "ERR value is not a valid float")
    v, _ = c.cmd("EXISTS", "x")
    expect("ZADD bad score applied nothing (no x key)", v, 0)
    v, _ = c.cmd("ZSCORE", "z", "x")
    expect("ZADD bad score added no member", v, None)
    v, _ = c.cmd("ZADD", "z", "nan", "bad")
    expect_err("ZADD nan rejected", v, "ERR value is not a valid float")
    v, _ = c.cmd("ZADD", "z", "inf", "top")
    expect("ZADD inf accepted", v, 1)
    v, _ = c.cmd("ZSCORE", "z", "top")
    expect("ZSCORE inf format", v, b"inf")
    v, _ = c.cmd("ZRANGE", "z", "-1", "-1")
    expect("ZRANGE last is inf member", v, [b"top"])
    v, _ = c.cmd("ZREM", "z", "top", "ghost")
    expect("ZREM count", v, 1)
    v, _ = c.cmd("TYPE", "z")
    expect("TYPE zset", v, "zset")
    v, _ = c.cmd("ZRANGE", "z", "0", "-1", "BOGUS")
    expect_err("ZRANGE bad option", v, "ERR syntax error")
    v, _ = c.cmd("ZADD", "zbig", "1e18", "huge", "5", "small")
    expect("ZADD extreme score", v, 2)
    v, _ = c.cmd("ZSCORE", "zbig", "huge")
    check("ZSCORE extreme round-trips", v is not None and float(v) == 1e18,
          "got %r" % v)
    v, _ = c.cmd("ZRANGE", "zbig", "0", "-1")
    expect("ZRANGE extreme order", v, [b"small", b"huge"])

    # -- WRONGTYPE matrix --
    c.cmd("SET", "wt_s", "v")
    c.cmd("RPUSH", "wt_l", "e")
    c.cmd("HSET", "wt_h", "f", "v")
    c.cmd("SADD", "wt_st", "m")
    c.cmd("ZADD", "wt_z", "1", "m")
    wt_cases = [
        ("GET on list", ("GET", "wt_l")),
        ("GET on hash", ("GET", "wt_h")),
        ("INCR on set", ("INCR", "wt_st")),
        ("LPUSH on string", ("LPUSH", "wt_s", "x")),
        ("LRANGE on zset", ("LRANGE", "wt_z", "0", "-1")),
        ("LLEN on string", ("LLEN", "wt_s")),
        ("HSET on list", ("HSET", "wt_l", "f", "v")),
        ("HGET on string", ("HGET", "wt_s", "f")),
        ("HGETALL on set", ("HGETALL", "wt_st")),
        ("SADD on hash", ("SADD", "wt_h", "m")),
        ("SMEMBERS on string", ("SMEMBERS", "wt_s")),
        ("SISMEMBER on list", ("SISMEMBER", "wt_l", "m")),
        ("ZADD on set", ("ZADD", "wt_st", "1", "m")),
        ("ZSCORE on string", ("ZSCORE", "wt_s", "m")),
        ("ZREM on hash", ("ZREM", "wt_h", "m")),
        ("LPOP on zset", ("LPOP", "wt_z")),
    ]
    for name, args in wt_cases:
        v, raw = c.cmd(*args)
        expect_err("WRONGTYPE: " + name, v,
                   "WRONGTYPE Operation against a key holding the wrong kind of value")
    v, raw = c.cmd("GET", "wt_l")
    check("WRONGTYPE raw bytes", raw.startswith(b"-WRONGTYPE ") and raw.endswith(b"\r\n"),
          "raw=%r" % raw)

    # -- inline commands + fragmented delivery --
    c.send_raw(b"PING\r\n")
    v, raw = c.read_reply()
    expect("inline PING", v, "PONG")
    c.send_raw(b"  set   inlinek   inlinev  \r\n")
    v, _ = c.read_reply()
    expect("inline SET padded spaces", v, "OK")
    c.send_raw(b"GET inlinek\r\n")
    v, _ = c.read_reply()
    expect("inline GET", v, b"inlinev")
    c.send_raw(b"*2\r\n$3\r\nGET\r\n$7\r\ninlin")
    time.sleep(0.2)
    c.send_raw(b"ek\r\n")
    v, _ = c.read_reply()
    expect("fragmented multibulk completes", v, b"inlinev")

    # -- pipelining: five commands in one write, five ordered replies --
    pipe = (encode("SET", "p1", "1") + encode("INCR", "p1") +
            encode("GET", "p1") + encode("PING") + encode("TYPE", "p1"))
    c.send_raw(pipe)
    got = [c.read_reply()[0] for _ in range(5)]
    expect("pipeline batch", got, ["OK", 2, b"2", "PONG", "string"])

    # -- protocol error: bad bulk length, error reply then close --
    c2 = Conn()
    c2.send_raw(b"*1\r\n$-5\r\n")
    v, _ = c2.read_reply()
    expect_err("protocol error reply", v, "ERR Protocol error")
    c2.sock.settimeout(3)
    try:
        tail = c2.sock.recv(64)
        check("protocol error closes connection", tail == b"", "tail=%r" % tail)
    except socket.timeout:
        check("protocol error closes connection", False, "no EOF within 3s")
    c2.close()

    c.close()

    # -- active sweep coexistence: expire passes while other traffic flows --
    c3 = Conn()
    c3.cmd("SET", "sweepme", "v", "PX", "300")
    t0 = time.time()
    n = 0
    while time.time() - t0 < 0.9:
        c3.cmd("PING")
        n += 1
    v, _ = c3.cmd("GET", "sweepme")
    expect("PX key gone after window with traffic flowing", v, None)
    check("traffic flowed during sweep window", n > 10, "pings=%d" % n)
    c3.close()

    # -- concurrency: parallel clients, private keys + shared counter --
    errors = []

    def worker(idx):
        try:
            w = Conn()
            key = "conc%d" % idx
            for i in range(200):
                v, _ = w.cmd("INCR", key)
                if v != i + 1:
                    errors.append("worker %d iter %d got %r" % (idx, i, v))
                    return
            for i in range(50):
                w.cmd("SET", "ck%d" % idx, "val%d" % i)
                v, _ = w.cmd("GET", "ck%d" % idx)
                if v != ("val%d" % i).encode():
                    errors.append("worker %d set/get mismatch %r" % (idx, v))
                    return
            w.close()
        except Exception as exc:  # noqa: BLE001
            errors.append("worker %d exception %r" % (idx, exc))

    def hammer():
        try:
            w = Conn()
            for _ in range(250):
                w.cmd("INCR", "shared")
            w.close()
        except Exception as exc:  # noqa: BLE001
            errors.append("hammer exception %r" % exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    threads += [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("concurrent workers clean", errors == [], "; ".join(errors[:3]))
    c4 = Conn()
    v, _ = c4.cmd("GET", "shared")
    expect("shared INCR total = 4*250", v, b"1000")
    for i in range(8):
        v, _ = c4.cmd("GET", "conc%d" % i)
        if v != b"200":
            check("private counter %d" % i, False, "got %r" % v)
    check("private counters all 200", True)
    c4.close()

    print("----")
    print("checks: %d, failures: %d" % (CHECKS[0], len(FAILURES)))
    if FAILURES:
        print("failed: %s" % ", ".join(FAILURES))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
