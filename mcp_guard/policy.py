"""Rules that decide some requests without asking.

Rules come from ``config.json`` and are checked in order; the first match wins.
Every field is a glob (``*`` and ``?``); missing fields match anything::

    "rules": [
      {"server": "filesystem", "tool": "read_*", "action": "allow"},
      {"tool": "delete_*", "action": "block"},
      {"action": "ask"}
    ]
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from mcp_guard.models import ApprovalRequest

log = logging.getLogger(__name__)

ACTIONS = ("allow", "block", "ask")


@dataclass(frozen=True)
class Rule:
    action: str
    server: str = "*"
    tool: str = "*"
    method: str = "*"
    source: str = "*"

    def matches(self, req: ApprovalRequest) -> bool:
        pairs = (
            (self.server, req.server),
            (self.tool, req.target or ""),
            (self.method, req.method),
            (self.source, req.source),
        )
        return all(fnmatch.fnmatchcase(value or "", pattern) for pattern, value in pairs)


class Policy:
    def __init__(self, rules: Iterable[Rule] = ()):
        self.rules = list(rules)

    @classmethod
    def from_config(cls, raw_rules: Iterable[dict[str, Any]]) -> "Policy":
        rules = []
        for raw in raw_rules:
            action = str(raw.get("action", "")).lower()
            if action not in ACTIONS:
                log.warning("policy: ignoring rule with unknown action: %r", raw)
                continue
            rules.append(Rule(
                action=action,
                server=str(raw.get("server", "*")),
                tool=str(raw.get("tool", "*")),
                method=str(raw.get("method", "*")),
                source=str(raw.get("source", "*")),
            ))
        return cls(rules)

    def evaluate(self, req: ApprovalRequest) -> str:
        """Return "allow", "block" or "ask" (the default)."""
        for rule in self.rules:
            if rule.matches(req):
                return rule.action
        return "ask"
