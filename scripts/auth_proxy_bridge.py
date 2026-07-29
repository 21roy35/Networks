"""Expose an authenticated upstream HTTP proxy on localhost without auth.

Chromium's command-line proxy switch cannot reliably carry embedded proxy
credentials.  This tiny bridge keeps those credentials in an environment
variable and presents a localhost proxy that a normally launched Chrome process
can use without Playwright's automation launch flags.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import selectors
import socket
import socketserver
from pathlib import Path
from urllib.parse import unquote, urlparse


MAX_HEADER = 128 * 1024


def _read_header(sock: socket.socket) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_HEADER:
            raise ValueError("Proxy request header is too large.")
    marker = data.find(b"\r\n\r\n")
    if marker < 0:
        return bytes(data), b""
    return bytes(data[: marker + 4]), bytes(data[marker + 4 :])


def _with_proxy_auth(header: bytes, authorization: str) -> bytes:
    head, separator, tail = header.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("Incomplete proxy request header.")
    lines = head.split(b"\r\n")
    lines = [
        line for line in lines
        if not line.lower().startswith(b"proxy-authorization:")
    ]
    lines.append(f"Proxy-Authorization: Basic {authorization}".encode("ascii"))
    return b"\r\n".join(lines) + b"\r\n\r\n" + tail


def _idle_timeout_seconds() -> int:
    raw_idle_timeout = os.environ.get(
        "FLIGHTBOT_PROXY_IDLE_TIMEOUT_SECONDS", "180").strip()
    try:
        return max(60, min(int(raw_idle_timeout), 300))
    except (TypeError, ValueError):
        return 180


def _tunnel(left: socket.socket, right: socket.socket) -> None:
    idle_timeout = _idle_timeout_seconds()
    selector = selectors.DefaultSelector()
    selector.register(left, selectors.EVENT_READ, right)
    selector.register(right, selectors.EVENT_READ, left)
    try:
        while True:
            events = selector.select(timeout=idle_timeout)
            if not events:
                # Residential proxy providers commonly discard an apparently
                # idle CONNECT mapping without sending FIN. Three minutes
                # still retires those mappings, but does not truncate GACA's
                # unusually slow chunked Step 4 HTML response.
                return
            for key, _ in events:
                source = key.fileobj
                target = key.data
                try:
                    data = source.recv(65536)
                except (ConnectionResetError, BrokenPipeError):
                    return
                if not data:
                    return
                try:
                    target.sendall(data)
                except (ConnectionResetError, BrokenPipeError):
                    return
    finally:
        selector.close()


class ProxyHandler(socketserver.BaseRequestHandler):
    upstream_host: str
    upstream_port: int
    upstream_username: str
    upstream_password: str
    session_file: Path | None = None
    target_city: str = ""

    @classmethod
    def _authorization(cls) -> str:
        username = cls.upstream_username
        if cls.target_city:
            city = cls.target_city.casefold()
            if not re.fullmatch(r"[a-z0-9]+", city):
                raise RuntimeError(
                    "FLIGHTBOT_GACA_PROXY_CITY is invalid.")
            if re.search(r"(?i)-city-[^-]+", username):
                username = re.sub(
                    r"(?i)(-city-)([^-]+)",
                    lambda match: match.group(1) + city,
                    username,
                    count=1,
                )
            else:
                username, count = re.subn(
                    r"(?i)(-country-[^-]+)",
                    lambda match: match.group(1) + f"-city-{city}",
                    username,
                    count=1,
                )
                if count != 1:
                    raise RuntimeError(
                        "The upstream proxy username has no country marker.")
        if cls.session_file is not None:
            try:
                session = cls.session_file.read_text(
                    encoding="ascii").strip()
            except OSError:
                session = ""
            if session:
                if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", session):
                    raise RuntimeError(
                        "The GACA proxy-session file is invalid.")
                username, count = re.subn(
                    r"(?i)(-session-)([^-]+)",
                    lambda match: match.group(1) + session,
                    username,
                    count=1,
                )
                if count != 1:
                    raise RuntimeError(
                        "The upstream proxy username has no session marker.")
        credentials = (
            f"{username}:{cls.upstream_password}".encode("utf-8"))
        return base64.b64encode(credentials).decode("ascii")

    def handle(self) -> None:
        client = self.request
        client.settimeout(90)
        header, remainder = _read_header(client)
        if not header:
            return
        upstream = socket.create_connection(
            (self.upstream_host, self.upstream_port), timeout=30)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        upstream.settimeout(90)
        try:
            upstream.sendall(_with_proxy_auth(
                header, self._authorization()))
            if remainder:
                upstream.sendall(remainder)
            response, response_remainder = _read_header(upstream)
            client.sendall(response)
            if response_remainder:
                client.sendall(response_remainder)
            status_line = response.split(b"\r\n", 1)[0]
            if header.startswith(b"CONNECT ") and b" 200 " in status_line:
                _tunnel(client, upstream)
                return
            _tunnel(client, upstream)
        finally:
            upstream.close()


class ThreadedProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=18888)
    args = parser.parse_args()

    raw = (
        os.environ.get("FLIGHTBOT_UPSTREAM_PROXY", "").strip()
        or os.environ.get("FLIGHTBOT_GACA_PROXY", "").strip()
    )
    if not raw:
        raise RuntimeError(
            "FLIGHTBOT_UPSTREAM_PROXY or FLIGHTBOT_GACA_PROXY "
            "must be configured.")
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    if not parsed.hostname or not parsed.port:
        raise RuntimeError("The upstream proxy URL is invalid.")
    if parsed.username is None or parsed.password is None:
        raise RuntimeError("The upstream proxy requires username and password.")
    ProxyHandler.upstream_host = parsed.hostname
    ProxyHandler.upstream_port = parsed.port
    ProxyHandler.upstream_username = unquote(parsed.username)
    ProxyHandler.upstream_password = unquote(parsed.password)
    ProxyHandler.target_city = os.environ.get(
        "FLIGHTBOT_GACA_PROXY_CITY", "").strip()
    session_file = os.environ.get(
        "FLIGHTBOT_GACA_PROXY_SESSION_FILE", "").strip()
    ProxyHandler.session_file = Path(session_file) if session_file else None

    with ThreadedProxyServer(
            (args.listen_host, args.listen_port), ProxyHandler) as server:
        print(
            f"Listening on {args.listen_host}:{args.listen_port}",
            flush=True,
        )
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
