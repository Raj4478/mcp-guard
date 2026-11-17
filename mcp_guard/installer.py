"""Wrap (and unwrap) the MCP servers in ``claude_desktop_config.json``.

Before::

    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/work"]}

After ``mcp-guard install``::

    "filesystem": {"command": "C:/Python313/python.exe",
                   "args": ["-m", "mcp_guard", "proxy", "--server-name", "filesystem", "--",
                            "npx", "-y", "@modelcontextprotocol/server-filesystem", "C:/work"]}

The original command sits after ``--``, so ``mcp-guard uninstall`` restores it
exactly, and installing twice changes nothing. Every write makes a timestamped
backup first and replaces the file atomically. Keys other than
``mcpServers`` are left as they are.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_guard.config import atomic_write_text

MARKER = ["-m", "mcp_guard", "proxy"]
DEMO_SERVER_NAME = "guard-demo"


def candidate_config_paths() -> list[Path]:
    """Where Claude Desktop keeps its config on each OS (first existing wins)."""
    home = Path.home()
    paths: list[Path] = []
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        paths.append(appdata / "Claude" / "claude_desktop_config.json")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        packages = local / "Packages"
        if packages.is_dir():  # Microsoft Store (MSIX) install keeps a virtualised copy
            for pkg in sorted(packages.glob("Claude_*")):
                paths.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    elif sys.platform == "darwin":
        paths.append(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    else:
        paths.append(Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "Claude" / "claude_desktop_config.json")
    return paths


def find_config_path() -> Path:
    candidates = candidate_config_paths()
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def is_wrapped(server: dict[str, Any]) -> bool:
    args = server.get("args") or []
    return isinstance(args, list) and args[:3] == MARKER and "--" in args


def wrap(name: str, server: dict[str, Any], python: str) -> dict[str, Any]:
    wrapped = dict(server)
    wrapped["command"] = python
    wrapped["args"] = [*MARKER, "--server-name", name, "--", server["command"], *(server.get("args") or [])]
    return wrapped


def unwrap(server: dict[str, Any]) -> dict[str, Any]:
    args = list(server.get("args") or [])
    rest = args[args.index("--") + 1:]
    original = dict(server)
    original["command"] = rest[0]
    if len(rest) > 1:
        original["args"] = rest[1:]
    else:
        original.pop("args", None)
    return original


@dataclass
class Change:
    wrapped: list[str] = field(default_factory=list)
    unwrapped: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    backup: Path | None = None

    @property
    def changed(self) -> bool:
        return bool(self.wrapped or self.unwrapped or self.added or self.removed)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not hold a JSON object")
    return data


def _save(path: Path, data: dict[str, Any], change: Change) -> None:
    if path.exists():
        backup = path.with_name(f"{path.name}.mcp-guard-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        change.backup = backup
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def install(path: Path, only: list[str] | None = None, add_demo: bool = False,
            python: str | None = None, dry_run: bool = False) -> tuple[Change, dict[str, Any]]:
    python = python or sys.executable
    data = load_config(path)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be a JSON object")
    change = Change()
    if add_demo and DEMO_SERVER_NAME not in servers:
        servers[DEMO_SERVER_NAME] = {"command": python, "args": ["-m", "mcp_guard.demo.stdio_server"]}
        change.added.append(DEMO_SERVER_NAME)
    if only is not None and add_demo:
        only = [*only, DEMO_SERVER_NAME]  # the demo server is always guarded
    for name, server in list(servers.items()):
        if only and name not in only:
            continue
        if not isinstance(server, dict):
            change.skipped[name] = "not an object"
        elif is_wrapped(server):
            change.skipped[name] = "already guarded"
        elif not isinstance(server.get("command"), str):
            change.skipped[name] = "no 'command' (remote/URL servers need the HTTP gateway)"
        else:
            servers[name] = wrap(name, server, python)
            change.wrapped.append(name)
    if change.changed and not dry_run:
        _save(path, data, change)
    return change, data


def uninstall(path: Path, remove_demo: bool = True, dry_run: bool = False) -> tuple[Change, dict[str, Any]]:
    data = load_config(path)
    servers = data.get("mcpServers") or {}
    change = Change()
    for name, server in list(servers.items()):
        if isinstance(server, dict) and is_wrapped(server):
            servers[name] = unwrap(server)
            change.unwrapped.append(name)
    if remove_demo and DEMO_SERVER_NAME in servers:
        del servers[DEMO_SERVER_NAME]
        change.removed.append(DEMO_SERVER_NAME)
    if change.changed and not dry_run:
        _save(path, data, change)
    return change, data
