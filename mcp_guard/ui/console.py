"""Headless UIs: a terminal prompt, and auto-allow / auto-block for tests and CI."""

from __future__ import annotations

import queue
import threading
import time
from typing import TYPE_CHECKING

from mcp_guard.models import ApprovalRequest, Decision, Verdict

if TYPE_CHECKING:
    from mcp_guard.broker import ApprovalBroker


class AutoUI:
    """Decides every request at once with a fixed verdict."""

    def __init__(self, verdict: Verdict):
        self.verdict = verdict
        self.broker: ApprovalBroker | None = None

    def attach(self, broker: "ApprovalBroker") -> None:
        self.broker = broker

    def on_new_request(self, req: ApprovalRequest, deadline: float) -> None:
        assert self.broker is not None
        # Decide on another thread: the broker is still inside request() here.
        threading.Thread(target=self.broker.resolve, args=(req.request_id, self.verdict),
                         kwargs={"reason": "auto"}, daemon=True).start()

    def on_request_closed(self, req: ApprovalRequest, decision: Decision) -> None:
        pass

    def show_dashboard(self) -> None:
        pass

    def run(self, stop_event: threading.Event) -> None:
        stop_event.wait()


class ConsoleUI:
    """Asks in the terminal, one request at a time (for machines without a desktop)."""

    def __init__(self) -> None:
        self.broker: ApprovalBroker | None = None
        self._queue: queue.Queue[ApprovalRequest] = queue.Queue()
        self._open: set[str] = set()
        self._lock = threading.Lock()

    def attach(self, broker: "ApprovalBroker") -> None:
        self.broker = broker

    def on_new_request(self, req: ApprovalRequest, deadline: float) -> None:
        with self._lock:
            self._open.add(req.request_id)
        self._queue.put(req)

    def on_request_closed(self, req: ApprovalRequest, decision: Decision) -> None:
        with self._lock:
            self._open.discard(req.request_id)
        print(f"  -> {decision.verdict.value.upper()} {req.server}/{req.target} ({decision.reason})", flush=True)

    def show_dashboard(self) -> None:
        pass

    def run(self, stop_event: threading.Event) -> None:
        assert self.broker is not None
        print("MCP Guard console mode. Waiting for requests (Ctrl+C to stop).", flush=True)
        while not stop_event.is_set():
            try:
                req = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                if req.request_id not in self._open:
                    continue
            print("\n=== MCP request needs approval ===")
            print(f"  time   : {time.strftime('%H:%M:%S', time.localtime(req.created_at))}")
            print(f"  source : {req.source} ({req.client_name or 'unknown client'})")
            print(f"  server : {req.server}")
            print(f"  method : {req.method}")
            print(f"  target : {req.target}")
            print("  args   :\n    " + req.arguments_json(2000).replace("\n", "\n    "))
            try:
                answer = input("Allow this request? [y/N] ").strip().lower()
            except EOFError:
                answer = ""
            verdict = Verdict.ALLOW if answer in ("y", "yes") else Verdict.BLOCK
            if not self.broker.resolve(req.request_id, verdict):
                print("  (already decided - it timed out or was cancelled)")
