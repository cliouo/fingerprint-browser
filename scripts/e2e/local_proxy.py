"""A tiny local proxy with authentication, for testing fpb's proxy path.

  python local_proxy.py http   --port 18080 --user u --password-env PROXY_PASS
  python local_proxy.py socks5 --port 18081 --user u --password-env PROXY_PASS

HTTP mode: CONNECT and absolute-URI requests, Basic Proxy-Authorization
(407 without it). SOCKS5 mode: RFC 1929 user/password auth, CONNECT only.
With --upstream HOST:PORT every outgoing connection is tunnelled through that
HTTP proxy (CONNECT), e.g. a machine whose internet access is via a local
system proxy. Logs one line per connection (target and whether auth passed), never the
password. Listens on 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ipaddress
import os
import struct
import sys

STATS = {"ok": 0, "denied": 0, "targets": {}}


def log(msg):
    print(msg, flush=True)


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


UPSTREAM = None  # (host, port) of an HTTP proxy to chain through


def _is_private(host):
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host == "localhost"
    return ip.is_private or ip.is_loopback


async def _open(host, port):
    # Private/LAN targets (a local mock backend) are reached directly: the
    # upstream (a system proxy) cannot be assumed to route back to this machine.
    if not UPSTREAM or _is_private(host):
        return await asyncio.open_connection(host, port)
    ur, uw = await asyncio.open_connection(*UPSTREAM)
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    uw.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await uw.drain()
    head = await ur.readuntil(b"\r\n\r\n")
    if b" 200" not in head.split(b"\r\n", 1)[0]:
        uw.close()
        raise ConnectionError(f"upstream refused CONNECT {target}: {head.splitlines()[0]!r}")
    return ur, uw


async def relay(cr, cw, host, port, initial=b""):
    try:
        ur, uw = await asyncio.wait_for(_open(host, port), 20)
    except Exception as exc:
        log(f"upstream {host}:{port} failed: {exc}")
        return None
    if initial:
        uw.write(initial)
        await uw.drain()
    return ur, uw


def count(target):
    STATS["targets"][target] = STATS["targets"].get(target, 0) + 1


class HttpProxy:
    def __init__(self, user, password):
        self.expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    async def handle(self, cr, cw):
        try:
            head = await cr.readuntil(b"\r\n\r\n")
        except Exception:
            cw.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if headers.get("proxy-authorization") != self.expected:
            STATS["denied"] += 1
            log(f"407 {method} {target} (auth {'missing' if 'proxy-authorization' not in headers else 'wrong'})")
            cw.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm=\"fpb-test\"\r\n"
                     b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            await cw.drain()
            cw.close()
            return
        STATS["ok"] += 1
        if method == "CONNECT":
            host, _, port = target.rpartition(":")
            count(f"CONNECT {target}")
            log(f"CONNECT {target} auth ok")
            up = await relay(cr, cw, host.strip("[]"), int(port))
            if not up:
                cw.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                cw.close()
                return
            cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await cw.drain()
        else:
            from urllib.parse import urlsplit

            u = urlsplit(target)
            host, port = u.hostname, u.port or 80
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            count(f"{method} {host}")
            log(f"{method} {host}{path[:60]} auth ok")
            kept = [l for l in lines[1:] if l and not l.lower().startswith(("proxy-authorization", "proxy-connection"))]
            req = f"{method} {path} {version}\r\n" + "\r\n".join(kept) + "\r\n\r\n"
            up = await relay(cr, cw, host, port, req.encode("latin-1"))
            if not up:
                cw.close()
                return
        ur, uw = up
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw))


class Socks5Proxy:
    def __init__(self, user, password):
        self.user, self.password = user.encode(), password.encode()

    async def handle(self, cr, cw):
        try:
            ver, n = await cr.readexactly(2)
            methods = await cr.readexactly(n)
            if ver != 5 or 2 not in methods:
                STATS["denied"] += 1
                log(f"socks5: client offered methods {list(methods)} (no user/pass) -> refused")
                cw.write(b"\x05\xff")
                await cw.drain()
                cw.close()
                return
            cw.write(b"\x05\x02")
            await cw.drain()
            _v, ulen = await cr.readexactly(2)
            user = await cr.readexactly(ulen)
            (plen,) = await cr.readexactly(1)
            password = await cr.readexactly(plen)
            if user != self.user or password != self.password:
                STATS["denied"] += 1
                log("socks5: auth wrong")
                cw.write(b"\x01\x01")
                await cw.drain()
                cw.close()
                return
            cw.write(b"\x01\x00")
            await cw.drain()
            _v, cmd, _rsv, atyp = await cr.readexactly(4)
            if atyp == 1:
                host = str(ipaddress.IPv4Address(await cr.readexactly(4)))
            elif atyp == 3:
                (ln,) = await cr.readexactly(1)
                host = (await cr.readexactly(ln)).decode()
            elif atyp == 4:
                host = str(ipaddress.IPv6Address(await cr.readexactly(16)))
            else:
                cw.close()
                return
            (port,) = struct.unpack(">H", await cr.readexactly(2))
            if cmd != 1:
                cw.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
                cw.close()
                return
            STATS["ok"] += 1
            count(f"SOCKS {host}:{port}")
            log(f"socks5 CONNECT {host}:{port} auth ok")
            up = await relay(cr, cw, host, port)
            if not up:
                cw.write(b"\x05\x05\x00\x01" + b"\x00" * 6)
                cw.close()
                return
            cw.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await cw.drain()
            ur, uw = up
            await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
        except (asyncio.IncompleteReadError, ConnectionError):
            cw.close()


async def amain(args):
    password = os.environ.get(args.password_env)
    if not password:
        sys.exit(f"set ${args.password_env}")
    global UPSTREAM
    if args.upstream:
        host, _, port = args.upstream.rpartition(":")
        UPSTREAM = (host, int(port))
    proxy = (HttpProxy if args.mode == "http" else Socks5Proxy)(args.user, password)
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", args.port)
    log(f"{args.mode} proxy on 127.0.0.1:{args.port} (user {args.user})")
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["http", "socks5"])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password-env", default="PROXY_PASS")
    ap.add_argument("--upstream", help="HOST:PORT of an HTTP proxy to chain outgoing connections through")
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
