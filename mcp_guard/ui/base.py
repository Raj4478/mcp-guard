"""The interface every approval UI implements.

The broker calls ``on_new_request`` and ``on_request_closed`` from worker
threads. ``run`` is called once from the main thread and blocks until
``stop_event`` is set (GUI toolkits such as Tk must own the main thread).
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mcp_guard.broker import ApprovalBroker
    from mcp_guard.models import ApprovalRequest, Decision


class ApprovalUI(Protocol):
    def attach(self, broker: "ApprovalBroker") -> None:
        """Called once before ``run``; the UI resolves requests through the broker."""

    def on_new_request(self, req: "ApprovalRequest", deadline: float) -> None:
        """A request needs a decision. ``deadline`` is a ``time.monotonic()`` value."""

    def on_request_closed(self, req: "ApprovalRequest", decision: "Decision") -> None:
        """A request was decided (by anyone) - close its popup, update history."""

    def show_dashboard(self) -> None:
        """Bring the main window forward (no-op for headless UIs)."""

    def run(self, stop_event: threading.Event) -> None:
        """Block the calling (main) thread until ``stop_event`` is set."""
