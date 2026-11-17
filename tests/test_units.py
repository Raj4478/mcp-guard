"""Unit tests: broker, policy, JSON-RPC helpers, IPC security, installer, config."""

from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from mcp_guard import installer, jsonrpc
from mcp_guard.broker import ApprovalBroker
from mcp_guard.config import Settings, get_or_create_token
from mcp_guard.models import ApprovalRequest, Verdict
from mcp_guard.policy import Policy


def req(target="get_time", server="demo", **kw):
    return ApprovalRequest(source="test", server=server, method="tools/call", target=target, arguments={}, **kw)


class RecordingUI:
    def __init__(self):
        self.shown, self.closed = [], []

    def attach(self, broker):
        pass

    def on_new_request(self, r, deadline):
        self.shown.append(r)

    def on_request_closed(self, r, d):
        self.closed.append((r, d))

    def show_dashboard(self):
        pass

    def run(self, stop):
        pass


# ----------------------------------------------------------------------------- broker


def test_broker_waits_for_resolve():
    ui = RecordingUI()
    broker = ApprovalBroker(Settings(), Policy(), ui)
    r = req()
    threading.Timer(0.2, broker.resolve, args=(r.request_id, Verdict.ALLOW)).start()
    decision = broker.request(r)
    assert decision.allowed and decision.reason == "user"
    assert ui.shown == [r] and ui.closed[0][1] is decision


def test_broker_timeout_blocks():
    broker = ApprovalBroker(Settings(approval_timeout_seconds=0.3), Policy(), RecordingUI())
    started = time.monotonic()
    decision = broker.request(req(), poll_seconds=0.05)
    assert decision.reason == "timeout" and not decision.allowed
    assert time.monotonic() - started < 2


def test_broker_remember_choice():
    ui = RecordingUI()
    broker = ApprovalBroker(Settings(), Policy(), ui)
    first = req()
    threading.Timer(0.1, broker.resolve, args=(first.request_id, Verdict.BLOCK), kwargs={"remember": True}).start()
    assert not broker.request(first).allowed
    second = broker.request(req())          # same server + tool: decided without a popup
    assert second.reason == "remembered" and not second.allowed
    assert len(ui.shown) == 1
    broker.forget_remembered()
    other = req()
    threading.Timer(0.1, broker.resolve, args=(other.request_id, Verdict.ALLOW)).start()
    assert broker.request(other).allowed


def test_broker_overload_and_shutdown():
    broker = ApprovalBroker(Settings(max_pending=2), Policy(), RecordingUI())
    results = []
    threads = [threading.Thread(target=lambda: results.append(broker.request(req()))) for _ in range(2)]
    for t in threads:
        t.start()
    while len(broker.pending_requests()) < 2:
        time.sleep(0.01)
    assert broker.request(req()).reason == "overload"
    broker.shutdown()
    for t in threads:
        t.join(5)
    assert [d.reason for d in results] == ["shutdown", "shutdown"]
    assert broker.request(req()).reason == "shutdown"   # refuses new work after shutdown


def test_broker_resolve_twice_is_harmless():
    broker = ApprovalBroker(Settings(), Policy(), RecordingUI())
    r = req()
    t = threading.Thread(target=broker.request, args=(r,))
    t.start()
    while not broker.pending_requests():
        time.sleep(0.01)
    assert broker.resolve(r.request_id, Verdict.ALLOW)
    assert not broker.resolve(r.request_id, Verdict.BLOCK)
    assert not broker.cancel(r.request_id)
    t.join(2)


# ----------------------------------------------------------------------------- policy


def test_policy_first_match_wins():
    policy = Policy.from_config([
        {"server": "fs", "tool": "read_*", "action": "allow"},
        {"tool": "*delete*", "action": "block"},
        {"action": "bogus"},
    ])
    assert policy.evaluate(req("read_file", "fs")) == "allow"
    assert policy.evaluate(req("read_file", "other")) == "ask"
    assert policy.evaluate(req("delete_all_notes")) == "block"
    assert len(policy.rules) == 2


# ----------------------------------------------------------------------------- jsonrpc


def test_denial_shapes():
    msg = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "rm"}}
    tool_error = jsonrpc.denial_response(msg, "fs", "user")
    assert tool_error["id"] == 5 and tool_error["result"]["isError"] is True
    rpc_error = jsonrpc.denial_response(msg, "fs", "timeout", style="jsonrpc_error")
    assert rpc_error["error"]["code"] == jsonrpc.BLOCKED_CODE
    other = jsonrpc.denial_response({"jsonrpc": "2.0", "id": 1, "method": "resources/read",
                                     "params": {"uri": "file:///x"}}, "fs", "user")
    assert "error" in other and "file:///x" in other["error"]["message"]


def test_request_from_untrusted_dict_is_clipped():
    r = ApprovalRequest.from_dict({"server": "s" * 5000, "method": "tools/call", "target": "t",
                                   "arguments": {"big": "x" * 400_000}, "annotations": "nope"})
    assert len(r.server) <= 200
    assert r.arguments == {"_truncated": "arguments too large to display"}
    assert r.annotations is None
    with pytest.raises(ValueError):
        ApprovalRequest.from_dict({"method": "tools/call"})


# ----------------------------------------------------------------------------- IPC security


def test_ipc_rejects_bad_token_and_browsers(make_daemon):
    daemon = make_daemon(lambda r: (Verdict.ALLOW, 0))
    port = daemon.ipc.port
    body = json.dumps(req().to_dict())

    def status(headers):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/v1/approvals", body=body, headers={"Content-Type": "application/json", **headers})
        s = conn.getresponse().status
        conn.close()
        return s

    token = get_or_create_token()
    assert status({}) == 401
    assert status({"Authorization": "Bearer wrong"}) == 401
    assert status({"Authorization": f"Bearer {token}", "Origin": "https://evil.example"}) == 403
    assert status({"Authorization": f"Bearer {token}", "Host": "evil.example"}) == 403
    assert status({"Authorization": f"Bearer {token}"}) == 200


def test_token_is_stable(guard_home):
    assert get_or_create_token() == get_or_create_token()
    assert len(get_or_create_token()) >= 40


# ----------------------------------------------------------------------------- installer


def test_install_uninstall_round_trip(tmp_path):
    path = tmp_path / "claude_desktop_config.json"
    original = {
        "globalShortcut": "Ctrl+Space",
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/w"],
                           "env": {"X": "1"}},
            "bare": {"command": "my-server"},
            "weird": "not-an-object",
        },
    }
    path.write_text(json.dumps(original))
    change, data = installer.install(path, python="py.exe")
    assert sorted(change.wrapped) == ["bare", "filesystem"]
    fs = data["mcpServers"]["filesystem"]
    assert fs["command"] == "py.exe" and fs["env"] == {"X": "1"}
    assert fs["args"][-4:] == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "C:/w"]
    assert change.backup and change.backup.exists()

    again, _ = installer.install(path, python="py.exe")         # idempotent
    assert again.wrapped == [] and not again.changed

    installer.uninstall(path)
    assert json.loads(path.read_text()) == original


def test_install_adds_demo_and_respects_only(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"mcpServers": {"a": {"command": "a"}, "b": {"command": "b"}}}))
    change, data = installer.install(path, only=["a"], add_demo=True, python="py")
    assert change.added == ["guard-demo"] and change.wrapped == ["a", "guard-demo"]
    assert not installer.is_wrapped(data["mcpServers"]["b"])
    assert installer.is_wrapped(data["mcpServers"]["guard-demo"])


def test_settings_ignore_bad_values(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"fail_mode": "maybe", "approval_timeout_seconds": -1, "max_pending": True,
                                "deny_style": "jsonrpc_error",
                                "gateways": [{"name": "x", "upstream": "http://h"}, {"bad": 1}]}))
    s = Settings.load(path)
    assert s.fail_mode == "closed" and s.approval_timeout_seconds == 120 and s.max_pending == 25
    assert s.deny_style == "jsonrpc_error"
    assert [g.name for g in s.gateways] == ["x"]
