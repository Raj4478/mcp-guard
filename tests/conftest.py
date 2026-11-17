"""Shared fixtures: an isolated MCP Guard home, an in-process daemon with a
scripted "user", and a helper that talks to the proxy like Claude Desktop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from mcp_guard.broker import ApprovalBroker
from mcp_guard.config import Settings, get_or_create_token, write_daemon_state
from mcp_guard.ipc import IPCServer
from mcp_guard.models import ApprovalRequest, Decision, Verdict
from mcp_guard.policy import Policy

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def guard_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "guard-home"
    home.mkdir()
    monkeypatch.setenv("MCP_GUARD_HOME", str(home))
    monkeypatch.setenv("MCP_GUARD_DEMO_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("PYTHONPATH", str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return home


class ScriptedUI:
    """Stands in for the user: ``decide(req)`` returns (verdict or None, delay seconds).

    None leaves the request waiting (to test timeouts and cancellation).
    """

    def __init__(self, decide: Callable[[ApprovalRequest], tuple[Verdict | None, float]]):
        self.decide = decide
        self.broker: ApprovalBroker | None = None
        self.shown: list[ApprovalRequest] = []
        self.closed: list[tuple[ApprovalRequest, Decision]] = []
        self.lock = threading.Lock()

    def attach(self, broker: ApprovalBroker) -> None:
        self.broker = broker

    def on_new_request(self, req: ApprovalRequest, deadline: float) -> None:
        with self.lock:
            self.shown.append(req)
        verdict, delay = self.decide(req)
        if verdict is not None:
            timer = threading.Timer(delay, self.broker.resolve, args=(req.request_id, verdict))
            timer.daemon = True
            timer.start()

    def on_request_closed(self, req: ApprovalRequest, decision: Decision) -> None:
        with self.lock:
            self.closed.append((req, decision))

    def show_dashboard(self) -> None:
        pass

    def run(self, stop_event: threading.Event) -> None:
        stop_event.wait()


class Daemon:
    def __init__(self, ui: ScriptedUI, settings: Settings):
        self.ui = ui
        self.settings = settings
        self.broker = ApprovalBroker(settings, Policy.from_config(settings.rules), ui)
        ui.attach(self.broker)
        self.ipc = IPCServer(0, self.broker, get_or_create_token(), ui, on_shutdown=lambda: None)
        write_daemon_state(self.ipc.port)
        threading.Thread(target=self.ipc.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.broker.shutdown()
        self.ipc.shutdown()
        self.ipc.server_close()


def write_config(home: Path, **overrides: Any) -> Settings:
    data = {"autostart_daemon": False, "approval_timeout_seconds": 20, "daemon_port": 1, **overrides}
    (home / "config.json").write_text(json.dumps(data), encoding="utf-8")
    return Settings.load(home / "config.json")


@pytest.fixture
def make_daemon(guard_home):
    started: list[Daemon] = []

    def factory(decide, **config) -> Daemon:
        settings = write_config(guard_home, **config)
        d = Daemon(ScriptedUI(decide), settings)
        started.append(d)
        return d

    yield factory
    for d in started:
        d.close()


class ProxyClient:
    """Plays Claude Desktop: spawns the proxy and exchanges JSON-RPC lines."""

    def __init__(self, server_cmd: list[str] | None = None, env: dict[str, str] | None = None):
        server_cmd = server_cmd or [sys.executable, "-m", "mcp_guard.demo.stdio_server"]
        cmd = [sys.executable, "-m", "mcp_guard", "proxy", "--server-name", "demo", "--", *server_cmd]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     bufsize=0, cwd=str(REPO), env={**os.environ, **(env or {})})
        self.messages: list[Any] = []
        self.cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.stderr: list[bytes] = []

    def _read(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            with self.cond:
                self.messages.append(json.loads(line))
                self.cond.notify_all()

    def _drain_stderr(self) -> None:
        for line in iter(self.proc.stderr.readline, b""):
            self.stderr.append(line)

    def send(self, msg: Any) -> None:
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()

    def call(self, rpc_id: Any, name: str, arguments: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments or {}}})

    def wait_for(self, rpc_id: Any, timeout: float = 20) -> dict:
        end = time.monotonic() + timeout
        with self.cond:
            while time.monotonic() < end:
                for m in self.messages:
                    if isinstance(m, dict) and m.get("id") == rpc_id and ("result" in m or "error" in m):
                        return m
                self.cond.wait(0.1)
        raise AssertionError(f"no reply for id {rpc_id}; got {self.messages}; stderr={b''.join(self.stderr)[-2000:]!r}")

    def wait_batch(self, timeout: float = 20) -> list:
        end = time.monotonic() + timeout
        with self.cond:
            while time.monotonic() < end:
                for m in self.messages:
                    if isinstance(m, list):
                        return m
                self.cond.wait(0.1)
        raise AssertionError(f"no batch reply; got {self.messages}")

    def handshake(self) -> list[dict]:
        self.send({"jsonrpc": "2.0", "id": "init", "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "claude-ai", "version": "0.1.0"}}})
        self.wait_for("init")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
        return self.wait_for("list")["result"]["tools"]

    def close(self) -> int:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            return self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise


@pytest.fixture
def proxy_client(guard_home):
    clients: list[ProxyClient] = []

    def factory(**kwargs) -> ProxyClient:
        c = ProxyClient(**kwargs)
        clients.append(c)
        return c

    yield factory
    for c in clients:
        if c.proc.poll() is None:
            c.proc.kill()


def is_blocked(reply: dict) -> bool:
    if "error" in reply:
        return True
    result = reply.get("result") or {}
    return bool(result.get("isError")) and "MCP Guard blocked" in result["content"][0]["text"]
