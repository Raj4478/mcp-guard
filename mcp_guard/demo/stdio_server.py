"""Demo MCP server over stdio: ``python -m mcp_guard.demo.stdio_server``.

Each request is answered on its own thread, so a slow tool does not hold up
the others (handy for showing concurrent approvals).
"""

from __future__ import annotations

import json
import sys
import threading

from mcp_guard.demo.tools import handle

_out = threading.Lock()


def _reply(msg: object) -> None:
    response = handle(msg)
    if response is not None:
        with _out:
            sys.stdout.buffer.write(json.dumps(response).encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()


def main() -> int:
    for raw in iter(sys.stdin.buffer.readline, b""):
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            with _out:
                sys.stdout.buffer.write(b'{"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error"}}\n')
                sys.stdout.buffer.flush()
            continue
        items = msg if isinstance(msg, list) else [msg]
        if isinstance(msg, list):  # batch: answer as a batch
            replies = [r for r in (handle(m) for m in items) if r is not None]
            if replies:
                with _out:
                    sys.stdout.buffer.write(json.dumps(replies).encode("utf-8") + b"\n")
                    sys.stdout.buffer.flush()
            continue
        threading.Thread(target=_reply, args=(msg,), daemon=True).start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
