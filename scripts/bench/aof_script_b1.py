#!/usr/bin/env python3
# aof_script_b1.py -- fixed operation script -> BGREWRITEAOF ->
# print sha256 + size of the rewritten AOF. Run against the base
# (e72e1f2) and B1 binaries with identical arguments; the hashes
# must match byte-for-byte (acceptance item 3: the B1 index layer
# must not change the entries order, hence not the rewrite bytes).
#
# The script deliberately exercises every path whose slot choice
# could leak into the serialization order:
#   - plain inserts (append order)
#   - DEL tombstones followed by inserts (smallest-slot reuse)
#   - lazy expiry (past PEXPIREAT + GET) followed by inserts
#   - mass delete past the compaction threshold (dead >= 64 and
#     >= half) so a sweep compaction renumbers the table
#   - all five kinds, packed aggregates and a >128-field hash
#     (plain conversion), a list with head-offset churn
# Deterministic: all expiries are absolute (PEXPIREAT), no
# wall-clock-relative TTLs in the serialized state.
#
# Usage: aof_script_b1.py <binary> <port> <aofpath>
import hashlib, os, socket, subprocess, sys, time

BIN, PORT, AOF = sys.argv[1], int(sys.argv[2]), sys.argv[3]

def enc(*args):
    out = [b"*%d\r\n" % len(args)]
    for a in args:
        if isinstance(a, str):
            a = a.encode()
        out.append(b"$%d\r\n%s\r\n" % (len(a), a))
    return b"".join(out)

class Client:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", PORT))
        self.buf = b""
    def _readline(self):
        while b"\r\n" not in self.buf:
            d = self.s.recv(65536)
            if not d:
                raise RuntimeError("eof")
            self.buf += d
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line
    def _readn(self, n):
        while len(self.buf) < n:
            d = self.s.recv(262144)
            if not d:
                raise RuntimeError("eof")
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out
    def reply(self):
        line = self._readline()
        t, rest = line[:1], line[1:]
        if t in b"+-":
            return rest
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n == -1:
                return None
            data = self._readn(n)
            self._readn(2)
            return data
        if t == b"*":
            n = int(rest)
            if n == -1:
                return None
            return [self.reply() for _ in range(n)]
        raise RuntimeError("bad reply: %r" % line)
    def cmd(self, *args):
        self.s.sendall(enc(*args))
        return self.reply()

def main():
    if os.path.exists(AOF):
        os.unlink(AOF)
    env = dict(os.environ)
    env["SLAREDIS_PORT"] = str(PORT)
    env["SLAREDIS_HOST"] = "127.0.0.1"
    env["SLAREDIS_AOF_PATH"] = AOF
    env["SLAREDIS_AOF_FSYNC"] = "always"
    p = subprocess.Popen([BIN], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                s = socket.create_connection(("127.0.0.1", PORT), timeout=1)
                s.close()
                break
            except OSError:
                time.sleep(0.1)
        c = Client()
        # phase 1: 300 plain string inserts
        for i in range(300):
            assert c.cmd("SET", f"k{i:04d}", f"v{i}") == b"OK"
        # phase 2: tombstone every third key, then insert fresh
        # keys (reuse order decides their serialization slots)
        for i in range(0, 300, 3):
            assert c.cmd("DEL", f"k{i:04d}") == 1
        for i in range(120):
            assert c.cmd("SET", f"r{i:04d}", f"w{i}") == b"OK"
        # phase 3: overwrite some live keys in place (SET on an
        # existing key must not move it), delete some r-keys
        for i in range(0, 120, 2):
            assert c.cmd("SET", f"r{i:04d}", f"W{i}") == b"OK"
        for i in range(1, 120, 4):
            assert c.cmd("DEL", f"r{i:04d}") == 1
        # phase 4: lazy expiry: past deadline + GET tombstones the
        # entry; a following insert reuses the freed slot
        for i in range(20):
            assert c.cmd("SET", f"x{i:02d}", "tmp") == b"OK"
            assert c.cmd("PEXPIREAT", f"x{i:02d}", "1000000000000") == 1
            assert c.cmd("GET", f"x{i:02d}") is None
            assert c.cmd("SET", f"y{i:02d}", f"keep{i}") == b"OK"
        # phase 5: aggregates of every kind
        for i in range(30):
            assert c.cmd("HSET", f"h{i:02d}", "f1", "a", "f2", "b") == 2
            assert c.cmd("SADD", f"s{i:02d}", "m1", "m2", "m3") == 3
            assert c.cmd("ZADD", f"z{i:02d}", "1.5", "za", "2.5", "zb") == 2
            assert c.cmd("LPUSH", f"l{i:02d}", "e1", "e2") == 2
            assert c.cmd("RPUSH", f"l{i:02d}", "e3") == 3
            assert c.cmd("LPOP", f"l{i:02d}") == b"e2"
        # big hash converts packed -> plain past 128 fields
        for i in range(200):
            c.cmd("HSET", "bighash", f"bf{i:03d}", f"bv{i}")
        # a key with a far-future absolute expiry survives into
        # the rewrite as PEXPIREAT with the same deadline
        assert c.cmd("SET", "future", "stays") == b"OK"
        assert c.cmd("PEXPIREAT", "future", "4102444800000") == 1
        # phase 6: mass delete to force sweep compaction
        # (dead >= 64 and >= half of the table), then rebuild
        for i in range(300):
            c.cmd("DEL", f"k{i:04d}")
        for i in range(120):
            c.cmd("DEL", f"r{i:04d}")
        time.sleep(1.5)  # let at least one sweep tick compact
        for i in range(50):
            assert c.cmd("SET", f"post{i:03d}", f"p{i}") == b"OK"
        # sanity: dataset shape as expected
        assert c.cmd("GET", "post000") == b"p0"
        assert c.cmd("GET", "y00") == b"keep0"
        assert c.cmd("GET", "future") == b"stays"
        r = c.cmd("BGREWRITEAOF")
        assert r == b"AOF rewrite completed", r
        time.sleep(0.5)
    finally:
        p.kill(); p.wait()
    data = open(AOF, "rb").read()
    print(f"aof_bytes={len(data)} sha256={hashlib.sha256(data).hexdigest()}")
    print("DONE")

if __name__ == "__main__":
    main()
