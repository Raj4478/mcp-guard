"""Command line: ``mcp-guard <command>`` (or ``python -m mcp_guard <command>``).

Commands
  daemon      run the approval service (popups, IPC, HTTP gateways)
  proxy       stdio proxy around one MCP server (Claude Desktop runs this)
  gateway     standalone HTTP gateway that asks a running daemon
  install     wrap the servers in claude_desktop_config.json
  uninstall   restore claude_desktop_config.json
  status      is the daemon running? what is pending?
  show        open the dashboard window
  stop        stop the daemon
  init-config write config.json with the defaults and print its path
  demo        fire concurrent tool calls through the proxy (no Claude needed)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading

from mcp_guard import __version__
from mcp_guard.config import GatewaySettings, Settings, config_path, setup_logging, write_default_config


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-guard", description="Approve or block every MCP request Claude makes.")
    p.add_argument("--version", action="version", version=f"mcp-guard {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("daemon", help="run the approval service")
    d.add_argument("--ui", choices=("tk", "console", "allow", "block"), default="tk",
                   help="tk popups (default), console prompts, or auto allow/block (testing)")
    d.add_argument("--background", action="store_true", help="start with the dashboard hidden")
    d.add_argument("--port", type=int, help="IPC port (default from config, 47821)")
    d.add_argument("--gateway", metavar="URL", help="also run an HTTP gateway in front of this MCP server URL")
    d.add_argument("--gateway-name", default="remote", help="server name shown in popups")
    d.add_argument("--gateway-port", type=int, default=8765)
    d.add_argument("--gateway-host", default="127.0.0.1")
    d.add_argument("--gateway-secret", default="", help="secret first path segment for the public URL")

    x = sub.add_parser("proxy", help="stdio proxy (used in claude_desktop_config.json)")
    x.add_argument("--server-name", required=True)
    x.add_argument("--source", default="Claude Desktop")
    x.add_argument("server_command", nargs=argparse.REMAINDER, help="-- <command> [args...]")

    g = sub.add_parser("gateway", help="standalone HTTP gateway (asks the daemon over IPC)")
    g.add_argument("--upstream", required=True, help="URL of the real MCP server, e.g. https://example.com")
    g.add_argument("--name", default="remote")
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--secret", default="")
    g.add_argument("--source", default="Claude Web")

    i = sub.add_parser("install", help="guard the MCP servers in claude_desktop_config.json")
    i.add_argument("--config", help="path to claude_desktop_config.json")
    i.add_argument("--servers", help="comma-separated server names (default: all)")
    i.add_argument("--add-demo", action="store_true", help=f"also add the '{'guard-demo'}' demo server")
    i.add_argument("--dry-run", action="store_true")

    u = sub.add_parser("uninstall", help="restore claude_desktop_config.json")
    u.add_argument("--config")
    u.add_argument("--keep-demo", action="store_true")
    u.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="daemon status and pending requests")
    sub.add_parser("show", help="show the dashboard window")
    sub.add_parser("stop", help="stop the daemon")
    sub.add_parser("init-config", help="write config.json with defaults")

    m = sub.add_parser("demo", help="send concurrent tool calls through the proxy, like Claude Desktop would")
    m.add_argument("--calls", type=int, default=3)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    settings = Settings.load()

    if args.command == "daemon":
        setup_logging("daemon", level)
        if args.port is not None:
            settings.daemon_port = args.port
        extra = []
        if args.gateway:
            extra.append(GatewaySettings(name=args.gateway_name, upstream=args.gateway, listen_host=args.gateway_host,
                                         listen_port=args.gateway_port, path_secret=args.gateway_secret))
        from mcp_guard.daemon import run_daemon
        return run_daemon(settings, ui_kind=args.ui, background=args.background, extra_gateways=extra)

    if args.command == "proxy":
        setup_logging(f"proxy-{_safe(args.server_name)}", level)  # stderr goes to Claude's MCP log
        command = list(args.server_command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            print("mcp-guard proxy: give the server command after --", file=sys.stderr)
            return 2
        from mcp_guard.proxy import run_proxy
        return run_proxy(args.server_name, command, settings, args.source)

    if args.command == "gateway":
        setup_logging(f"gateway-{_safe(args.name)}", level)
        from mcp_guard.gateway import start_gateway
        from mcp_guard.ipc import ApprovalClient
        cfg = GatewaySettings(name=args.name, upstream=args.upstream, listen_host=args.host,
                              listen_port=args.port, path_secret=args.secret, source=args.source)
        server = start_gateway(cfg, settings, ApprovalClient(settings))
        print(f"Gateway '{cfg.name}' on http://{args.host}:{server.port}{server.secret_prefix} -> {args.upstream}")
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0

    if args.command in ("install", "uninstall"):
        from mcp_guard import installer
        path = installer.find_config_path() if not args.config else __import__("pathlib").Path(args.config)
        if args.command == "install":
            only = [s.strip() for s in args.servers.split(",")] if args.servers else None
            change, data = installer.install(path, only=only, add_demo=args.add_demo, dry_run=args.dry_run)
        else:
            change, data = installer.uninstall(path, remove_demo=not args.keep_demo, dry_run=args.dry_run)
        print(f"Claude Desktop config: {path}")
        for label, names in (("guarded", change.wrapped), ("restored", change.unwrapped),
                             ("added", change.added), ("removed", change.removed)):
            for name in names:
                print(f"  {label:9} {name}")
        for name, why in change.skipped.items():
            print(f"  skipped   {name}: {why}")
        if args.dry_run:
            print(json.dumps(data.get("mcpServers", {}), indent=2))
        elif change.changed:
            print(f"Backup: {change.backup}" if change.backup else "Created a new config file.")
            print("Restart Claude Desktop (quit it from the tray icon) to apply.")
        else:
            print("Nothing to change.")
        return 0

    if args.command in ("status", "show", "stop"):
        from mcp_guard.ipc import ApprovalClient, GuardUnavailable
        client = ApprovalClient(settings, autostart=False)
        if not client.health():
            print(f"MCP Guard daemon is not running (port {client.port()}).")
            return 1
        try:
            if args.command == "status":
                pending = client.pending()
                print(f"MCP Guard daemon is running on 127.0.0.1:{client.port()}. {len(pending)} pending.")
                for req in pending:
                    print(f"  {req['request_id'][:8]}  {req['source']:<15} {req['server']}/{req['target']}")
            elif args.command == "show":
                client.show_dashboard()
            else:
                client.shutdown()
                print("Stopping MCP Guard daemon.")
        except GuardUnavailable as exc:
            print(f"error: {exc}")
            return 1
        return 0

    if args.command == "init-config":
        print(write_default_config())
        return 0

    if args.command == "demo":
        from mcp_guard.demo.client import run_demo
        return run_demo(args.calls)

    return 2


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40]


if __name__ == "__main__":
    raise SystemExit(main())
