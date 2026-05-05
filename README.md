# RemoteCodetrol Claude Code plugin

Bundles the `remotecodetrol-mcp` Python MCP server, a matching skill, and a
`/remotecodetrol:link` slash command into a single Claude Code plugin.
Installing the plugin gives Claude seven tools (`send_message`,
`peek_messages`, `ack_messages`, `wait_for_response`, `set_thread`,
`list_threads`, `whoami`, `link`, `logout`) and the operational skill that
tells Claude *when* to use them.

The end result: Claude can ping you on your iPhone via the RemoteCodetrol
iOS app and wait for your reply — useful before destructive ops, after
long-running tasks, or any time it's stuck on a decision.

## Prerequisites

Before installing, make sure you have:

1. **The RemoteCodetrol iOS app** installed via TestFlight on your iPhone, with
   an account that's been allowlisted by the admin. (You should be able to
   sign in with Apple and reach the empty thread list view, not the
   "Account isn't approved yet" screen.)
2. **Claude Code** ([install docs](https://docs.anthropic.com/en/docs/claude-code))
3. **`uv`** for running the Python MCP. The plugin invokes
   `uvx --from <plugin-cache>/mcp-server remotecodetrol-mcp`, which uses
   uv's ephemeral environment cache:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
4. **macOS** — the MCP stores OAuth tokens in macOS Keychain via the
   `keyring` library. (Linux/Windows have keyring backends too but haven't
   been tested.)

## Install

### Option A — from a local checkout (current setup)

If you have this repo cloned locally:

```bash
# Replace this path with wherever you cloned the repo:
PLUGIN_PATH=/path/to/RemoteCodetrolPlugin

claude plugin marketplace add "$PLUGIN_PATH"
claude plugin install remotecodetrol@remotecodetrol
```

### Option B — from a Git host (when published)

Once the repo is pushed to GitHub:

```bash
claude plugin marketplace add github:OWNER/REPO  # subdir auto-detected via .claude-plugin/marketplace.json
claude plugin install remotecodetrol@remotecodetrol
```

### Verify

```bash
claude plugin list
# Look for: remotecodetrol@remotecodetrol  Status: ✔ enabled

claude mcp list
# Look for: plugin:remotecodetrol:bridge  ✓ Connected
```

## First-run authorization

The plugin doesn't have any credentials of its own — it uses an OAuth 2.0
device-code flow against the backend. **First time you ask Claude to do
anything via the `remotecodetrol` MCP**, it will need to be linked.

The cleanest path: start a Claude Code session and run:

```
/remotecodetrol:link
```

Claude will call the `link` tool and show you a `user_code` like
`WDJB-MJHT`. Then on your iPhone:

1. Open the RemoteCodetrol app
2. **Settings → Authorize new device**
3. Enter the code, tap **Confirm**

Claude will pick up the completed authorization on the next tool call (use
`whoami` to verify; you should see your email). The refresh token persists
to macOS Keychain — subsequent sessions are silent until you `logout` or
revoke the device from the iOS app.

## Daily use

```
/remotecodetrol:link              # one-time, or after revoking
```

Then in any session, Claude has tools available. Some useful patterns to
ask Claude to do:

- *"Set my remotecodetrol thread to `release-prep`"*
- *"List my remotecodetrol threads"*
- *"Send a status update to the `release-prep` thread saying X, no need to wait"*
- *"Send a question to `release-prep` and wait up to 5 minutes for my reply"*
  → uses `wait_for_response(timeout_minutes=5)`

The `remotecodetrol` skill (auto-loaded with the plugin) tells Claude *when*
to use these — generally before destructive actions, after long-running
tasks, and when stuck on a decision.

## Configuration

All env vars are optional with sensible defaults:

| Variable | Default | What it does |
|---|---|---|
| `REMOTECODETROL_THREAD` | none | Default thread for `send_message` etc. when no per-call override is set. |
| `REMOTECODETROL_DEVICE_LABEL` | `Claude Code on <hostname>` | Shown in iOS Settings → Devices so you can revoke this MCP later. |
| `REMOTECODETROL_DEFAULT_TIMEOUT_MINUTES` | `10` | Default `wait_for_response` timeout. |
| `REMOTECODETROL_DEFAULT_POLL_INTERVAL_SECONDS` | `300` | Default poll cadence inside `wait_for_response`. |
| `REMOTECODETROL_API_BASE` | `https://us-central1-remotecodetrol.cloudfunctions.net/api` | Override only if you're running your own backend. |

Set them in your shell rc (`~/.zshrc`, `~/.bashrc`):

```bash
export REMOTECODETROL_THREAD=primary
export REMOTECODETROL_DEVICE_LABEL="MacBook Pro (work)"
```

## The slash command and skill

- **`/remotecodetrol:link`** — kicks off the OAuth flow with clear UX (gives Claude the user_code to display, no stderr nonsense).
- **`/remotecodetrol:remotecodetrol`** — pulls the operational skill into the conversation explicitly. Usually you don't need to — Claude auto-loads it based on the description. Useful if you want Claude to re-read the rules.

## Common operations

```
"Show me what thread I'm on"          → calls whoami
"Switch my thread to alerts"          → calls set_thread
"Tell me what threads I have"         → calls list_threads
"Log out of remotecodetrol"           → calls logout (clears tokens; next call needs re-link)
```

## Verifying end-to-end

After `/remotecodetrol:link` succeeds:

```
"Send me a test push on the test thread"
```

Claude calls `send_message`. Within ~5 seconds your phone should show a
push notification. Tap to open the app, navigate to the thread, see the
message rendered.

## Updating

The plugin is versioned. To upgrade after a new release:

```bash
claude plugin uninstall remotecodetrol@remotecodetrol
claude plugin install remotecodetrol@remotecodetrol
```

(Or `claude plugin update` if your Claude Code version supports it.)

If you've made local changes to a checked-out copy, also kill any running
MCP processes so they respawn with the new code:

```bash
pkill -f remotecodetrol-mcp
```

## Uninstall

```bash
claude plugin uninstall remotecodetrol@remotecodetrol
claude plugin marketplace remove remotecodetrol

# Optional: wipe the stored OAuth refresh token from Keychain
keyring delete com.remotecodetrol.mcp <your-email>
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `command not found: uvx` | uv not installed | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, restart shell |
| `claude plugin marketplace add` fails | Path wrong or marketplace.json missing | Verify the path you passed contains `.claude-plugin/marketplace.json` |
| Tool calls return "Not authorized. Run /remotecodetrol:link" | No keychain token | Run the slash command and authorize on phone |
| Tool calls return "Authorization is still pending..." | You called a tool before tapping Confirm in the iOS app | Tap Confirm, then retry the call (or any other tool) |
| Tool calls return `messaging/mismatched-credential` | Backend FCM SA missing role; tell the admin | (Admin-side: grant `roles/firebase.admin` to fanout-sa) |
| Push doesn't arrive on phone | APNs key missing in Firebase, or no device registered | Open the iOS app once to register your FCM token; admin verifies APNs key |
| MCP returns stale data after a code change | An older MCP process is still running in memory | `pkill -f remotecodetrol-mcp`; next tool call respawns fresh |
| Plugin reload doesn't pick up code changes | Plugins cache by version; reinstall to force | `claude plugin uninstall ... && claude plugin install ...` |

For deeper debugging:

```bash
# Tail the MCP server stderr (the device-code URL gets printed here too):
ps aux | grep remotecodetrol-mcp        # find PID
# (Claude Code captures stdio; for stderr-only logs, run the MCP standalone:)
uvx --from /path/to/plugin-cache/mcp-server remotecodetrol-mcp
```
