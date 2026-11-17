"""The background service: approval queue + IPC server + gateways + UI.

One daemon runs per user. It binds ``127.0.0.1:<daemon_port>`` exclusively,
so a second copy notices the first and exits quietly. Shutdown (Ctrl+C,
SIGTERM, ``mcp-guard stop``, Quit in the dashboard) blocks every pending
request before exiting, so nothing is left hanging or silently allowed.
"""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any

from mcp_guard.audit import AuditLog
from mcp_guard.broker import ApprovalBroker
from mcp_guard.config import (GatewaySettings, Settings, clear_daemon_state, get_or_create_token, guard_home,
                              write_daemon_state)
from mcp_guard.gateway import GatewayServer, start_gateway
from mcp_guard.ipc import ApprovalClient, IPCServer
from mcp_guard.policy import Policy
from mcp_guard.ui import make_ui

log = logging.getLogger(__name__)


def run_daemon(settings: Settings, ui_kind: str = "tk", background: bool = False,
               extra_gateways: list[GatewaySettings] | None = None) -> int:
    token = get_or_create_token()
    stop = threading.Event()

    if ApprovalClient(settings, autostart=False).health():
        log.info("MCP Guard is already running on port %s", settings.daemon_port)
        print(f"MCP Guard is already running on port {settings.daemon_port}.")
        return 0

    gateways_cfg = list(settings.gateways) + list(extra_gateways or [])
    status = f"Guarding on 127.0.0.1:{settings.daemon_port}"
    if gateways_cfg:
        status += " · gateways: " + ", ".join(f"{g.name}:{g.listen_port}" for g in gateways_cfg)
    ui = make_ui(ui_kind, settings, background=background, status=status)
    broker = ApprovalBroker(settings, Policy.from_config(settings.rules), ui,
                            AuditLog(guard_home() / "audit.log", settings.audit_log_arguments))
    ui.attach(broker)

    def request_stop() -> None:
        stop.set()

    try:
        ipc = IPCServer(settings.daemon_port, broker, token, ui, on_shutdown=request_stop)
    except OSError as exc:
        log.error("cannot listen on 127.0.0.1:%s (%s). Is another program using it? "
                  "Set daemon_port in %s", settings.daemon_port, exc, guard_home() / "config.json")
        return 2
    write_daemon_state(ipc.port)
    threading.Thread(target=ipc.serve_forever, name="ipc", daemon=True).start()
    log.info("MCP Guard daemon listening on 127.0.0.1:%s (ui=%s)", ipc.port, ui_kind)

    gateways: list[GatewayServer] = []
    for cfg in gateways_cfg:
        try:
            gateways.append(start_gateway(cfg, settings, broker))
        except (OSError, ValueError) as exc:
            log.error("gateway %r not started: %s", cfg.name, exc)

    def on_signal(signum: int, _frame: Any) -> None:
        log.info("signal %s: stopping", signum)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass

    try:
        ui.run(stop)  # blocks the main thread until stop is set
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("UI crashed; blocking all pending requests and stopping")
    finally:
        stop.set()
        broker.shutdown()
        for gw in gateways:
            gw.shutdown()
            gw.server_close()
        ipc.shutdown()
        ipc.server_close()
        clear_daemon_state()
        log.info("MCP Guard daemon stopped")
    return 0
