#!/usr/bin/env python3
# drive_b1.py -- defect-B acceptance driver for the B1 key index.
#
# Same caliber as the triage curve (~/workspace/slaredis-triage/
# drive.py phase_curve): fresh server, P=1 request-response,
# 16-byte values, sequential keys c<i>, load windows of 5000.
# Extended to N=100000 and with per-op latency sampling at the
# final N. Reports per-window load rate + RSS, then GET hit /
# miss latency (mean + p50) at N=100000.
#
# Usage: drive_b1.py <binary> <port> [nkeys]
import os, socket, subprocess, sys, time

BIN = sys.argv[1]
PORT = int(sys.argv[2])
NKEYS = int(sys.argv[3]) if len(sys.argv) > 3 else 100000
WIN = 5000

def rss_kb(pid):
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return -1

def start_server():
    env = dict(os.environ)
    env["SLAREDIS_PORT"] = str(PORT)
    env["SLAREDIS_HOST"] = "127.0.0.1"
    env.pop("SLAREDIS_AOF_PATH", None)
    p = subprocess.Popen([BIN], env=env, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            s = socket.create_connection(("127.0.0.1", PORT), timeout=1)
            s.close()
            return p
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("server did not start")

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
        raise RuntimeError("bad reply: %r" % line)
    def cmd(self, *args):
        self.s.sendall(enc(*args))
        return self.reply()

def main():
    srv = start_server()
    rss0 = rss_kb(srv.pid)
    c = Client()
    val = b"x" * 16
    total = 0
    print(f"[curve] bin={BIN} start rss={rss0} kB", flush=True)
    while total < NKEYS:
        t0 = time.time()
        for i in range(WIN):
            assert c.cmd("SET", f"c{total + i}", val) == b"OK"
        dt = time.time() - t0
        total += WIN
        print(f"[curve] loaded_to={total} window_rate={WIN/dt:.0f} ops/s "
              f"rss={rss_kb(srv.pid)} kB", flush=True)
    # GET hit latency at final N (strided sample over the keyspace)
    lat = []
    for i in range(1000):
        k = f"c{(i * 9973) % NKEYS}"
        t0 = time.perf_counter()
        r = c.cmd("GET", k)
        lat.append(time.perf_counter() - t0)
        assert r == val
    lat.sort()
    hit_mean = sum(lat) / len(lat) * 1000
    hit_p50 = lat[len(lat) // 2] * 1000
    # GET miss latency at final N
    lat = []
    for i in range(1000):
        t0 = time.perf_counter()
        r = c.cmd("GET", f"nope{i}")
        lat.append(time.perf_counter() - t0)
        assert r is None
    lat.sort()
    miss_mean = sum(lat) / len(lat) * 1000
    miss_p50 = lat[len(lat) // 2] * 1000
    rss1 = rss_kb(srv.pid)
    print(f"[lat] N={NKEYS} GET hit mean={hit_mean:.3f} ms p50={hit_p50:.3f} ms")
    print(f"[lat] N={NKEYS} GET miss mean={miss_mean:.3f} ms p50={miss_p50:.3f} ms")
    print(f"[rss] start={rss0} kB end={rss1} kB delta_per_key="
          f"{(rss1 - rss0) * 1024 / NKEYS:.1f} B")
    srv.kill(); srv.wait()
    print("DONE")

if __name__ == "__main__":
    main()
