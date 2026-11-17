"""A tiny MCP server core with harmless demo tools (no SDK needed).

Notes are saved in a sandbox folder (``<guard home>/demo-notes``), so
nothing outside it is ever touched, even by ``delete_all_notes``.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from mcp_guard.config import guard_home

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "mcp-guard-demo", "version": "1.0.0"}
_lock = threading.Lock()


def notes_dir() -> Path:
    path = Path(os.environ.get("MCP_GUARD_DEMO_DIR") or guard_home() / "demo-notes")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _note_path(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name).strip(".") or "note"
    return notes_dir() / f"{safe[:60]}.txt"


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_time",
        "title": "Current time",
        "description": "Return the current date and time on this computer.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "add_numbers",
        "description": "Add two numbers.",
        "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                        "required": ["a", "b"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "write_note",
        "title": "Write a note",
        "description": "Save a text note in the demo notes folder.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["name", "content"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "read_note",
        "description": "Read a note from the demo notes folder.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "delete_all_notes",
        "title": "Delete all notes",
        "description": "Delete every note in the demo notes folder.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"destructiveHint": True},
    },
    {
        "name": "slow_task",
        "description": "Wait the given number of seconds (max 30), then report back.",
        "inputSchema": {"type": "object", "properties": {"seconds": {"type": "number"}}},
    },
]


def _text(text: str, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "get_time":
        return _text(time.strftime("%A %d %B %Y, %H:%M:%S"))
    if name == "add_numbers":
        try:
            return _text(str(float(args["a"]) + float(args["b"])))
        except (KeyError, TypeError, ValueError):
            return _text("a and b must be numbers", True)
    if name == "write_note":
        path = _note_path(str(args.get("name", "")))
        with _lock:
            path.write_text(str(args.get("content", "")), encoding="utf-8")
        return _text(f"Saved note '{path.stem}' ({path.stat().st_size} bytes).")
    if name == "read_note":
        path = _note_path(str(args.get("name", "")))
        if not path.exists():
            return _text(f"No note called '{path.stem}'.", True)
        return _text(path.read_text(encoding="utf-8"))
    if name == "delete_all_notes":
        with _lock:
            files = list(notes_dir().glob("*.txt"))
            for f in files:
                f.unlink()
        return _text(f"Deleted {len(files)} note(s).")
    if name == "slow_task":
        seconds = max(0.0, min(30.0, float(args.get("seconds", 2) or 0)))
        time.sleep(seconds)
        return _text(f"Finished after {seconds:g} s.")
    if name == "crash" and os.environ.get("MCP_GUARD_DEMO_ALLOW_CRASH") == "1":
        os._exit(3)
    return _text(f"Unknown tool: {name}", True)


def handle(msg: Any) -> dict[str, Any] | None:
    """Answer one JSON-RPC message; None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method, rpc_id = msg.get("method"), msg.get("id")
    if "id" not in msg:
        return None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        wanted = params.get("protocolVersion")
        version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": SERVER_INFO,
                  "instructions": "Demo server for MCP Guard. Every tool call is held for approval."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(str(params.get("name")), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}
