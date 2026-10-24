#!/usr/bin/env python3
"""netguard: an HTTPS (CONNECT) proxy that logs every host a build contacts
and refuses the conda package infrastructure.

    python3 tests/netguard.py --port 18080 --log hosts.txt &
    HTTPS_PROXY=http://127.0.0.1:18080 https_proxy=http://127.0.0.1:18080 \
        bash build-openjdk21.sh

Blocked by default: prefix.dev (and *.prefix.dev), anaconda.org,
*.anaconda.org, conda.anaconda.org, repo.anaconda.com, conda-forge.org.
If HTTPS_PROXY is set when netguard starts, allowed connections are chained
through it (useful behind a corporate / sandbox proxy).
"""
import argparse
import asyncio
import os
import sys
import time
from urllib.parse import urlparse

BLOCK = ["prefix.dev", "anaconda.org", "anaconda.com", "conda-forge.org", "emscripten-forge.org"]


def blocked(host):
    h = host.lower().rstrip(".")
    return any(h == b or h.endswith("." + b) for b in BLOCK)


async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            w.close()
        except Exception:  # noqa
            pass


class Guard:
    def __init__(self, log, upstream):
        self.log = log
        self.upstream = upstream
        self.seen = {}

    def record(self, host, verdict):
        """Allowed hosts are logged once; every refused attempt is logged."""
        key = (host, verdict)
        if key not in self.seen or verdict != "ALLOWED":
            self.seen[key] = time.time()
            stamp = time.strftime("%H:%M:%S")
            with open(self.log, "a") as f:
                f.write("%s %s %s\n" % (verdict, host, stamp))
            sys.stderr.write("[netguard] %s %s\n" % (verdict, host))

    async def handle(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except Exception:  # noqa
            writer.close()
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            # plain-HTTP proxying is not supported: log the host and refuse
            host = urlparse(parts[1]).hostname if len(parts) > 1 else "?"
            self.record(host or "?", "HTTP-REFUSED")
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        target = parts[1]
        host, _, port = target.rpartition(":")
        if blocked(host):
            self.record(host, "BLOCKED")
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        self.record(host, "ALLOWED")
        try:
            if self.upstream:
                ur, uw = await asyncio.open_connection(self.upstream.hostname, self.upstream.port)
                uw.write(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (target, target)).encode())
                await uw.drain()
                resp = await ur.readuntil(b"\r\n\r\n")
                if b" 200" not in resp.split(b"\r\n", 1)[0]:
                    writer.write(resp)
                    await writer.drain()
                    writer.close()
                    uw.close()
                    return
            else:
                ur, uw = await asyncio.open_connection(host, int(port or 443))
        except Exception as e:  # noqa
            writer.write(("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nX-Error: %s\r\n\r\n"
                          % str(e).replace("\r", " ").replace("\n", " ")).encode())
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(pipe(reader, uw), pipe(ur, writer))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--log", default="netguard-hosts.txt")
    ap.add_argument("--block", action="append", default=[],
                    help="extra domain to refuse (with its subdomains)")
    a = ap.parse_args()
    BLOCK.extend(a.block)
    up = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    upstream = urlparse(up) if up else None
    g = Guard(os.path.abspath(a.log), upstream)

    async def run():
        srv = await asyncio.start_server(g.handle, "127.0.0.1", a.port)
        sys.stderr.write("[netguard] listening on 127.0.0.1:%d%s, blocking %s\n" % (
            a.port, (" via %s" % up) if up else "", ", ".join(BLOCK)))
        async with srv:
            await srv.serve_forever()
    asyncio.run(run())


if __name__ == "__main__":
    main()
