"""Small JSON-RPC 2.0 helpers for MCP messages."""

from __future__ import annotations

import json
from typing import Any, Iterable

# Implementation-defined server error range is -32000..-32099.
BLOCKED_CODE = -32001
BATCH_ABORTED_CODE = -32002
SERVER_GONE_CODE = -32003


def is_request(msg: Any) -> bool:
    """A request has a method and an id (notifications have no id)."""
    return isinstance(msg, dict) and isinstance(msg.get("method"), str) and "id" in msg


def is_notification(msg: Any) -> bool:
    return isinstance(msg, dict) and isinstance(msg.get("method"), str) and "id" not in msg


def id_key(rpc_id: Any) -> str:
    """Stable dict key for a JSON-RPC id (keeps 1 and "1" apart)."""
    return json.dumps(rpc_id, sort_keys=True)


def iter_messages(payload: Any) -> Iterable[dict[str, Any]]:
    """Yield the message objects in a single message or a batch."""
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item
    elif isinstance(payload, dict):
        yield payload


def error_response(rpc_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": error}


def denial_response(msg: dict[str, Any], server: str, reason: str, detail: str = "",
                    style: str = "tool_error") -> dict[str, Any]:
    """The reply sent back instead of running a blocked request.

    For ``tools/call`` the default is a normal tool result with ``isError: true``.
    MCP clients pass that text to the model, so Claude can tell the user the
    call was blocked instead of failing with a protocol error. Other methods
    (or ``style="jsonrpc_error"``) get a JSON-RPC error.
    """
    method = msg.get("method", "")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    target = params.get("name") or params.get("uri") or method
    why = {
        "user": "the user clicked Block",
        "policy": "a policy rule blocks it",
        "remembered": "the user chose to always block it",
        "timeout": "nobody approved it in time",
        "dismissed": "the user closed the approval popup",
        "overload": "too many requests were waiting for approval",
        "guard-unavailable": "the approval service could not be reached",
        "shutdown": "MCP Guard is shutting down",
    }.get(reason, reason)
    text = f"MCP Guard blocked '{target}' on server '{server}': {why}."
    if detail:
        text += f" ({detail})"
    if method == "tools/call" and style == "tool_error":
        text += " Do not retry unless the user asks you to."
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"content": [{"type": "text", "text": text}], "isError": True},
        }
    return error_response(msg.get("id"), BLOCKED_CODE, text, {"blockedBy": "mcp-guard", "reason": reason})


def dumps_line(payload: Any) -> bytes:
    """Serialise one message as a newline-terminated line (stdio framing)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
