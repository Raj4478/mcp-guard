"""Demo remote MCP server: ``python -m mcp_guard.demo.http_server --port 9000``.

Speaks both HTTP transports so the gateway can be tested against each:

* Streamable HTTP at ``POST /mcp`` (JSON reply; add ``?sse=1`` for an SSE reply)
* legacy HTTP+SSE at ``GET /sse`` + ``POST /messages?sessionId=...``
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mcp_guard.demo.tools import handle


class DemoHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]):
        self.sessions: dict[str, queue.Queue[bytes | None]] = {}
        self.lock = threading.Lock()
        self.calls: list[str] = []      # tool names actually executed (tests check this)
        super().__init__(address, _Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    server: DemoHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _answer(self, payload: Any) -> Any:
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get("method") == "tools/call":
                with self.server.lock:
                    self.server.calls.append(str((item.get("params") or {}).get("name")))
        replies = [r for r in (handle(m) for m in items) if r is not None]
        if not replies:
            return None
        return replies if isinstance(payload, list) else replies[0]

    def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            payload = json.loads(body)
        except ValueError:
            self._send(400, b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error"}}')
            return
        if url.path == "/messages":
            sid = (parse_qs(url.query).get("sessionId") or [""])[0]
            with self.server.lock:
                q = self.server.sessions.get(sid)
            if q is None:
                self._send(404, b"unknown session", "text/plain")
                return
            reply = self._answer(payload)
            if reply is not None:
                q.put(json.dumps(reply).encode("utf-8"))
            self._send(202, b"Accepted", "text/plain")
            return
        if url.path != "/mcp":
            self._send(404, b"not found", "text/plain")
            return
        reply = self._answer(payload)
        extra = {}
        if isinstance(payload, dict) and payload.get("method") == "initialize":
            extra["Mcp-Session-Id"] = uuid.uuid4().hex
        if reply is None:
            self._send(202, b"", "text/plain", extra)
        elif "sse=1" in url.query:
            event = b"event: message\ndata: " + json.dumps(reply).encode("utf-8") + b"\n\n"
            self._send(200, event, "text/event-stream", extra)
        else:
            self._send(200, json.dumps(reply).encode("utf-8"), "application/json", extra)

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/sse":
            self._send(405, b"use POST /mcp or GET /sse", "text/plain")
            return
        sid = uuid.uuid4().hex
        q: queue.Queue[bytes | None] = queue.Queue()
        with self.server.lock:
            self.server.sessions[sid] = q
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(data: bytes) -> None:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        try:
            chunk(f"event: endpoint\ndata: /messages?sessionId={sid}\n\n".encode())
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    chunk(b": keep-alive\n\n")
                    continue
                if item is None:
                    break
                chunk(b"event: message\ndata: " + item + b"\n\n")
            chunk(b"")  # zero-length chunk ends the chunked response
        except OSError:
            pass
        finally:
            with self.server.lock:
                self.server.sessions.pop(sid, None)
            self.close_connection = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo remote MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    server = DemoHTTPServer((args.host, args.port))
    print(f"Demo MCP server: Streamable HTTP at http://{args.host}:{server.port}/mcp, "
          f"legacy SSE at http://{args.host}:{server.port}/sse", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
