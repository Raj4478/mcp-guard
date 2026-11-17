"""Data passed between the interceptors, the broker and the UI."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

MAX_TEXT = 2_000          # cap for free-text fields coming over IPC
MAX_ARGUMENT_BYTES = 256_000


class Verdict(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True)
class Decision:
    """The outcome for one request.

    ``reason`` says who decided: ``user``, ``policy``, ``remembered``,
    ``timeout``, ``dismissed`` (popup closed), ``cancelled`` (the client gave
    up), ``client-disconnected``, ``overload``, ``shutdown``,
    ``guard-unavailable`` or ``error``.
    """

    verdict: Verdict
    reason: str
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict.value, "reason": self.reason, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Decision":
        return cls(Verdict(data["verdict"]), str(data.get("reason", "")), str(data.get("detail", "")))


def _clip(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_target(method: str, params: Any) -> str | None:
    """The thing a request acts on: tool name, resource URI or prompt name."""
    if not isinstance(params, dict):
        return None
    if method == "tools/call":
        return _clip(params.get("name"), 200)
    if method in ("resources/read", "resources/subscribe", "resources/unsubscribe"):
        return _clip(params.get("uri"), 500)
    if method == "prompts/get":
        return _clip(params.get("name"), 200)
    return None


def extract_arguments(method: str, params: Any) -> Any:
    if not isinstance(params, dict):
        return params
    if method in ("tools/call", "prompts/get"):
        return params.get("arguments", {})
    return {k: v for k, v in params.items() if k != "_meta"}


@dataclass
class ApprovalRequest:
    """Everything the popup shows about one pending MCP request."""

    source: str                      # "Claude Desktop", "Claude Web", ...
    server: str                      # MCP server name from the Claude config / gateway
    method: str                      # JSON-RPC method, e.g. "tools/call"
    target: str | None = None        # tool name / resource URI / prompt name
    arguments: Any = None
    client_name: str | None = None   # clientInfo.name from initialize, e.g. "claude-ai"
    client_version: str | None = None
    tool_title: str | None = None
    tool_description: str | None = None
    annotations: dict[str, Any] | None = None   # tool hints: readOnlyHint, destructiveHint...
    rpc_id: str | None = None        # JSON-RPC id, for the logs
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    created_at: float = field(default_factory=time.time)

    @property
    def tool(self) -> str | None:
        return self.target if self.method == "tools/call" else None

    def arguments_json(self, limit: int = 20_000) -> str:
        try:
            text = json.dumps(self.arguments, indent=2, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = repr(self.arguments)
        if len(text) > limit:
            text = text[:limit] + f"\n… ({len(text) - limit} more characters not shown)"
        return text

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ApprovalRequest":
        """Build from untrusted JSON (IPC), clipping every field to a sane size."""
        if not isinstance(data, dict):
            raise ValueError("approval request must be a JSON object")
        method = _clip(data.get("method"), 200)
        server = _clip(data.get("server"), 200)
        if not method or not server:
            raise ValueError("approval request needs 'method' and 'server'")
        arguments = data.get("arguments")
        if len(json.dumps(arguments, default=str)) > MAX_ARGUMENT_BYTES:
            arguments = {"_truncated": "arguments too large to display"}
        annotations = data.get("annotations")
        request = cls(
            source=_clip(data.get("source"), 100) or "unknown",
            server=server,
            method=method,
            target=_clip(data.get("target"), 500),
            arguments=arguments,
            client_name=_clip(data.get("client_name"), 200),
            client_version=_clip(data.get("client_version"), 100),
            tool_title=_clip(data.get("tool_title"), 200),
            tool_description=_clip(data.get("tool_description")),
            annotations=annotations if isinstance(annotations, dict) else None,
            rpc_id=_clip(data.get("rpc_id"), 200),
        )
        if isinstance(data.get("request_id"), str) and 8 <= len(data["request_id"]) <= 64:
            request.request_id = data["request_id"]
        return request
