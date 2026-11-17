"""Append-only audit trail: one JSON line per decision."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from mcp_guard.models import ApprovalRequest, Decision

log = logging.getLogger(__name__)

MAX_BYTES = 5_000_000


class AuditLog:
    def __init__(self, path: Path, log_arguments: bool = False):
        self.path = path
        self.log_arguments = log_arguments
        self._lock = threading.Lock()

    def record(self, req: ApprovalRequest, decision: Decision) -> None:
        args_text = json.dumps(req.arguments, sort_keys=True, default=str)
        entry = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": req.request_id,
            "source": req.source,
            "client": req.client_name,
            "server": req.server,
            "method": req.method,
            "target": req.target,
            "decision": decision.verdict.value,
            "reason": decision.reason,
            "wait_seconds": round(time.time() - req.created_at, 3),
            # A hash lets you match a call to its arguments without storing them.
            "arguments_sha256": hashlib.sha256(args_text.encode("utf-8")).hexdigest(),
        }
        if self.log_arguments:
            entry["arguments"] = req.arguments
        line = json.dumps(entry, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                    os.replace(self.path, self.path.with_suffix(".log.1"))
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError as exc:
                log.warning("audit log write failed: %s", exc)
