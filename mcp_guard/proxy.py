"""stdio proxy: sits between Claude Desktop and one local MCP server.

Claude Desktop starts every local MCP server as a child process and talks
JSON-RPC over its stdin/stdout, one message per line. ``mcp-guard install``
rewrites ``claude_desktop_config.json`` so Claude starts *this proxy* instead,
and the proxy starts the real server::

    Claude Desktop  <-stdio->  mcp-guard proxy  <-stdio->  real MCP server
                                     |
                                     +-- IPC --> mcp-guard daemon (popup)

Message flow:

* client -> server: ``tools/call`` (and any other method listed in
  ``intercept_methods``) is held. A worker thread asks the daemon and then
  either writes the original line to the server or writes a denial back to
  Claude. Everything else (initialize, tools/list, pings, notifications) is
  passed through at once, so Claude never stalls while a popup is open.
* server -> client: passed through unchanged. ``tools/list`` results are read
  on the way past so the popup can show the tool's description and hints.
* ``notifications/cancelled`` for a held request closes its popup, and the
  request is dropped (the spec says not to answer a cancelled request).

Every held request has its own thread, so many can wait at once and be
answered in any order; JSON-RPC ids keep the replies matched up.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Protocol

from mcp_guard import jsonrpc
from mcp_guard.config import Settings
from mcp_guard.ipc import GuardUnavailable
from mcp_guard.models import ApprovalRequest, Decision, Verdict, describe_target, extract_arguments

log = logging.getLogger(__name__)


class Approver(Protocol):
    def request(self, req: ApprovalRequest, is_alive: Callable[[], bool] | None = None) -> Decision: ...
    def cancel(self, request_id: str) -> bool: ...


@dataclass
class _HeldCall:
    messages: list[dict[str, Any]]        # one request, or the requests of a batch
    raw: bytes                            # the original line, forwarded byte-for-byte if allowed
    payload: Any                          # parsed line (dict or list)
    requests: list[ApprovalRequest] = field(default_factory=list)
    cancelled: threading.Event = field(default_factory=threading.Event)


class StdioProxy:
    def __init__(self, server_name: str, command: list[str], settings: Settings, approver: Approver,
                 source: str = "Claude Desktop", client_in: IO[bytes] | None = None,
                 client_out: IO[bytes] | None = None, exit_hook: Callable[[int], None] | None = None):
        if not command:
            raise ValueError("no server command given")
        self.server_name = server_name
        self.command = command
        self.settings = settings
        self.approver = approver
        self.source = source
        self.client_in = client_in or sys.stdin.buffer
        self.client_out = client_out or sys.stdout.buffer
        self.exit_hook = exit_hook or _hard_exit
        self.child: subprocess.Popen[bytes] | None = None
        self._out_lock = threading.Lock()
        self._child_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._held: dict[str, _HeldCall] = {}          # id_key -> call (single requests)
        self._tools_list_ids: set[str] = set()
        self._tools: dict[str, dict[str, Any]] = {}
        self._client_info: dict[str, Any] = {}
        self._closing = threading.Event()

    # ================================================================== lifecycle

    def run(self) -> int:
        """Start the server, pump both directions, return the server's exit code."""
        try:
            self.child = self._spawn()
        except OSError as exc:
            log.error("cannot start MCP server %r: %s", self.command, exc)
            return 127
        log.info("proxy for %r started server pid %s: %s", self.server_name, self.child.pid, self.command)
        threading.Thread(target=self._pump_server, name="server->client", daemon=True).start()
        try:
            for raw in iter(self.client_in.readline, b""):
                self._on_client_line(raw)
        except (OSError, ValueError) as exc:
            log.info("client stream closed: %s", exc)
        log.info("client closed stdin; shutting down")
        return self.shutdown()

    def shutdown(self) -> int:
        """Cancel held requests, stop the server politely, then firmly."""
        self._closing.set()
        self._cancel_all_held()
        child = self.child
        if child is None:
            return 0
        try:
            if child.stdin:
                child.stdin.close()
        except OSError:
            pass
        for wait, action in ((3.0, child.terminate), (2.0, child.kill)):
            try:
                return child.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                action()
        return child.wait()

    def _spawn(self) -> subprocess.Popen[bytes]:
        exe = shutil.which(self.command[0]) or self.command[0]  # resolves npx -> npx.cmd on Windows
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # We usually run without a console; stop Windows opening a new one for the child.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return subprocess.Popen([exe, *self.command[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=None, bufsize=0, env=os.environ.copy(), **kwargs)

    def _pump_server(self) -> None:
        assert self.child is not None and self.child.stdout is not None
        try:
            for raw in iter(self.child.stdout.readline, b""):
                self._inspect_server_line(raw)
                self._to_client(raw if raw.endswith(b"\n") else raw + b"\n")
        except (OSError, ValueError) as exc:
            log.info("server stream closed: %s", exc)
        code = self.child.wait()
        if self._closing.is_set():
            return  # we are shutting the server down ourselves
        log.warning("MCP server %r exited with code %s", self.server_name, code)
        self._closing.set()
        self._fail_all_held("the MCP server process exited")
        self._flush_client()
        self.exit_hook(code if code is not None else 1)

    # ================================================================== client -> server

    def _on_client_line(self, raw: bytes) -> None:
        line = raw.strip()
        if not line:
            return
        try:
            payload = json.loads(line)
        except ValueError:
            self._to_child(line + b"\n")  # not JSON: let the server report the parse error
            return
        messages = list(jsonrpc.iter_messages(payload))
        for msg in messages:
            self._observe(msg)
        if isinstance(payload, dict) and payload.get("method") == "notifications/cancelled":
            params = payload.get("params") or {}
            if isinstance(params, dict) and self._cancel_held(params.get("requestId")):
                return  # the server never saw that request, so it must not see the cancel either
        needing = [m for m in messages if jsonrpc.is_request(m) and m["method"] in self.settings.intercept_methods]
        if not needing:
            self._to_child(line + b"\n")
            return
        call = _HeldCall(messages=needing, raw=line + b"\n", payload=payload)
        call.requests = [self._build_request(m) for m in needing]
        if isinstance(payload, dict):
            key = jsonrpc.id_key(payload.get("id"))
            with self._state_lock:
                if key in self._held:
                    self._to_client(jsonrpc.dumps_line(jsonrpc.error_response(
                        payload.get("id"), -32600, "a request with this id is already pending")))
                    return
                self._held[key] = call
        threading.Thread(target=self._decide_and_dispatch, args=(call,), daemon=True,
                         name=f"hold-{call.requests[0].request_id[:8]}").start()

    def _observe(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if method == "initialize" and isinstance(params.get("clientInfo"), dict):
            self._client_info = params["clientInfo"]
        elif method == "tools/list" and "id" in msg:
            with self._state_lock:
                self._tools_list_ids.add(jsonrpc.id_key(msg["id"]))

    def _build_request(self, msg: dict[str, Any]) -> ApprovalRequest:
        method = msg["method"]
        params = msg.get("params")
        target = describe_target(method, params)
        meta = self._tools.get(target or "", {}) if method == "tools/call" else {}
        return ApprovalRequest(
            source=self.source,
            server=self.server_name,
            method=method,
            target=target,
            arguments=extract_arguments(method, params),
            client_name=_str_or_none(self._client_info.get("name")),
            client_version=_str_or_none(self._client_info.get("version")),
            tool_title=meta.get("title"),
            tool_description=meta.get("description"),
            annotations=meta.get("annotations"),
            rpc_id=json.dumps(msg.get("id")),
        )

    def _decide_and_dispatch(self, call: _HeldCall) -> None:
        decisions = self._decide_all(call)
        if isinstance(call.payload, dict):
            with self._state_lock:
                self._held.pop(jsonrpc.id_key(call.payload.get("id")), None)
        if call.cancelled.is_set() or self._closing.is_set():
            log.info("dropping %s: cancelled before a decision", call.requests[0].target)
            return
        if all(d.allowed for d in decisions):
            if not self._to_child(call.raw):
                self._reply_server_gone(call)
            return
        self._to_client(jsonrpc.dumps_line(self._denial_payload(call, decisions)))

    def _decide_all(self, call: _HeldCall) -> list[Decision]:
        def alive() -> bool:
            return not call.cancelled.is_set() and not self._closing.is_set()

        if len(call.requests) == 1:
            return [self._decide(call.requests[0], alive)]
        # Batch: ask about every request at once, so the popups appear together.
        results: list[Decision | None] = [None] * len(call.requests)

        def one(i: int) -> None:
            results[i] = self._decide(call.requests[i], alive)

        threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(len(call.requests))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return [r or Decision(Verdict.BLOCK, "error") for r in results]

    def _decide(self, req: ApprovalRequest, alive: Callable[[], bool]) -> Decision:
        try:
            return self.approver.request(req, is_alive=alive)
        except GuardUnavailable as exc:
            if self.settings.fail_mode == "open":
                log.warning("approval service unavailable (%s); fail_mode=open so allowing", exc)
                return Decision(Verdict.ALLOW, "guard-unavailable", str(exc))
            log.error("approval service unavailable (%s); blocking", exc)
            return Decision(Verdict.BLOCK, "guard-unavailable", "start it with: mcp-guard daemon")
        except Exception as exc:  # never let one bad request kill the proxy
            log.exception("approval failed")
            return Decision(Verdict.BLOCK, "error", str(exc))

    def _denial_payload(self, call: _HeldCall, decisions: list[Decision]) -> Any:
        style = self.settings.deny_style
        if isinstance(call.payload, dict):
            d = decisions[0]
            return jsonrpc.denial_response(call.payload, self.server_name, d.reason, d.detail, style)
        # Batch with at least one blocked request: nothing in it is forwarded,
        # so a batch never runs half of its steps. Every request gets an answer.
        by_id = {jsonrpc.id_key(m.get("id")): d for m, d in zip(call.messages, decisions)}
        replies = []
        for msg in jsonrpc.iter_messages(call.payload):
            if not jsonrpc.is_request(msg):
                continue
            d = by_id.get(jsonrpc.id_key(msg.get("id")))
            if d is not None and not d.allowed:
                replies.append(jsonrpc.denial_response(msg, self.server_name, d.reason, d.detail, style))
            else:
                replies.append(jsonrpc.error_response(
                    msg.get("id"), jsonrpc.BATCH_ABORTED_CODE,
                    "Not run: another request in the same batch was blocked by MCP Guard"))
        return replies

    # ================================================================== cancel / failure

    def _cancel_held(self, rpc_id: Any) -> bool:
        with self._state_lock:
            call = self._held.get(jsonrpc.id_key(rpc_id))
        if call is None:
            return False
        call.cancelled.set()
        for req in call.requests:
            self.approver.cancel(req.request_id)
        log.info("client cancelled held request %s", rpc_id)
        return True

    def _cancel_all_held(self) -> None:
        with self._state_lock:
            calls = list(self._held.values())
        for call in calls:
            call.cancelled.set()
            for req in call.requests:
                self.approver.cancel(req.request_id)

    def _fail_all_held(self, why: str) -> None:
        with self._state_lock:
            calls = list(self._held.values())
            self._held.clear()
        for call in calls:
            call.cancelled.set()
            for req in call.requests:
                self.approver.cancel(req.request_id)
            self._reply_server_gone(call, why)

    def _reply_server_gone(self, call: _HeldCall, why: str = "could not reach the MCP server") -> None:
        for msg in call.messages:
            self._to_client(jsonrpc.dumps_line(jsonrpc.error_response(
                msg.get("id"), jsonrpc.SERVER_GONE_CODE, f"MCP Guard: {why}")))

    # ================================================================== server -> client

    def _inspect_server_line(self, raw: bytes) -> None:
        """Remember tool descriptions/annotations from tools/list replies."""
        if not self._tools_list_ids or b'"tools"' not in raw:
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if not isinstance(msg, dict):
            return
        key = jsonrpc.id_key(msg.get("id"))
        with self._state_lock:
            if key not in self._tools_list_ids:
                return
            self._tools_list_ids.discard(key)
        tools = (msg.get("result") or {}).get("tools") if isinstance(msg.get("result"), dict) else None
        if not isinstance(tools, list):
            return
        for tool in tools:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else None
                self._tools[tool["name"]] = {
                    "title": _str_or_none(tool.get("title") or (annotations or {}).get("title")),
                    "description": _str_or_none(tool.get("description")),
                    "annotations": annotations,
                }

    # ================================================================== writers

    def _to_client(self, data: bytes) -> bool:
        with self._out_lock:
            try:
                self.client_out.write(data)
                self.client_out.flush()
                return True
            except (OSError, ValueError):
                log.info("client output closed")
                self._closing.set()
                return False

    def _flush_client(self) -> None:
        with self._out_lock:
            try:
                self.client_out.flush()
            except (OSError, ValueError):
                pass

    def _to_child(self, data: bytes) -> bool:
        child = self.child
        if child is None or child.stdin is None:
            return False
        with self._child_lock:
            try:
                child.stdin.write(data)
                child.stdin.flush()
                return True
            except (OSError, ValueError):
                log.warning("could not write to the MCP server (it may have exited)")
                return False


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _hard_exit(code: int) -> None:
    # The main thread is blocked reading stdin; exiting the process is the
    # only reliable way to stop it once the server has gone.
    logging.shutdown()
    os._exit(code)


def run_proxy(server_name: str, command: list[str], settings: Settings, source: str) -> int:
    """Entry point used by ``mcp-guard proxy``."""
    from mcp_guard.ipc import ApprovalClient

    client = ApprovalClient(settings)
    proxy = StdioProxy(server_name, command, settings, client, source=source)

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("signal %s: shutting down", signum)
        proxy.exit_hook(proxy.shutdown())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass
    # Start the daemon now (not at the first tool call) so the first popup is quick.
    threading.Thread(target=_warm_up, args=(client,), daemon=True).start()
    return proxy.run()


def _warm_up(client: Any) -> None:
    try:
        client.ensure_daemon()
    except GuardUnavailable as exc:
        log.warning("approval daemon not available yet: %s", exc)
