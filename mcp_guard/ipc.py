"""Localhost channel between interceptors and the daemon.

A small HTTP/JSON API on ``127.0.0.1`` only, protected by a bearer token that
is stored in the user's profile folder::

    GET  /v1/health                     -> {"ok": true, "app": "mcp-guard"}   (no token)
    POST /v1/approvals                  -> blocks until decided -> Decision JSON
    POST /v1/approvals/<id>/cancel      -> {"cancelled": bool}
    GET  /v1/pending                    -> list of pending requests
    POST /v1/ui/show                    -> show the dashboard window
    POST /v1/shutdown                   -> stop the daemon

``POST /v1/approvals`` is a long poll. While it waits, the handler watches the
socket: if the proxy dies (Claude quit, server restarted), the connection
closes and the request is cancelled so the popup goes away.

Requests that carry an ``Origin`` header are refused, and the ``Host`` header
must be a loopback name, so a web page cannot reach the API through the
browser (CSRF / DNS rebinding).
"""

from __future__ import annotations

import hmac
import http.client
import json
import logging
import os
import select
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from mcp_guard import __version__
from mcp_guard.config import Settings, get_or_create_token, read_daemon_state
from mcp_guard.models import ApprovalRequest, Decision

if TYPE_CHECKING:
    from mcp_guard.broker import ApprovalBroker
    from mcp_guard.ui.base import ApprovalUI

log = logging.getLogger(__name__)

MAX_BODY = 1_000_000
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


class GuardUnavailable(Exception):
    """The daemon cannot be reached (not running, crashed, wrong token...)."""


# ============================================================================ helpers


def socket_alive(sock: socket.socket) -> bool:
    """True while the peer keeps the connection open.

    The request body has been read already, so the socket only becomes
    readable when the peer closes it (recv returns b"") or misbehaves.
    """
    try:
        readable, _, _ = select.select([sock], [], [], 0)
        if not readable:
            return True
        return bool(sock.recv(1, socket.MSG_PEEK))
    except (OSError, ValueError):
        return False


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that refuses to share its port.

    On Windows SO_REUSEADDR lets a second process bind the same port, which
    would allow two daemons (or a squatter). SO_EXCLUSIVEADDRUSE prevents it.
    """

    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A peer hanging up mid-request is normal (Claude restarting a server);
        # log it instead of printing a traceback to stderr.
        log.debug("connection from %s ended with an error", client_address, exc_info=True)


# ============================================================================ server


class IPCServer(ExclusiveHTTPServer):
    def __init__(self, port: int, broker: "ApprovalBroker", token: str, ui: "ApprovalUI | None",
                 on_shutdown: Callable[[], None]):
        self.broker = broker
        self.token = token
        self.ui = ui
        self.on_shutdown = on_shutdown
        super().__init__(("127.0.0.1", port), _IPCHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]


class _IPCHandler(BaseHTTPRequestHandler):
    server: IPCServer
    server_version = f"mcp-guard/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:  # route to logging, not stderr
        log.debug("ipc %s - " + fmt, self.address_string(), *args)

    # -------------------------------------------------------------- plumbing

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            log.debug("ipc client went away before the reply")

    def _guard_request(self, need_token: bool = True) -> bool:
        if self.headers.get("Origin"):
            self._send_json(403, {"error": "browser requests are not allowed"})
            return False
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        if host not in LOOPBACK_HOSTS:
            self._send_json(403, {"error": "bad host"})
            return False
        if need_token:
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {self.server.token}"
            if not hmac.compare_digest(auth.encode(), expected.encode()):
                self._send_json(401, {"error": "bad token"})
                return False
        return True

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("missing or oversized body")
        return json.loads(self.rfile.read(length))

    # -------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self.path == "/v1/health":
            if self._guard_request(need_token=False):
                self._send_json(200, {"ok": True, "app": "mcp-guard", "version": __version__, "pid": os.getpid()})
            return
        if not self._guard_request():
            return
        if self.path == "/v1/pending":
            self._send_json(200, [r.to_dict() for r in self.server.broker.pending_requests()])
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard_request():
            return
        path = self.path
        if path == "/v1/approvals":
            self._approve()
        elif path.startswith("/v1/approvals/") and path.endswith("/cancel"):
            request_id = path[len("/v1/approvals/"):-len("/cancel")]
            self._send_json(200, {"cancelled": self.server.broker.cancel(request_id)})
        elif path == "/v1/ui/show":
            if self.server.ui is not None:
                self.server.ui.show_dashboard()
            self._send_json(200, {"ok": True})
        elif path == "/v1/shutdown":
            self._send_json(200, {"ok": True})
            threading.Thread(target=self.server.on_shutdown, daemon=True).start()
        else:
            self._send_json(404, {"error": "not found"})

    def _approve(self) -> None:
        try:
            req = ApprovalRequest.from_dict(self._read_json())
        except (ValueError, TypeError) as exc:
            self._send_json(400, {"error": f"bad request: {exc}"})
            return
        conn = self.connection
        decision = self.server.broker.request(req, is_alive=lambda: socket_alive(conn))
        self._send_json(200, decision.to_dict())


# ============================================================================ client


class ApprovalClient:
    """Used by the stdio proxy (and a standalone gateway) to ask the daemon."""

    def __init__(self, settings: Settings, autostart: bool | None = None):
        self.settings = settings
        self.autostart = settings.autostart_daemon if autostart is None else autostart
        self._token: str | None = None
        self._start_lock = threading.Lock()

    # -------------------------------------------------------------- endpoint

    def port(self) -> int:
        state = read_daemon_state()
        if state and isinstance(state.get("port"), int) and state["port"] > 0:
            return state["port"]
        return self.settings.daemon_port

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = get_or_create_token()
        return self._token

    def _call(self, method: str, path: str, payload: Any = None, timeout: float = 5.0) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port(), timeout=timeout)
        try:
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                raise GuardUnavailable(f"daemon answered HTTP {resp.status}: {data[:200]!r}")
            return json.loads(data)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise GuardUnavailable(str(exc) or exc.__class__.__name__) from exc
        finally:
            conn.close()

    # -------------------------------------------------------------- API

    def health(self) -> bool:
        conn = http.client.HTTPConnection("127.0.0.1", self.port(), timeout=1.5)
        try:
            conn.request("GET", "/v1/health")
            resp = conn.getresponse()
            data = json.loads(resp.read())
            return resp.status == 200 and data.get("app") == "mcp-guard"
        except (OSError, http.client.HTTPException, ValueError):
            return False
        finally:
            conn.close()

    def ensure_daemon(self, wait_seconds: float = 15.0) -> None:
        """Start the daemon in the background if it is not running."""
        if self.health():
            return
        if not self.autostart:
            raise GuardUnavailable("MCP Guard daemon is not running (autostart is off)")
        with self._start_lock:
            if self.health():
                return
            log.info("starting MCP Guard daemon in the background")
            spawn_daemon()
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if self.health():
                    return
                time.sleep(0.2)
        raise GuardUnavailable("MCP Guard daemon did not start")

    def request(self, req: ApprovalRequest, is_alive: Callable[[], bool] | None = None) -> Decision:
        """Ask the daemon and wait for the user's decision.

        If ``is_alive`` turns False while waiting, the request is cancelled in
        the daemon (popup closes) and a "cancelled" block comes back.
        """
        self.ensure_daemon()
        timeout = self.settings.approval_timeout_seconds + 30
        if is_alive is None:
            return Decision.from_dict(self._call("POST", "/v1/approvals", req.to_dict(), timeout))

        result: dict[str, Any] = {}

        def worker() -> None:
            try:
                result["decision"] = Decision.from_dict(
                    self._call("POST", "/v1/approvals", req.to_dict(), timeout))
            except BaseException as exc:  # handed to the caller below
                result["error"] = exc

        thread = threading.Thread(target=worker, daemon=True, name=f"approve-{req.request_id[:8]}")
        thread.start()
        cancelled = False
        while thread.is_alive():
            thread.join(0.25)
            if thread.is_alive() and not cancelled and not is_alive():
                cancelled = True
                self.cancel(req.request_id)
        if "error" in result:
            raise result["error"]
        return result["decision"]

    def cancel(self, request_id: str) -> bool:
        try:
            return bool(self._call("POST", f"/v1/approvals/{request_id}/cancel", {}).get("cancelled"))
        except GuardUnavailable:
            return False

    def pending(self) -> list[dict[str, Any]]:
        return self._call("GET", "/v1/pending")

    def show_dashboard(self) -> None:
        self._call("POST", "/v1/ui/show", {})

    def shutdown(self) -> None:
        self._call("POST", "/v1/shutdown", {})


def spawn_daemon() -> subprocess.Popen:
    """Launch ``python -m mcp_guard daemon --background`` detached from this process.

    On Windows we use pythonw.exe (no console window) and try to break away
    from the job object of the app that started us, so the daemon survives
    when Claude restarts its MCP servers.
    """
    exe = Path(sys.executable)
    if sys.platform == "win32":
        windowless = exe.with_name("pythonw.exe")
        if windowless.exists():
            exe = windowless
    args = [str(exe), "-m", "mcp_guard", "daemon", "--background"]
    common: dict[str, Any] = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, close_fds=True, env=os.environ.copy())
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            return subprocess.Popen(args, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
        except OSError:
            return subprocess.Popen(args, creationflags=flags, **common)
    return subprocess.Popen(args, start_new_session=True, **common)
