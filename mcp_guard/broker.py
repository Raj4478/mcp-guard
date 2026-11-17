"""The approval queue: one place where every pending request waits.

Interceptors (stdio proxy over IPC, HTTP gateway in-process) call
:meth:`ApprovalBroker.request`, which blocks the calling thread until one of:

* a policy rule or a remembered choice decides at once,
* the user clicks Allow/Block (UI thread calls :meth:`resolve`),
* the timeout passes (blocked),
* the requester goes away or cancels (:meth:`cancel` / ``is_alive``),
* the daemon shuts down (everything pending is blocked).

Each pending request has its own ``threading.Event``, so any number of
requests can wait at the same time and be decided in any order.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from mcp_guard.audit import AuditLog
from mcp_guard.config import Settings
from mcp_guard.models import ApprovalRequest, Decision, Verdict
from mcp_guard.policy import Policy
from mcp_guard.ui.base import ApprovalUI

log = logging.getLogger(__name__)


@dataclass
class _Pending:
    req: ApprovalRequest
    deadline: float
    event: threading.Event = field(default_factory=threading.Event)
    decision: Decision | None = None


@dataclass(frozen=True)
class HistoryEntry:
    req: ApprovalRequest
    decision: Decision
    decided_at: float


class ApprovalBroker:
    def __init__(self, settings: Settings, policy: Policy, ui: ApprovalUI | None = None,
                 audit: AuditLog | None = None):
        self.settings = settings
        self.policy = policy
        self.ui = ui
        self.audit = audit
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._remembered: dict[tuple[str, str, str], Verdict] = {}
        self._closed = False
        self.history: deque[HistoryEntry] = deque(maxlen=500)

    # ------------------------------------------------------------------ public API

    def request(self, req: ApprovalRequest, is_alive: Callable[[], bool] | None = None,
                poll_seconds: float = 0.25) -> Decision:
        """Block until ``req`` is decided and return the decision."""
        pending = self._submit(req)
        while not pending.event.wait(poll_seconds):
            if time.monotonic() >= pending.deadline:
                self._finish(req.request_id, Decision(Verdict.BLOCK, "timeout"))
            elif is_alive is not None and not _safe_call(is_alive):
                self._finish(req.request_id, Decision(Verdict.BLOCK, "client-disconnected"))
        assert pending.decision is not None
        return pending.decision

    def resolve(self, request_id: str, verdict: Verdict, remember: bool = False,
                reason: str = "user") -> bool:
        """Decide a pending request (called by the UI). False if it was already decided."""
        with self._lock:
            pending = self._pending.get(request_id)
            if pending and remember:
                self._remembered[_remember_key(pending.req)] = verdict
        if pending is None:
            return False
        return self._finish(request_id, Decision(verdict, reason))

    def cancel(self, request_id: str, reason: str = "cancelled") -> bool:
        """The requester no longer wants an answer (e.g. notifications/cancelled)."""
        return self._finish(request_id, Decision(Verdict.BLOCK, reason))

    def pending_requests(self) -> list[ApprovalRequest]:
        with self._lock:
            return [p.req for p in self._pending.values()]

    def forget_remembered(self) -> None:
        with self._lock:
            self._remembered.clear()

    def shutdown(self) -> None:
        """Block everything still waiting and refuse new requests."""
        with self._lock:
            self._closed = True
            ids = list(self._pending)
        for request_id in ids:
            self._finish(request_id, Decision(Verdict.BLOCK, "shutdown"))

    # ------------------------------------------------------------------ internals

    def _submit(self, req: ApprovalRequest) -> _Pending:
        immediate: Decision | None = None
        action = self.policy.evaluate(req)
        deadline = time.monotonic() + self.settings.approval_timeout_seconds
        pending = _Pending(req, deadline)
        with self._lock:
            if self._closed:
                immediate = Decision(Verdict.BLOCK, "shutdown")
            elif action == "allow":
                immediate = Decision(Verdict.ALLOW, "policy")
            elif action == "block":
                immediate = Decision(Verdict.BLOCK, "policy")
            elif _remember_key(req) in self._remembered:
                immediate = Decision(self._remembered[_remember_key(req)], "remembered")
            elif len(self._pending) >= self.settings.max_pending:
                immediate = Decision(Verdict.BLOCK, "overload",
                                     f"more than {self.settings.max_pending} requests waiting")
            else:
                self._pending[req.request_id] = pending
        if immediate is not None:
            pending.decision = immediate
            pending.event.set()
            self._record(req, immediate)
            return pending
        log.info("waiting for approval: %s %s/%s (%s)", req.request_id[:8], req.server, req.target, req.source)
        if self.ui is not None:
            try:
                self.ui.on_new_request(req, deadline)
            except Exception:  # a broken UI must not leave the request hanging
                log.exception("UI failed to show request; blocking it")
                self._finish(req.request_id, Decision(Verdict.BLOCK, "error", "approval UI failed"))
        return pending

    def _finish(self, request_id: str, decision: Decision) -> bool:
        with self._lock:
            pending = self._pending.pop(request_id, None)
        if pending is None:
            return False
        pending.decision = decision
        pending.event.set()
        self._record(pending.req, decision)
        return True

    def _record(self, req: ApprovalRequest, decision: Decision) -> None:
        log.info("%s %s %s/%s (%s)", decision.verdict.value.upper(), req.request_id[:8],
                 req.server, req.target, decision.reason)
        self.history.append(HistoryEntry(req, decision, time.time()))
        if self.audit is not None:
            self.audit.record(req, decision)
        if self.ui is not None:
            try:
                self.ui.on_request_closed(req, decision)
            except Exception:
                log.exception("UI failed to close request")


def _remember_key(req: ApprovalRequest) -> tuple[str, str, str]:
    return (req.server, req.method, req.target or "")


def _safe_call(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:
        return False
