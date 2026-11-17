"""Tk approval popups that stay above every other window.

Tk is in the Python standard library, so there is nothing extra to install.
Tk is not thread-safe: the broker's worker threads only put events on a
queue, and the Tk main loop (main thread) reads that queue every 100 ms.

* One popup per pending request, offset so several are visible at once.
* ``-topmost`` is set when a popup opens and checked every second; it is set
  again only if something removed it (re-setting it constantly makes the
  window blink on Windows).
* Allow is ignored for ``allow_button_delay_ms`` after a popup opens, so a
  stray Enter or double-click cannot approve something unread.
* Esc and the window's close button mean Block. There is no default key
  for Allow.
* A countdown shows when the request will be blocked automatically.
* The dashboard window lists recent decisions; in background mode it starts
  hidden (``mcp-guard show`` brings it back).
"""

from __future__ import annotations

import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING, Any

from mcp_guard.config import Settings
from mcp_guard.models import ApprovalRequest, Decision, Verdict
from mcp_guard.ui import macos

if TYPE_CHECKING:
    from mcp_guard.broker import ApprovalBroker

# One constant for the Tk title and the macOS window matcher, so they cannot drift apart.
POPUP_TITLE = "MCP Guard - approval needed"
FONT = "Segoe UI" if sys.platform == "win32" else "Helvetica"
MONO = "Consolas" if sys.platform == "win32" else "Courier"
GREEN, GREEN_DARK = "#1a7f37", "#116329"
RED, RED_DARK = "#cf222e", "#a40e26"
AMBER = "#9a6700"
MUTED = "#57606a"
HINT_LABELS = {
    "readOnlyHint": ("read-only", GREEN),
    "destructiveHint": ("destructive", RED),
    "idempotentHint": ("idempotent", MUTED),
    "openWorldHint": ("reaches the internet", AMBER),
}


def _enable_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def _lost_topmost(win: tk.Misc) -> bool:
    """True if the window is no longer always-on-top (Windows only).

    The topmost flag persists once set, so this is normally False. Elsewhere
    we have no cheap way to check, and the flag also persists, so return False.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        hwnd = int(win.wm_frame(), 16)          # the outer frame window Tk created
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)   # GWL_EXSTYLE
        return not ex_style & 0x00000008                             # WS_EX_TOPMOST
    except Exception:
        return False


class TkApprovalUI:
    def __init__(self, settings: Settings, background: bool = False, status: str = ""):
        self.settings = settings
        self.background = background
        self.status = status
        self.broker: ApprovalBroker | None = None
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._popups: dict[str, _Popup] = {}
        self.root: tk.Tk | None = None
        self._stop: threading.Event | None = None

    # ------------------------------------------------------------ ApprovalUI API (any thread)

    def attach(self, broker: "ApprovalBroker") -> None:
        self.broker = broker

    def on_new_request(self, req: ApprovalRequest, deadline: float) -> None:
        self._events.put(("new", (req, deadline)))

    def on_request_closed(self, req: ApprovalRequest, decision: Decision) -> None:
        self._events.put(("closed", (req, decision)))

    def show_dashboard(self) -> None:
        self._events.put(("show", None))

    # ------------------------------------------------------------ main thread

    def run(self, stop_event: threading.Event) -> None:
        _enable_dpi_awareness()
        self._stop = stop_event
        self.root = root = tk.Tk()
        macos.make_accessory_app()  # macOS: lets popups appear over full-screen apps
        root.title("MCP Guard")
        root.geometry("860x380")
        root.minsize(640, 260)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
        self._build_dashboard(root)
        root.protocol("WM_DELETE_WINDOW", self._on_dashboard_close)
        if self.background:
            root.withdraw()
        root.after(100, self._pump)
        root.mainloop()
        try:
            root.destroy()
        except tk.TclError:
            pass

    def _build_dashboard(self, root: tk.Tk) -> None:
        top = ttk.Frame(root, padding=(12, 10, 12, 4))
        top.pack(fill="x")
        ttk.Label(top, text="MCP Guard", font=(FONT, 14, "bold")).pack(side="left")
        self._status_var = tk.StringVar(value=self.status)
        ttk.Label(top, textvariable=self._status_var, foreground=MUTED).pack(side="left", padx=12)
        ttk.Button(top, text="Quit", command=self._confirm_quit).pack(side="right")
        ttk.Button(top, text="Hide", command=root.withdraw).pack(side="right", padx=6)
        ttk.Button(top, text="Forget remembered choices", command=self._forget).pack(side="right")

        cols = ("time", "source", "server", "target", "decision", "reason")
        frame = ttk.Frame(root, padding=(12, 4, 12, 12))
        frame.pack(fill="both", expand=True)
        self._tree = ttk.Treeview(frame, columns=cols, show="headings", height=12)
        widths = {"time": 80, "source": 120, "server": 140, "target": 220, "decision": 80, "reason": 140}
        for col in cols:
            self._tree.heading(col, text=col.title())
            self._tree.column(col, width=widths[col], anchor="w")
        self._tree.tag_configure("allow", foreground=GREEN)
        self._tree.tag_configure("block", foreground=RED)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=scroll.set)
        self._tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _pump(self) -> None:
        assert self.root is not None
        if self._stop is not None and self._stop.is_set():
            for popup in list(self._popups.values()):
                popup.destroy()
            self.root.quit()
            return
        try:
            while True:
                kind, data = self._events.get_nowait()
                if kind == "new":
                    self._open_popup(*data)
                elif kind == "closed":
                    self._close_popup(*data)
                elif kind == "show":
                    self.root.deiconify()
                    self.root.lift()
                    self.root.focus_force()
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _open_popup(self, req: ApprovalRequest, deadline: float) -> None:
        if req.request_id in self._popups:
            return
        slot = len(self._popups)
        self._popups[req.request_id] = _Popup(self, req, deadline, slot)
        self._refresh_titles()

    def _close_popup(self, req: ApprovalRequest, decision: Decision) -> None:
        popup = self._popups.pop(req.request_id, None)
        if popup is not None:
            popup.destroy()
        self._refresh_titles()
        stamp = time.strftime("%H:%M:%S")
        self._tree.insert("", 0, values=(stamp, req.source, req.server, req.target or req.method,
                                         decision.verdict.value.upper(), decision.reason),
                          tags=(decision.verdict.value,))
        rows = self._tree.get_children()
        for extra in rows[300:]:
            self._tree.delete(extra)

    def _refresh_titles(self) -> None:
        total = len(self._popups)
        for i, popup in enumerate(self._popups.values(), start=1):
            popup.set_position_text(i, total)
        base = self.status
        self._status_var.set(f"{base} · {total} waiting" if total else base)

    def _decide(self, request_id: str, verdict: Verdict, remember: bool, reason: str = "user") -> None:
        if self.broker is not None:
            self.broker.resolve(request_id, verdict, remember=remember, reason=reason)

    def _forget(self) -> None:
        if self.broker is not None:
            self.broker.forget_remembered()
            messagebox.showinfo("MCP Guard", "Remembered Allow/Block choices were cleared.", parent=self.root)

    def _on_dashboard_close(self) -> None:
        assert self.root is not None
        self.root.withdraw()  # keep running in the background

    def _confirm_quit(self) -> None:
        if messagebox.askyesno("Quit MCP Guard",
                               "Stop MCP Guard? Requests waiting for approval will be blocked, and new "
                               "requests are blocked until it runs again.", parent=self.root):
            if self._stop is not None:
                self._stop.set()


class _Popup:
    """One approval window."""

    def __init__(self, ui: TkApprovalUI, req: ApprovalRequest, deadline: float, slot: int):
        assert ui.root is not None
        self.ui = ui
        self.req = req
        self.deadline = deadline
        self.opened = time.monotonic()
        self.closed = False
        self.win = win = tk.Toplevel(ui.root)
        win.withdraw()  # position it before it is shown, or Windows picks its own spot
        win.title(POPUP_TITLE)
        win.resizable(True, True)
        win.minsize(520, 420)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", lambda: self._decide(Verdict.BLOCK, "dismissed"))
        win.bind("<Escape>", lambda _e: self._decide(Verdict.BLOCK, "user"))
        self._build()
        self._place(slot)
        win.deiconify()
        win.attributes("-topmost", True)
        win.lift()
        if sys.platform == "darwin":
            # Make Tk create the native window now, then let it join every Space
            # (including a full-screen Claude). No focus_force here: activating
            # the app would switch the user away from Claude's Space.
            win.update_idletasks()
            macos.float_over_all_spaces(POPUP_TITLE)
        else:
            try:
                win.focus_force()
            except tk.TclError:
                pass
        try:
            win.bell()
        except tk.TclError:
            pass
        self._tick()

    # ------------------------------------------------------------ layout

    def _build(self) -> None:
        req, win = self.req, self.win
        outer = tk.Frame(win, padx=18, pady=14)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer)
        header.pack(fill="x")
        verb = "run a tool" if req.method == "tools/call" else f"call {req.method}"
        tk.Label(header, text=f"Claude wants to {verb}", font=(FONT, 14, "bold"), anchor="w").pack(side="left")
        self.position_var = tk.StringVar()
        tk.Label(header, textvariable=self.position_var, fg=MUTED, font=(FONT, 9)).pack(side="right")

        grid = tk.Frame(outer, pady=8)
        grid.pack(fill="x")
        client = " ".join(x for x in (req.client_name, req.client_version) if x) or "unknown"
        rows = [
            ("Source", f"{req.source}  (client: {client})"),
            ("MCP server", req.server),
            ("Tool" if req.method == "tools/call" else "Target", req.tool_title and f"{req.target}  ({req.tool_title})"
             or req.target or "-"),
            ("Method", req.method),
            ("Requested", time.strftime("%H:%M:%S", time.localtime(req.created_at))),
        ]
        for r, (label, value) in enumerate(rows):
            tk.Label(grid, text=label, fg=MUTED, font=(FONT, 9), anchor="w").grid(row=r, column=0, sticky="nw", padx=(0, 12))
            tk.Label(grid, text=value, font=(FONT, 10, "bold" if label in ("MCP server", "Tool") else "normal"),
                     anchor="w", justify="left", wraplength=480).grid(row=r, column=1, sticky="w")

        hints = [(text, color) for key, (text, color) in HINT_LABELS.items()
                 if req.annotations and req.annotations.get(key) is True]
        if hints:
            hint_row = tk.Frame(outer)
            hint_row.pack(fill="x", pady=(0, 6))
            tk.Label(hint_row, text="Server says:", fg=MUTED, font=(FONT, 9)).pack(side="left")
            for text, color in hints:
                tk.Label(hint_row, text=f" {text} ", fg="white", bg=color, font=(FONT, 9, "bold")).pack(side="left", padx=4)
        if req.tool_description:
            tk.Label(outer, text=req.tool_description, fg=MUTED, font=(FONT, 9), anchor="w", justify="left",
                     wraplength=560).pack(fill="x", pady=(0, 6))

        tk.Label(outer, text="Arguments", fg=MUTED, font=(FONT, 9), anchor="w").pack(fill="x")
        text_frame = tk.Frame(outer)
        text_frame.pack(fill="both", expand=True, pady=(2, 8))
        text = tk.Text(text_frame, height=9, wrap="word", font=(MONO, 10), relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", req.arguments_json())
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.remember_var = tk.BooleanVar(value=False)
        tk.Checkbutton(outer, text="Remember my choice for this tool until MCP Guard restarts",
                       variable=self.remember_var, font=(FONT, 9)).pack(anchor="w")
        self.countdown_var = tk.StringVar()
        tk.Label(outer, textvariable=self.countdown_var, fg=AMBER, font=(FONT, 9), anchor="w").pack(fill="x", pady=(2, 8))

        buttons = tk.Frame(outer)
        buttons.pack(fill="x")
        common = dict(font=(FONT, 11, "bold"), fg="white", relief="flat", padx=22, pady=7, cursor="hand2", bd=0)
        tk.Button(buttons, text="Block", bg=RED, activebackground=RED_DARK, activeforeground="white",
                  command=lambda: self._decide(Verdict.BLOCK, "user"), **common).pack(side="right")
        self.allow_btn = tk.Button(buttons, text="Allow", bg=GREEN, activebackground=GREEN_DARK,
                                   activeforeground="white", command=self._on_allow, **common)
        self.allow_btn.pack(side="right", padx=10)
        tk.Label(buttons, text="Esc = Block", fg=MUTED, font=(FONT, 8)).pack(side="left")

    def _place(self, slot: int) -> None:
        win = self.win
        win.update_idletasks()
        width, height = max(win.winfo_reqwidth(), 600), max(win.winfo_reqheight(), 460)
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        offset = (slot % 8) * 32
        x = max(0, (sw - width) // 2 + offset)
        y = max(0, (sh - height) // 3 + offset)
        win.geometry(f"{width}x{height}+{x}+{y}")

    def set_position_text(self, index: int, total: int) -> None:
        if not self.closed:
            self.position_var.set(f"{index} of {total} waiting" if total > 1 else "")

    # ------------------------------------------------------------ behaviour

    def _on_allow(self) -> None:
        delay = self.ui.settings.allow_button_delay_ms / 1000
        if time.monotonic() - self.opened < delay:
            return  # too soon after the popup appeared: ignore
        self._decide(Verdict.ALLOW, "user")

    def _decide(self, verdict: Verdict, reason: str) -> None:
        if self.closed:
            return
        self.ui._decide(self.req.request_id, verdict, self.remember_var.get(), reason)
        # The broker reports back through on_request_closed, which destroys the window.

    def _tick(self) -> None:
        if self.closed:
            return
        left = max(0, int(self.deadline - time.monotonic()))
        self.countdown_var.set(f"No answer in {left} s blocks this request automatically.")
        try:
            # Re-apply "always on top" only if something removed it. Setting it
            # (or raising the window) every second makes Windows repaint it,
            # which shows up as a blink.
            if _lost_topmost(self.win):
                self.win.attributes("-topmost", True)
        except tk.TclError:
            return
        self.win.after(1000, self._tick)

    def destroy(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.win.destroy()
        except tk.TclError:
            pass
