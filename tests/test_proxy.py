"""End-to-end: Claude-like client -> real proxy process -> demo server, with the
in-process daemon playing the user."""

from __future__ import annotations

import time

from mcp_guard.models import Verdict

from conftest import is_blocked


def wait_until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "condition not met in time"
        time.sleep(0.05)


def allow_safe(req):
    return (Verdict.BLOCK if req.target == "delete_all_notes" else Verdict.ALLOW), 0.05


def test_allow_forwards_and_block_denies(make_daemon, proxy_client):
    daemon = make_daemon(allow_safe)
    client = proxy_client()
    tools = client.handshake()
    assert {t["name"] for t in tools} >= {"get_time", "delete_all_notes"}

    client.call(1, "add_numbers", {"a": 2, "b": 3})
    reply = client.wait_for(1)
    assert not is_blocked(reply)
    assert reply["result"]["content"][0]["text"] == "5.0"

    client.call(2, "delete_all_notes")
    reply = client.wait_for(2)
    assert is_blocked(reply)
    assert "clicked Block" in reply["result"]["content"][0]["text"]

    # The popup got the details: server, tool, args, client info and tool hints.
    shown = {r.target: r for r in daemon.ui.shown}
    assert shown["add_numbers"].arguments == {"a": 2, "b": 3}
    assert shown["add_numbers"].server == "demo"
    assert shown["add_numbers"].client_name == "claude-ai"
    assert shown["delete_all_notes"].annotations == {"destructiveHint": True}
    assert client.close() == 0


def test_non_tool_traffic_is_not_held(make_daemon, proxy_client):
    daemon = make_daemon(lambda req: (None, 0))   # nobody ever answers
    client = proxy_client()
    client.handshake()
    client.call(1, "get_time")                      # held forever
    client.send({"jsonrpc": "2.0", "id": "p", "method": "ping"})
    assert client.wait_for("p", timeout=5)["result"] == {}   # ping still flows
    wait_until(lambda: len(daemon.broker.pending_requests()) == 1)
    assert not any(isinstance(m, dict) and m.get("id") == 1 for m in client.messages)
    client.close()


def test_concurrent_requests_decided_out_of_order(make_daemon, proxy_client):
    # Request i is decided after (6 - i) * 0.3 s, so the last one sent is answered first.
    delays = {f"n{i}": (6 - i) * 0.3 for i in range(6)}

    def decide(req):
        name = req.arguments["name"]
        return (Verdict.ALLOW if int(name[1]) % 2 == 0 else Verdict.BLOCK), delays[name]

    daemon = make_daemon(decide)
    client = proxy_client()
    client.handshake()
    for i in range(6):
        client.call(100 + i, "write_note", {"name": f"n{i}", "content": "x"})
    replies = {i: client.wait_for(100 + i) for i in range(6)}
    for i, reply in replies.items():
        assert is_blocked(reply) == (i % 2 == 1), (i, reply)
    order = [m["id"] for m in client.messages if isinstance(m, dict) and isinstance(m.get("id"), int)]
    assert order == sorted(order, reverse=True), order   # answered in decision order, not send order
    assert len(daemon.ui.shown) == 6
    client.close()


def test_cancelled_request_is_dropped_and_popup_closed(make_daemon, proxy_client):
    daemon = make_daemon(lambda req: (None, 0))
    client = proxy_client()
    client.handshake()
    client.call(7, "delete_all_notes")
    deadline = time.monotonic() + 10
    while not daemon.broker.pending_requests() and time.monotonic() < deadline:
        time.sleep(0.05)
    client.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}})
    deadline = time.monotonic() + 10
    while daemon.broker.pending_requests() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert daemon.broker.pending_requests() == []
    assert daemon.ui.closed[-1][1].reason == "cancelled"
    time.sleep(0.5)
    assert not any(isinstance(m, dict) and m.get("id") == 7 for m in client.messages)  # no reply, as the spec says
    client.close()


def test_timeout_blocks(make_daemon, proxy_client):
    make_daemon(lambda req: (None, 0), approval_timeout_seconds=1)
    client = proxy_client()
    client.handshake()
    client.call(1, "get_time")
    reply = client.wait_for(1, timeout=10)
    assert is_blocked(reply)
    assert "in time" in reply["result"]["content"][0]["text"]
    client.close()


def test_policy_rules_skip_the_popup(make_daemon, proxy_client):
    daemon = make_daemon(lambda req: (None, 0), rules=[
        {"tool": "get_*", "action": "allow"},
        {"tool": "delete_*", "action": "block"},
    ])
    client = proxy_client()
    client.handshake()
    client.call(1, "get_time")
    client.call(2, "delete_all_notes")
    assert not is_blocked(client.wait_for(1))
    assert "policy" in client.wait_for(2)["result"]["content"][0]["text"]
    assert daemon.ui.shown == []
    client.close()


def test_jsonrpc_error_style(make_daemon, proxy_client):
    make_daemon(lambda req: (Verdict.BLOCK, 0), deny_style="jsonrpc_error")
    client = proxy_client()
    client.handshake()
    client.call(1, "get_time")
    reply = client.wait_for(1)
    assert reply["error"]["code"] == -32001
    assert reply["error"]["data"]["reason"] == "user"
    client.close()


def test_fail_closed_when_daemon_is_down(guard_home, proxy_client):
    from conftest import write_config
    write_config(guard_home, daemon_port=1)       # nothing listens there, autostart off
    client = proxy_client()
    client.handshake()
    client.call(1, "get_time")
    reply = client.wait_for(1)
    assert is_blocked(reply)
    assert "could not be reached" in reply["result"]["content"][0]["text"]
    client.close()


def test_fail_open_when_configured(guard_home, proxy_client):
    from conftest import write_config
    write_config(guard_home, daemon_port=1, fail_mode="open")
    client = proxy_client()
    client.handshake()
    client.call(1, "add_numbers", {"a": 1, "b": 1})
    assert client.wait_for(1)["result"]["content"][0]["text"] == "2.0"
    client.close()


def test_batch_with_a_blocked_call_runs_nothing(make_daemon, proxy_client, tmp_path):
    make_daemon(allow_safe)
    client = proxy_client()
    client.handshake()
    client.send([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "write_note", "arguments": {"name": "b", "content": "x"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete_all_notes", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ])
    replies = {r["id"]: r for r in client.wait_batch()}
    assert replies[1]["error"]["code"] == -32002
    assert is_blocked(replies[2])
    assert replies[3]["error"]["code"] == -32002
    assert not (tmp_path / "notes" / "b.txt").exists()
    client.close()


def test_server_crash_ends_proxy_and_answers_pending(make_daemon, proxy_client):
    daemon = make_daemon(lambda req: (Verdict.ALLOW, 0) if req.target == "crash" else (None, 0))
    client = proxy_client(env={"MCP_GUARD_DEMO_ALLOW_CRASH": "1"})
    client.handshake()
    client.call(1, "slow_task", {"seconds": 0})   # stays held: nobody answers
    deadline = time.monotonic() + 10
    while not daemon.broker.pending_requests() and time.monotonic() < deadline:
        time.sleep(0.05)
    client.call(2, "crash")
    reply = client.wait_for(1)
    assert reply["error"]["code"] == -32003       # held call answered, not left hanging
    assert client.proc.wait(timeout=10) == 3      # proxy exits with the server's code
    deadline = time.monotonic() + 5
    while daemon.broker.pending_requests() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert daemon.broker.pending_requests() == []  # popup closed


def test_client_exit_closes_popup(make_daemon, proxy_client):
    daemon = make_daemon(lambda req: (None, 0))
    client = proxy_client()
    client.handshake()
    client.call(1, "get_time")
    deadline = time.monotonic() + 10
    while not daemon.broker.pending_requests() and time.monotonic() < deadline:
        time.sleep(0.05)
    client.proc.kill()                              # Claude Desktop dies
    deadline = time.monotonic() + 10
    while daemon.broker.pending_requests() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert daemon.broker.pending_requests() == []
    assert daemon.ui.closed[-1][1].reason == "client-disconnected"


def test_bad_command_exits_cleanly(make_daemon, proxy_client):
    make_daemon(lambda req: (Verdict.ALLOW, 0))
    client = proxy_client(server_cmd=["definitely-not-a-real-command-xyz"])
    assert client.proc.wait(timeout=15) == 127
