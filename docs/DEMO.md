# Demo video script (about 4 minutes)

Record with the Windows Snipping Tool (**Win + Shift + R**, then choose a region) or OBS. Use full screen so it is clear the popup sits above other windows.

## 0. Setup before recording

```bash
cd mcp-guard
python -m pip install -e .
mcp-guard install --add-demo      # adds and guards the "guard-demo" server
```

Quit Claude Desktop completely (tray icon → Quit). Close any other MCP Guard daemon first with `mcp-guard stop`.

## 1. Intro (20 s)

Show the repo README, then `claude_desktop_config.json`, and point out that `guard-demo` now starts `python -m mcp_guard proxy ... -- <real server>`.

## 2. Start the guard (15 s)

```bash
mcp-guard daemon
```

The dashboard window opens (it is empty for now).

## 3. Claude Desktop: approve (60 s)

Open Claude Desktop. Check that the `guard-demo` tools are listed under the tools/connectors button. Ask:

> Use the guard-demo server to save a note called *groceries* with the text "milk, eggs, bread", then read it back.

* The popup appears **on top of Claude**. Point out the source (`Claude Desktop`, client `claude-ai`), the server, the tool, its description and the arguments.
* Click **Allow**. Claude shows "Saved note…".
* A second popup appears for `read_note`. Click **Allow**, and Claude shows the note's text.

## 4. Claude Desktop: block (45 s)

> Now delete all the notes.

* The popup shows the red **destructive** badge. Click **Block**.
* Claude replies that the call was blocked by the user. Ask Claude to read the note again, and it is still there, which proves the delete never reached the server.

## 5. Several requests at once (40 s)

In a terminal:

```bash
mcp-guard demo --calls 3
```

Three popups appear stacked ("1 of 3 waiting"…). Answer them **out of order**: Block, Allow, Allow. Show the terminal printing each result in the order you decided, each matched to its own request.

## 6. Reliability (30 s)

* Open a request, then wait, or show `approval_timeout_seconds` set to 15 in `config.json`: the countdown reaches 0 and the request is blocked.
* Open a request and close the popup with **X**: it is blocked.
* Click **Quit** on the dashboard while a request is pending: it is blocked with reason `shutdown`.
* Show the dashboard history and `%APPDATA%\mcp-guard\audit.log`.

## 7. Claude Web (optional, 60 s; needs a plan with custom connectors and `cloudflared`)

```bash
python -m mcp_guard.demo.http_server --port 9000
mcp-guard daemon --gateway http://127.0.0.1:9000 --gateway-name remote-demo --gateway-secret demo-7f3k9
cloudflared tunnel --url http://127.0.0.1:8765
```

In claude.ai: **Settings → Connectors → Add custom connector** → `https://<tunnel>.trycloudflare.com/demo-7f3k9/mcp`. In a chat, enable it and ask "what time is it? use remote-demo". The popup (source **Claude Web**) appears on your desktop. Show both Allow and Block.

## 8. Wrap-up (15 s)

Run `mcp-guard uninstall` to restore the config, then show the test run: `python -m pytest -q` (34 passed, 1 skipped on Windows).
