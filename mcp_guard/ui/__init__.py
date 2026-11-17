"""Approval UIs: Tk popups (default), terminal prompts and auto-decide (tests)."""

from __future__ import annotations

from mcp_guard.config import Settings
from mcp_guard.ui.base import ApprovalUI

UI_CHOICES = ("tk", "console", "allow", "block")


def make_ui(kind: str, settings: Settings, background: bool = False, status: str = "") -> ApprovalUI:
    if kind == "tk":
        from mcp_guard.ui.tk_popup import TkApprovalUI
        return TkApprovalUI(settings, background=background, status=status)
    if kind == "console":
        from mcp_guard.ui.console import ConsoleUI
        return ConsoleUI()
    if kind in ("allow", "block"):
        from mcp_guard.ui.console import AutoUI
        from mcp_guard.models import Verdict
        return AutoUI(Verdict(kind))
    raise ValueError(f"unknown UI {kind!r}; choose from {UI_CHOICES}")
