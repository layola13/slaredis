import socket, time, sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 16782
def conn():
    return socket.create_connection(('127.0.0.1', PORT), 5)
def cmd(s, *args):
    out = b'*%d\r\n' % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b'$%d\r\n%s\r\n' % (len(a), a)
    s.sendall(out)
def readline(s, buf):
    while b'\r\n' not in buf:
        chunk = s.recv(4096)
        if not chunk: raise IOError('eof')
        buf += chunk
    line, buf = buf.split(b'\r\n', 1)
    return line, buf
def reply(s, buf=b''):
    line, buf = readline(s, buf)
    t, rest = line[:1], line[1:]
    if t in b'+-:': return rest, buf
    if t == b'$':
        n = int(rest)
        if n == -1: return None, buf
        while len(buf) < n + 2:
            buf += s.recv(4096)
        return buf[:n], buf[n+2:]
    raise ValueError('bad reply %r' % line)

fails = []
def check(name, cond, detail=''):
    print(('ok   ' if cond else 'FAIL ') + name + ('' if cond else '  ' + str(detail)))
    if not cond: fails.append(name)

# 200 concurrent connections: wait-set chunking (POLL_CHUNK=128)
conns = [conn() for _ in range(200)]
time.sleep(0.3)
for idx in (0, 1, 126, 127, 128, 150, 199):
    cmd(conns[idx], 'PING')
    r, _ = reply(conns[idx])
    check('poll chunked wait: PING on conn %d' % idx, r == b'PONG', r)
cmd(conns[150], 'SET', 'chunked', 'yes')
r, _ = reply(conns[150]); check('poll chunked wait: SET on conn 150', r == b'OK', r)
cmd(conns[3], 'GET', 'chunked')
r, _ = reply(conns[3]); check('poll chunked wait: GET via conn 3', r == b'yes', r)

# expiry sweep must fire while the loop idles in poll
s0 = conns[0]
cmd(s0, 'SET', 'ttlkey', 'x', 'PX', '300')
r, _ = reply(s0); check('poll idle sweep: SET PX 300', r == b'OK', r)
time.sleep(0.6)
cmd(s0, 'GET', 'ttlkey')
r, _ = reply(s0); check('poll idle sweep: key expired while idle', r is None, r)

# mass close -> reap compaction era; service must stay correct
for c in conns[10:]:
    c.close()
time.sleep(0.5)
fresh = conn()
cmd(fresh, 'PING'); r, _ = reply(fresh)
check('poll post-close: fresh conn PING', r == b'PONG', r)
cmd(fresh, 'GET', 'chunked'); r, _ = reply(fresh)
check('poll post-close: data intact', r == b'yes', r)
fresh.close()
for c in conns[:10]:
    c.close()
print('FAILURES: %d' % len(fails))
sys.exit(1 if fails else 0)
