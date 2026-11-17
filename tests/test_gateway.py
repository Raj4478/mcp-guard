"""The HTTP gateway in front of a remote MCP server (the Claude Web path)."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from mcp_guard.config import GatewaySettings
from mcp_guard.demo.http_server import DemoHTTPServer
from mcp_guard.gateway import start_gateway
from mcp_guard.models import Verdict

from conftest import is_blocked


@pytest.fixture
def upstream():
    server = DemoHTTPServer(("127.0.0.1", 0))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def gateway(make_daemon, upstream):
    made = []

    def factory(decide, secret="", **config):
        daemon = make_daemon(decide, **config)
        cfg = GatewaySettings(name="remote-demo", upstream=f"http://127.0.0.1:{upstream.port}",
                              listen_port=0, path_secret=secret)
        gw = start_gateway(cfg, daemon.settings, daemon.broker)
        made.append(gw)
        return gw, daemon

    yield factory
    for gw in made:
        gw.shutdown()
        gw.server_close()


def post(port, path, payload, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("POST", path, body=json.dumps(payload),
                 headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                          **(headers or {})})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


def call(rpc_id, name, args=None):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": {"name": name, "arguments": args or {}}}


def init():
    return {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "Anthropic/ClaudeAI", "version": "1.0.0"}}}


def test_streamable_http_allow_and_block(gateway, upstream):
    gw, daemon = gateway(lambda req: (Verdict.ALLOW if req.target == "get_time" else Verdict.BLOCK, 0.05))
    resp, body = post(gw.port, "/mcp", init())
    sid = resp.getheader("Mcp-Session-Id")
    assert resp.status == 200 and sid

    resp, body = post(gw.port, "/mcp", call(1, "get_time"), {"Mcp-Session-Id": sid})
    assert resp.status == 200 and not is_blocked(json.loads(body))

    resp, body = post(gw.port, "/mcp", call(2, "delete_all_notes"), {"Mcp-Session-Id": sid})
    assert is_blocked(json.loads(body))
    assert upstream.calls == ["get_time"]                     # the blocked call never reached the server
    req = next(r for r in daemon.ui.shown if r.target == "delete_all_notes")
    assert req.source == "Claude Web" and req.server == "remote-demo"
    assert req.client_name == "Anthropic/ClaudeAI"            # remembered from initialize


def test_sse_response_is_relayed(gateway):
    gw, _ = gateway(lambda req: (Verdict.ALLOW, 0))
    resp, body = post(gw.port, "/mcp?sse=1", call(1, "add_numbers", {"a": 1, "b": 2}))
    assert resp.getheader("Content-Type") == "text/event-stream"
    data = [line for line in body.decode().splitlines() if line.startswith("data:")][0]
    assert json.loads(data[5:])["result"]["content"][0]["text"] == "3.0"


def test_concurrent_http_calls(gateway, upstream):
    gw, daemon = gateway(lambda req: (Verdict.ALLOW if req.arguments["a"] % 2 == 0 else Verdict.BLOCK,
                                      0.6 - req.arguments["a"] * 0.1))
    results = {}

    def one(i):
        _, body = post(gw.port, "/mcp", call(i, "add_numbers", {"a": i, "b": 0}))
        results[i] = json.loads(body)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert {i: is_blocked(r) for i, r in results.items()} == {i: i % 2 == 1 for i in range(6)}
    assert sorted(upstream.calls) == ["add_numbers"] * 3


def test_secret_path_prefix(gateway):
    gw, _ = gateway(lambda req: (Verdict.ALLOW, 0), secret="s3cr3t")
    resp, _ = post(gw.port, "/mcp", call(1, "get_time"))
    assert resp.status == 404
    resp, body = post(gw.port, "/s3cr3t/mcp", call(1, "get_time"))
    assert resp.status == 200 and not is_blocked(json.loads(body))


def test_legacy_sse_transport_block_is_delivered_on_stream(gateway, upstream):
    gw, _ = gateway(lambda req: (Verdict.ALLOW if req.target == "get_time" else Verdict.BLOCK, 0))
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
    sock.sendall(b"GET /sse HTTP/1.1\r\nHost: localhost\r\nAccept: text/event-stream\r\n\r\n")
    buf = b""

    def read_until(marker: bytes) -> bytes:
        nonlocal buf
        while marker not in buf:
            chunk = sock.recv(4096)
            assert chunk, buf
            buf += chunk
        head, _, buf = buf.partition(marker)
        return head + marker

    read_until(b"\r\n\r\n")                                   # response headers
    endpoint_event = read_until(b"\n\n")
    endpoint = [l for l in endpoint_event.decode().splitlines() if l.startswith("data:")][0][5:].strip()
    assert endpoint.startswith("/messages?sessionId=")

    resp, _ = post(gw.port, endpoint, call(1, "get_time"))
    assert resp.status == 202
    allowed = read_until(b"\n\n")
    assert b'"id": 1' in allowed or b'"id":1' in allowed

    resp, _ = post(gw.port, endpoint, call(2, "delete_all_notes"))
    assert resp.status == 202
    blocked = read_until(b"\n\n")
    payload = json.loads(blocked.decode().split("data: ", 1)[1].split("\n")[0])
    assert payload["id"] == 2 and is_blocked(payload)
    assert upstream.calls == ["get_time"]
    sock.close()


def test_client_disconnect_cancels_popup(gateway):
    gw, daemon = gateway(lambda req: (None, 0))
    sock = socket.create_connection(("127.0.0.1", gw.port), timeout=10)
    body = json.dumps(call(1, "delete_all_notes")).encode()
    sock.sendall(b"POST /mcp HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
                 b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
    deadline = time.monotonic() + 10
    while not daemon.broker.pending_requests() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert daemon.broker.pending_requests()
    sock.close()
    deadline = time.monotonic() + 10
    while daemon.broker.pending_requests() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert daemon.broker.pending_requests() == []
    assert daemon.ui.closed[-1][1].reason == "client-disconnected"


def test_upstream_down_gives_502(make_daemon):
    daemon = make_daemon(lambda req: (Verdict.ALLOW, 0))
    cfg = GatewaySettings(name="dead", upstream="http://127.0.0.1:1", listen_port=0)
    gw = start_gateway(cfg, daemon.settings, daemon.broker)
    try:
        resp, _ = post(gw.port, "/mcp", init())
        assert resp.status == 502
    finally:
        gw.shutdown()
        gw.server_close()
