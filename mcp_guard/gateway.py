"""HTTP gateway: approval in front of a remote MCP server (Claude Web connectors).

Claude on the web (and remote "custom connectors" in Claude Desktop) do not
start local processes. Anthropic's servers call the MCP server's URL directly,
so the only place you can hold a call is on the path to that server. The
gateway is a reverse proxy you put on that path::

    Claude Web --HTTPS--> tunnel (cloudflared / ngrok) --> mcp-guard gateway --> real MCP server
                                                               |
                                                               +--> approval broker -> popup

You add the gateway's public URL as the custom connector instead of the real
server's URL. Both MCP HTTP transports are handled:

* **Streamable HTTP** (2025-03-26+): client POSTs JSON-RPC; the reply is JSON
  or an SSE stream. A blocked call gets a JSON reply with the denial.
* **HTTP+SSE** (2024-11-05): client keeps a GET /sse stream open and POSTs to
  the ``endpoint`` URL it was given; replies travel on the stream. A blocked
  call is answered with ``202 Accepted`` and the denial is written into the
  matching SSE stream, between two events so the stream stays valid.

Everything else (GET streams, DELETE, OAuth discovery, other methods) is
passed through untouched, headers included (``Mcp-Session-Id``,
``Authorization``...). Optional ``path_secret`` makes the public URL
unguessable: ``https://<tunnel>/<secret>/mcp``.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlsplit

from mcp_guard import __version__, jsonrpc
from mcp_guard.config import GatewaySettings, Settings
from mcp_guard.ipc import ExclusiveHTTPServer, GuardUnavailable, socket_alive
from mcp_guard.models import ApprovalRequest, Decision, Verdict, describe_target, extract_arguments

log = logging.getLogger(__name__)

MAX_REQUEST_BODY = 10_000_000
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding",
}


class Approver(Protocol):
    def request(self, req: ApprovalRequest, is_alive: Callable[[], bool] | None = None) -> Decision: ...


class _SseStream:
    """A client SSE stream we relay. Writes are whole events, under a lock."""

    def __init__(self, wfile: Any):
        self.wfile = wfile
        self.lock = threading.Lock()
        self.open = True

    def write_event(self, event: bytes) -> bool:
        with self.lock:
            if not self.open:
                return False
            try:
                self.wfile.write(b"%x\r\n%s\r\n" % (len(event), event))  # one HTTP chunk
                self.wfile.flush()
                return True
            except OSError:
                self.open = False
                return False

    def close(self) -> None:
        with self.lock:
            if self.open:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
            self.open = False


class GatewayServer(ExclusiveHTTPServer):
    def __init__(self, cfg: GatewaySettings, settings: Settings, approver: Approver):
        upstream = urlsplit(cfg.upstream)
        if upstream.scheme not in ("http", "https") or not upstream.hostname:
            raise ValueError(f"gateway {cfg.name!r}: upstream must be an http(s) URL, got {cfg.upstream!r}")
        self.cfg = cfg
        self.settings = settings
        self.approver = approver
        self.upstream = upstream
        self.secret_prefix = "/" + cfg.path_secret.strip("/") if cfg.path_secret.strip("/") else ""
        self.sse_sessions: dict[str, _SseStream] = {}       # legacy transport: sessionId -> stream
        self.client_info: dict[str, dict[str, Any]] = {}     # session id -> clientInfo
        self.state_lock = threading.Lock()
        super().__init__((cfg.listen_host, cfg.listen_port), _GatewayHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def upstream_connection(self) -> http.client.HTTPConnection:
        timeout = self.settings.gateway_upstream_timeout_seconds
        if self.upstream.scheme == "https":
            return http.client.HTTPSConnection(self.upstream.hostname, self.upstream.port, timeout=timeout)
        return http.client.HTTPConnection(self.upstream.hostname, self.upstream.port, timeout=timeout)


class _GatewayHandler(BaseHTTPRequestHandler):
    server: GatewayServer
    protocol_version = "HTTP/1.1"
    server_version = f"mcp-guard-gateway/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("gateway %s - " + fmt, self.address_string(), *args)

    # --------------------------------------------------------------- entry points

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def do_PATCH(self) -> None:  # noqa: N802
        self._handle()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle()

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle()

    # --------------------------------------------------------------- main path

    def _handle(self) -> None:
        path = self._upstream_path()
        if path is None:
            self._send_plain(404, b"not found")
            return
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_plain(413, str(exc).encode())
            return
        payload = None
        if self.command == "POST" and body:
            try:
                payload = json.loads(body)
            except ValueError:
                payload = None
        if payload is not None:
            needing = [m for m in jsonrpc.iter_messages(payload)
                       if jsonrpc.is_request(m) and m["method"] in self.server.settings.intercept_methods]
            if needing:
                decisions = self._decide(needing)
                if not all(d.allowed for d in decisions):
                    self._deny(payload, needing, decisions)
                    return
        self._forward(path, body, payload)

    def _upstream_path(self) -> str | None:
        """Map the gateway path to the upstream path (strip the secret prefix)."""
        path = self.path
        prefix = self.server.secret_prefix
        if prefix:
            if path != prefix and not path.startswith(prefix + "/") and not path.startswith(prefix + "?"):
                return None
            path = path[len(prefix):] or "/"
            if path.startswith("?"):
                path = "/" + path
        base = self.server.upstream.path.rstrip("/")
        return base + path if base and not path.startswith(base + "/") and path != base else path

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks, total = [], 0
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                total += size
                if total > MAX_REQUEST_BODY:
                    raise ValueError("request body too large")
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BODY:
            raise ValueError("request body too large")
        return self.rfile.read(length) if length > 0 else b""

    # --------------------------------------------------------------- approval

    def _session_key(self) -> str | None:
        sid = self.headers.get("Mcp-Session-Id")
        if sid:
            return "h:" + sid
        legacy = parse_qs(urlsplit(self.path).query).get("sessionId") or parse_qs(
            urlsplit(self.path).query).get("session_id")
        return "s:" + legacy[0] if legacy else None

    def _decide(self, messages: list[dict[str, Any]]) -> list[Decision]:
        key = self._session_key()
        with self.server.state_lock:
            info = dict(self.server.client_info.get(key, {})) if key else {}
        user_agent = self.headers.get("User-Agent")
        conn = self.connection

        def alive() -> bool:
            return socket_alive(conn)

        def ask(msg: dict[str, Any]) -> Decision:
            method = msg["method"]
            params = msg.get("params")
            req = ApprovalRequest(
                source=self.server.cfg.source,
                server=self.server.cfg.name,
                method=method,
                target=describe_target(method, params),
                arguments=extract_arguments(method, params),
                client_name=info.get("name") or (f"HTTP client ({user_agent[:60]})" if user_agent else None),
                client_version=info.get("version"),
                rpc_id=json.dumps(msg.get("id")),
            )
            try:
                return self.server.approver.request(req, is_alive=alive)
            except GuardUnavailable as exc:
                allow = self.server.settings.fail_mode == "open"
                return Decision(Verdict.ALLOW if allow else Verdict.BLOCK, "guard-unavailable", str(exc))
            except Exception as exc:
                log.exception("gateway approval failed")
                return Decision(Verdict.BLOCK, "error", str(exc))

        if len(messages) == 1:
            return [ask(messages[0])]
        results: list[Decision | None] = [None] * len(messages)

        def run(i: int) -> None:
            results[i] = ask(messages[i])

        threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(len(messages))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return [r or Decision(Verdict.BLOCK, "error") for r in results]

    def _deny(self, payload: Any, needing: list[dict[str, Any]], decisions: list[Decision]) -> None:
        style = self.server.settings.deny_style
        name = self.server.cfg.name
        by_id = {jsonrpc.id_key(m.get("id")): d for m, d in zip(needing, decisions)}
        replies = []
        for msg in jsonrpc.iter_messages(payload):
            if not jsonrpc.is_request(msg):
                continue
            d = by_id.get(jsonrpc.id_key(msg.get("id")))
            if d is not None and not d.allowed:
                replies.append(jsonrpc.denial_response(msg, name, d.reason, d.detail, style))
            else:
                replies.append(jsonrpc.error_response(msg.get("id"), jsonrpc.BATCH_ABORTED_CODE,
                                                      "Not run: another request in the same batch was blocked"))
        out: Any = replies[0] if isinstance(payload, dict) else replies

        # Legacy HTTP+SSE: the reply belongs on the session's event stream.
        key = self._session_key()
        if key and key.startswith("s:"):
            with self.server.state_lock:
                stream = self.server.sse_sessions.get(key[2:])
            if stream is not None:
                event = b"event: message\ndata: " + json.dumps(out).encode("utf-8") + b"\n\n"
                if stream.write_event(event):
                    self._send_plain(202, b"Accepted")
                    return
        body = json.dumps(out).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.headers.get("Mcp-Session-Id"):
            self.send_header("Mcp-Session-Id", self.headers["Mcp-Session-Id"])
        self.end_headers()
        self.wfile.write(body)

    # --------------------------------------------------------------- forwarding

    def _forward(self, path: str, body: bytes, payload: Any) -> None:
        srv = self.server
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["Host"] = srv.upstream.netloc
        headers["X-Forwarded-For"] = self.client_address[0]
        headers["X-Forwarded-Host"] = self.headers.get("Host", "")
        conn = srv.upstream_connection()
        try:
            conn.request(self.command, path, body=body if body else None, headers=headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            log.warning("gateway %s: upstream unreachable: %s", srv.cfg.name, exc)
            conn.close()
            self._send_plain(502, f"MCP Guard gateway: upstream unreachable ({exc})".encode())
            return
        try:
            self._remember_client(payload, resp)
            content_type = resp.getheader("Content-Type", "")
            if "text/event-stream" in content_type.lower():
                if conn.sock is not None:
                    conn.sock.settimeout(None)  # event streams may be quiet for a long time
                self._relay_sse(resp)
            else:
                data = resp.read()
                self.send_response(resp.status, resp.reason)
                self._copy_headers(resp)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
        except OSError as exc:
            log.info("gateway %s: client went away: %s", srv.cfg.name, exc)
            self.close_connection = True
        finally:
            resp.close()
            conn.close()

    def _copy_headers(self, resp: http.client.HTTPResponse) -> None:
        for key, value in resp.getheaders():
            if key.lower() not in HOP_BY_HOP:
                self.send_header(key, value)

    def _remember_client(self, payload: Any, resp: http.client.HTTPResponse) -> None:
        """Map the session to the clientInfo from its initialize request."""
        for msg in jsonrpc.iter_messages(payload):
            if msg.get("method") == "initialize" and isinstance(msg.get("params"), dict):
                info = msg["params"].get("clientInfo")
                sid = resp.getheader("Mcp-Session-Id")
                key = "h:" + sid if sid else self._session_key()
                if isinstance(info, dict) and key:
                    with self.server.state_lock:
                        self.server.client_info[key] = {k: str(v)[:200] for k, v in info.items()}

    def _relay_sse(self, resp: http.client.HTTPResponse) -> None:
        self.send_response(resp.status, resp.reason)
        self._copy_headers(resp)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        stream = _SseStream(self.wfile)
        session: str | None = None
        lines: list[bytes] = []
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                lines.append(line)
                if line in (b"\n", b"\r\n"):
                    event = b"".join(lines)
                    lines = []
                    event, new_session = self._inspect_event(event)
                    if new_session:
                        session = new_session
                        with self.server.state_lock:
                            self.server.sse_sessions[session] = stream
                    if not stream.write_event(event):
                        break
            if lines and stream.open:
                stream.write_event(b"".join(lines))
        except OSError as exc:
            log.info("gateway %s: SSE relay ended: %s", self.server.cfg.name, exc)
        finally:
            if session:
                with self.server.state_lock:
                    if self.server.sse_sessions.get(session) is stream:
                        del self.server.sse_sessions[session]
                    self.server.client_info.pop("s:" + session, None)
            stream.close()
            self.close_connection = True

    def _inspect_event(self, event: bytes) -> tuple[bytes, str | None]:
        """For legacy SSE, find the ``endpoint`` event and point it at the gateway."""
        if b"event: endpoint" not in event and b"event:endpoint" not in event:
            return event, None
        text = event.decode("utf-8", "replace")
        out_lines, session = [], None
        for line in text.splitlines():
            if line.startswith("data:"):
                url = line[5:].strip()
                parts = urlsplit(url)
                if parts.netloc and parts.netloc != self.server.upstream.netloc:
                    out_lines.append(line)  # points somewhere else; leave it alone
                    continue
                path = parts.path or "/"
                base = self.server.upstream.path.rstrip("/")
                if base and path.startswith(base + "/"):
                    path = path[len(base):]
                new_url = self.server.secret_prefix + path + (f"?{parts.query}" if parts.query else "")
                qs = parse_qs(parts.query)
                session = (qs.get("sessionId") or qs.get("session_id") or [None])[0]
                out_lines.append("data: " + new_url)
            else:
                out_lines.append(line)
        return ("\n".join(out_lines) + "\n\n").encode("utf-8"), session

    def _send_plain(self, status: int, body: bytes) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


def start_gateway(cfg: GatewaySettings, settings: Settings, approver: Approver) -> GatewayServer:
    """Bind and serve a gateway on a background thread."""
    server = GatewayServer(cfg, settings, approver)
    threading.Thread(target=server.serve_forever, name=f"gateway-{cfg.name}", daemon=True).start()
    log.info("gateway %r listening on http://%s:%s%s -> %s", cfg.name, cfg.listen_host, server.port,
             server.secret_prefix, cfg.upstream)
    return server
