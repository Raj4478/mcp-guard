"""Settings, file locations, the shared secret and logging.

Everything MCP Guard stores lives in one per-user folder (``guard_home()``):

* ``config.json``  - user settings (optional; defaults are used for missing keys)
* ``token``        - random secret that proxies use to talk to the daemon
* ``daemon.json``  - port and pid of the running daemon
* ``audit.log``    - one JSON line per decision
* ``logs/``        - rotating log files per component

Set ``MCP_GUARD_HOME`` to use another folder (the tests do this).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "mcp-guard"
DEFAULT_DAEMON_PORT = 47821

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- paths


def guard_home() -> Path:
    """Return (and create) the per-user MCP Guard folder."""
    override = os.environ.get("MCP_GUARD_HOME")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_NAME
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return guard_home() / "config.json"


# --------------------------------------------------------------------------- settings


@dataclass
class GatewaySettings:
    """One HTTP gateway in front of a remote MCP server (for Claude Web)."""

    name: str
    upstream: str
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765
    path_secret: str = ""
    source: str = "Claude Web"


@dataclass
class Settings:
    daemon_port: int = DEFAULT_DAEMON_PORT
    # Seconds a request waits for a click before it is blocked automatically.
    approval_timeout_seconds: float = 120.0
    # "closed": block when the approval service cannot be reached; "open": allow.
    fail_mode: str = "closed"
    # How a blocked tools/call is reported: "tool_error" (a tool result with
    # isError=true, which Claude shows to the model) or "jsonrpc_error".
    deny_style: str = "tool_error"
    # JSON-RPC methods that need approval. tools/call is the one that runs code.
    intercept_methods: list[str] = field(default_factory=lambda: ["tools/call"])
    # Ordered rules, first match wins: {"server": glob, "tool": glob, "method": glob,
    # "source": glob, "action": "allow" | "block" | "ask"}. Default is "ask".
    rules: list[dict[str, Any]] = field(default_factory=list)
    # More pending popups than this are blocked straight away (popup flood guard).
    max_pending: int = 25
    # Start the daemon automatically when a proxy cannot reach it.
    autostart_daemon: bool = True
    # Write tool arguments into audit.log (off by default: they can hold secrets).
    audit_log_arguments: bool = False
    # Ignore clicks on Allow for this long after a popup appears (stops a stray
    # keypress or double-click from approving something you did not read).
    allow_button_delay_ms: int = 700
    gateways: list[GatewaySettings] = field(default_factory=list)
    # Read timeout for upstream servers behind the HTTP gateway.
    gateway_upstream_timeout_seconds: float = 300.0

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        """Load settings, keeping defaults for missing or invalid values."""
        path = path or config_path()
        settings = cls()
        if not path.exists():
            return settings
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Could not read %s (%s); using defaults", path, exc)
            return settings
        if not isinstance(raw, dict):
            log.warning("%s must hold a JSON object; using defaults", path)
            return settings
        settings._apply(raw)
        return settings

    def _apply(self, raw: dict[str, Any]) -> None:
        def take(key: str, kind: type | tuple[type, ...], check=lambda v: True) -> None:
            if key not in raw:
                return
            value = raw[key]
            if isinstance(value, bool) and kind in (int, float, (int, float)):
                log.warning("config %s: expected a number, got %r", key, value)
                return
            if isinstance(value, kind) and check(value):
                setattr(self, key, value)
            else:
                log.warning("config %s: ignoring invalid value %r", key, value)

        take("daemon_port", int, lambda v: 0 <= v <= 65535)
        take("approval_timeout_seconds", (int, float), lambda v: v > 0)
        take("fail_mode", str, lambda v: v in ("closed", "open"))
        take("deny_style", str, lambda v: v in ("tool_error", "jsonrpc_error"))
        take("intercept_methods", list, lambda v: all(isinstance(m, str) for m in v))
        take("rules", list, lambda v: all(isinstance(r, dict) for r in v))
        take("max_pending", int, lambda v: v >= 1)
        take("autostart_daemon", bool)
        take("audit_log_arguments", bool)
        take("allow_button_delay_ms", int, lambda v: 0 <= v <= 10_000)
        take("gateway_upstream_timeout_seconds", (int, float), lambda v: v > 0)
        gateways = raw.get("gateways")
        if isinstance(gateways, list):
            parsed: list[GatewaySettings] = []
            for item in gateways:
                try:
                    parsed.append(GatewaySettings(**item))
                except TypeError as exc:
                    log.warning("config gateways: ignoring %r (%s)", item, exc)
            self.gateways = parsed


DEFAULT_CONFIG_TEMPLATE: dict[str, Any] = {
    "daemon_port": DEFAULT_DAEMON_PORT,
    "approval_timeout_seconds": 120,
    "fail_mode": "closed",
    "deny_style": "tool_error",
    "intercept_methods": ["tools/call"],
    "rules": [
        {"server": "*", "tool": "*", "action": "ask"},
    ],
    "max_pending": 25,
    "autostart_daemon": True,
    "audit_log_arguments": False,
    "allow_button_delay_ms": 700,
    "gateways": [],
}


def write_default_config(path: Path | None = None) -> Path:
    """Create config.json with the defaults if it does not exist yet."""
    path = path or config_path()
    if not path.exists():
        atomic_write_text(path, json.dumps(DEFAULT_CONFIG_TEMPLATE, indent=2) + "\n")
    return path


# --------------------------------------------------------------------------- secret token


def get_or_create_token() -> str:
    """Return the shared secret, creating it on first use.

    Created with O_EXCL so a daemon and several proxies starting at once agree
    on one value. The file sits in the user's own profile folder.
    """
    path = guard_home() / "token"
    for _ in range(50):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            token = path.read_text(encoding="ascii").strip()
            if token:
                return token
            time.sleep(0.02)  # another process is still writing it
            continue
        token = secrets.token_urlsafe(32)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(token)
        return token
    raise RuntimeError(f"token file {path} stays empty")


# --------------------------------------------------------------------------- daemon state


def daemon_state_path() -> Path:
    return guard_home() / "daemon.json"


def write_daemon_state(port: int) -> None:
    state = {"port": port, "pid": os.getpid(), "started_at": time.time()}
    atomic_write_text(daemon_state_path(), json.dumps(state))


def read_daemon_state() -> dict[str, Any] | None:
    try:
        data = json.loads(daemon_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def clear_daemon_state() -> None:
    state = read_daemon_state()
    if state and state.get("pid") == os.getpid():
        try:
            daemon_state_path().unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- helpers


def atomic_write_text(path: Path, text: str) -> None:
    """Write a file so readers never see half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def setup_logging(component: str, level: int = logging.INFO, to_stderr: bool = True) -> None:
    """Log to a rotating file and (optionally) stderr. Never to stdout.

    stdout is the MCP channel for the stdio proxy, so nothing else may write there.
    """
    root = logging.getLogger()
    if getattr(root, "_mcp_guard_configured", False):
        return
    root.setLevel(level)
    fmt = logging.Formatter(f"%(asctime)s [{component}:%(process)d] %(levelname)s %(name)s: %(message)s")
    try:
        log_dir = guard_home() / "logs"
        log_dir.mkdir(exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / f"{component}.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass  # logging must never stop the proxy from working
    if to_stderr and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)
    root._mcp_guard_configured = True  # type: ignore[attr-defined]
