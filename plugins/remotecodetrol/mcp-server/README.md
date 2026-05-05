# remotecodetrol-mcp

A Python [FastMCP](https://github.com/jlowin/fastmcp) server that lets Claude
Code talk to the **RemoteCodetrol** iOS app: send notifications, receive
replies, and run a polling loop while the user is away. The full system design
lives in `/Users/zpriddy/.claude/plans/i-want-you-to-golden-yao.md`.

## Why

Corporate DNS blocks Telegram and most consumer push relays. RemoteCodetrol
ships push via APNs through a private Firebase project the user owns end to
end. This MCP is the dev-machine half — Claude calls `send_message`, the user
gets a push on their phone, replies from the iOS app, and Claude polls for the
response.

## Install

### From the bundled Claude Code plugin (recommended)

The repo also ships a Claude Code plugin at `../plugin/` which installs the
MCP server and the matching skill in one step. See `../plugin/README.md`.

### Standalone (just the MCP, no skill)

```bash
# From the repo root
cd mcp-remotecodetrol
uv tool install .
# or
pip install -e .
```

Then register it with Claude Code:

```bash
claude mcp add remotecodetrol -- remotecodetrol-mcp
```

## First-run UX

The first time Claude calls any tool (e.g. `send_message`), the MCP runs the
OAuth 2.0 device-code flow. You'll see a line on stderr like:

```
[remotecodetrol-mcp] Open this URL on your iPhone to authorize:
  https://remotecodetrol.web.app/authorize?user_code=WDJB-MJHT
  (or enter code WDJB-MJHT in the app's Settings -> Authorize new device)
[remotecodetrol-mcp] Authorized as you@example.com.
```

Open the URL on your phone (or type the user_code into Settings → Authorize new
device in the RemoteCodetrol app). The MCP polls for up to 10 minutes; once you
tap Authorize, it stores a refresh token in your macOS Keychain (service
`com.remotecodetrol.mcp`) and continues with the original tool call.

## Configuration

Every variable is optional.

| Variable | Default | Purpose |
| --- | --- | --- |
| `REMOTECODETROL_API_BASE` | `https://us-central1-remotecodetrol.cloudfunctions.net/api` | Backend root (override only for staging). |
| `REMOTECODETROL_THREAD` | _(unset)_ | Default thread for `send_message` when no `thread=` arg and `set_thread` hasn't run. |
| `REMOTECODETROL_DEVICE_LABEL` | `Claude Code on <hostname>` | Shown in the iOS app's Settings → Devices list. |
| `REMOTECODETROL_DEFAULT_POLL_INTERVAL_SECONDS` | `300` | `wait_for_response` poll cadence. |
| `REMOTECODETROL_DEFAULT_TIMEOUT_MINUTES` | `10` | `wait_for_response` timeout. |
| `REMOTECODETROL_KEYCHAIN_SERVICE` | `com.remotecodetrol.mcp` | Keychain service name. |

## Tools

| Tool | Description |
| --- | --- |
| `set_thread(name)` | Pick the active thread for subsequent calls. Persists to `~/.config/remotecodetrol/state.json`. |
| `send_message(body, require_response=False, thread=None, idempotency_key=None)` | Send a message. Returns `{messageId, thread}`. |
| `peek_messages(since_cursor=None, thread=None)` | Return unacked user replies (crash-safe — repeat until you ack). |
| `ack_messages(message_ids, thread=None)` | Mark messages processed. Idempotent. |
| `list_threads()` | List the user's threads with `lastMessageAt`. |
| `wait_for_response(timeout_minutes=10, poll_interval_seconds=300, thread=None)` | Loop peek → ack → return. Empty list on timeout. |
| `whoami()` | Email + active thread (debug). |

### Example

```python
# Inside a Claude Code session, the model would call (paraphrased):
set_thread("dev")
send_message(body="Tests pass. Commit?", require_response=True)
wait_for_response(timeout_minutes=15)
# -> [{"id": "...", "body": "yes — go ahead", "senderType": "user", ...}]
```

## Troubleshooting

- **"refresh failed" then a new device-code prompt**: your refresh token was
  rotated out of the server (e.g. you revoked the device from the iOS app's
  Settings → Devices). Just authorize again on the phone.
- **Token rotation**: refresh tokens rotate on every refresh. The MCP writes
  the new value back to Keychain; if two MCP processes race, the loser will
  fall back to the device-code flow.
- **`No thread set` error**: call `set_thread("name")` once or set
  `REMOTECODETROL_THREAD` in the plugin's MCP env config.
- **Wipe local state**: `keyring delete com.remotecodetrol.mcp <your-email>`
  and `rm ~/.config/remotecodetrol/state.json`.

## Development

```bash
uv sync
uv run pytest
uv run python -m remotecodetrol_mcp.server   # runs over stdio
```

Tests mock both `keyring` and httpx — no live backend calls.
