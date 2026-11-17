"""Demo client: drives the proxy exactly the way Claude Desktop does.

``mcp-guard demo --calls 3`` starts ``mcp-guard proxy`` around the demo
stdio server, performs the MCP handshake, then sends several ``tools/call``
requests at the same time. A popup appears for each one; the replies print
as you click, in whatever order you decide them.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Any

CALLS = [
    ("write_note", {"name": "shopping", "content": "milk, eggs, bread"}),
    ("get_time", {}),
    ("delete_all_notes", {}),
    ("add_numbers", {"a": 19, "b": 23}),
    ("read_note", {"name": "shopping"}),
    ("slow_task", {"seconds": 1}),
]


def run_demo(calls: int = 3) -> int:
    cmd = [sys.executable, "-m", "mcp_guard", "proxy", "--server-name", "guard-demo", "--source", "Demo client",
           "--", sys.executable, "-m", "mcp_guard.demo.stdio_server"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    assert proc.stdin and proc.stdout
    replies: dict[int, dict[str, Any]] = {}
    arrived = threading.Condition()

    def reader() -> None:
        assert proc.stdout
        for line in iter(proc.stdout.readline, b""):
            msg = json.loads(line)
            with arrived:
                replies[msg.get("id")] = msg
                arrived.notify_all()

    threading.Thread(target=reader, daemon=True).start()

    def send(msg: dict[str, Any]) -> None:
        assert proc.stdin
        proc.stdin.write(json.dumps(msg).encode() + b"\n")
        proc.stdin.flush()

    def wait_for(ids: set[int], timeout: float) -> None:
        end = time.monotonic() + timeout
        with arrived:
            while not ids <= replies.keys() and time.monotonic() < end:
                arrived.wait(0.2)

    send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
          "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                     "clientInfo": {"name": "mcp-guard-demo-client", "version": "1.0"}}})
    wait_for({0}, 30)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    wait_for({1}, 30)
    print("Connected. Sending", calls, "tool calls at once - answer each popup.\n", flush=True)

    started = time.monotonic()
    pending = {}
    for n in range(calls):
        name, arguments = CALLS[n % len(CALLS)]
        rpc_id = 100 + n
        pending[rpc_id] = name
        send({"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        print(f"  -> sent #{rpc_id} {name}({json.dumps(arguments)})", flush=True)

    shown: set[int] = set()
    deadline = time.monotonic() + 600
    while len(shown) < len(pending) and time.monotonic() < deadline:
        with arrived:
            arrived.wait(0.5)
            ready = [i for i in pending if i in replies and i not in shown]
        for rpc_id in ready:
            shown.add(rpc_id)
            msg = replies[rpc_id]
            result = msg.get("result") or {}
            text = (result.get("content") or [{}])[0].get("text") if result else msg.get("error", {}).get("message")
            status = "BLOCKED" if "MCP Guard" in str(text) else "ALLOWED"
            print(f"  <- #{rpc_id} {pending[rpc_id]:<17} {status:<8} after {time.monotonic() - started:5.1f}s: {text}",
                  flush=True)
    proc.stdin.close()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return 0
