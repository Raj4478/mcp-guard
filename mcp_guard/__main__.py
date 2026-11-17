"""Allow ``python -m mcp_guard ...``."""

from mcp_guard.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
