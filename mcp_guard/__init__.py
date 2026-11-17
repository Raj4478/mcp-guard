"""MCP Guard: watch and approve every MCP request Claude makes.

MCP Guard sits between Claude (Claude Desktop or Claude on the web) and the
MCP servers it talks to. Each tool call is held until you press **Allow** or
**Block** in a popup that stays above your other windows.

Components (see ``docs/ARCHITECTURE.md``):

* :mod:`mcp_guard.proxy`   - stdio proxy that wraps local MCP servers (Claude Desktop)
* :mod:`mcp_guard.gateway` - HTTP gateway for remote MCP servers (Claude Web connectors)
* :mod:`mcp_guard.daemon`  - background service that owns the approval queue and popups
* :mod:`mcp_guard.broker`  - thread-safe approval queue (policy, timeouts, cancel)
* :mod:`mcp_guard.ipc`     - authenticated localhost channel between proxies and the daemon
"""

__version__ = "1.0.0"
