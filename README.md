# RemoteCodetrol — Claude Code plugin

> Let Claude Code **send you push notifications on your iPhone** and **wait for your reply** — useful before destructive ops, after long-running tasks, or any time it's stuck on a decision and you're away from your terminal.

This plugin bundles:

- **MCP server** with 13 tools Claude can call: `send_message`, `peek_messages`, `ack_messages`, `get_messages`, `get_last_messages`, `wait_for_response`, `set_thread`, `list_threads`, `list_known_threads`, `forget_thread`, `whoami`, `link`, `complete_link`, `logout`
- **Slash commands** under the `/rc:` prefix — `/rc:link`, `/rc:send_message`, `/rc:send_wait`, `/rc:wait_blocked`, `/rc:peek`, `/rc:ack`, `/rc:get_messages`, `/rc:get_last_messages`
- **`rcct` CLI** — same operations from any shell, talks to the running MCP over a Unix domain socket. Faster than tool-call round-trips and useful in scripts.
- **An operational skill** that auto-loads with the plugin and tells Claude *when* to use each tool

The companion **iOS app** renders Claude's messages with markdown + code blocks, supports selectable response buttons, quick-reply from the notification, and has lock-screen + home-screen widgets that show pending replies.

---

## How it works (mental model)

Claude calls `send_message("Tests pass — should I deploy?", require_response=True)`. The MCP hits the backend, which writes to Firestore, fans out to FCM, and lands a push on your phone within ~5 seconds. You read the push, reply directly from the notification or open the app. Claude's MCP runs a polling consumer that picks up your reply on its next cycle (~60 s idle, ~2 s while actively waiting) and surfaces it to Claude. The push is the *interruption* signal; polling is how Claude *receives* the answer.

---

## Prerequisites

1. **The RemoteCodetrol iOS app** installed on your iPhone, signed in with an allowlisted Apple ID. You should reach the empty thread list — not the "Account isn't approved yet" screen.
2. **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — any version that supports plugins.
3. **[`uv`](https://docs.astral.sh/uv/)** on `PATH` — the plugin runs the MCP server via `uvx`. Install:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
4. **macOS** — primary target. Linux/Windows may work (`uv` and `keyring` are cross-platform) but aren't tested.

---

## Install

Install via the public marketplace over HTTPS (no SSH or auth required — the repo is public):

```bash
claude plugin marketplace add https://github.com/zpriddy/ClaudeRemoteCodetrol.git
claude plugin install rc@remotecodetrol
```

The first argument is the HTTPS URL of this repo. Claude Code clones over HTTPS — no GitHub auth, no SSH key setup, no Personal Access Token needed for a public repo.

Verify:

```bash
claude plugin list
# Should show:  rc@remotecodetrol  Status: ✔ enabled

claude mcp list
# Should show:  plugin:rc:bridge  ✓ Connected
```

The `rcct` CLI installs automatically via a `SessionStart` hook that symlinks `~/.local/bin/rcct` → the plugin's `bin/rcct`. If `~/.local/bin` isn't on your PATH, the hook prints a one-line stderr hint with the export command to add.

### Updating later

```bash
claude plugin uninstall rc@remotecodetrol
claude plugin install rc@remotecodetrol
```

Reinstall is the cleanest update path. If the running MCP process is serving stale code:

```bash
pkill -f remotecodetrol-mcp
```

The next tool call respawns the MCP cleanly.

---

## First-run authorization

The plugin uses an OAuth 2.0 device-code flow against the backend. Run:

```
/rc:link
```

Claude shows a QR code + a `user_code` like `WDJB-MJHT`. Either scan the QR with the iPhone's camera (deep-links straight into the app's authorize sheet) or:

1. Open the RemoteCodetrol iOS app → **Settings → Authorize new device**
2. Enter the code, tap **Confirm**

Claude completes the authorization on the next tool call. The token (single 14-day opaque token, rotated mid-life) is stored at `~/Library/Application Support/RemoteCodetrol/tokens.json` (chmod 0600).

---

## Slash commands

All under the `/rc:` prefix:

| Command | What it does |
|---|---|
| `/rc:link` | OAuth device-code flow — one-time per Claude Code install |
| `/rc:send_message` | Send a message to your phone (fire-and-forget) |
| `/rc:send_wait` | Send + block until you reply (timeout-bounded) |
| `/rc:wait_blocked` | Block waiting for a reply (no send; default 10-min timeout) |
| `/rc:peek` | Look at pending replies WITHOUT acking |
| `/rc:ack` | Manually ack specific message ids |
| `/rc:get_messages` | Fetch all pending replies AND ack them (combined peek + ack) |
| `/rc:get_last_messages` | Fetch the last N user messages including already-acked ones (context recovery) |

**When to use which:**
- Sending: `/rc:send_message` (default) or `/rc:send_wait` (when you can't continue without the answer).
- Reading new replies: `/rc:get_messages` (the 90% case — consume + ack) or `/rc:peek` (read-only check).
- Recovering history: `/rc:get_last_messages` (works on already-acked messages too).
- Waiting: `/rc:wait_blocked` (already sent, need to block) or `/rc:send_wait` (send + block in one call).

---

## CLI (`rcct`)

Auto-installs alongside the MCP via a `SessionStart` hook. Available from any shell once the plugin is loaded once:

```bash
rcct send-message "tests pass — deploy?"        # fire-and-forget
rcct send-wait "deploy now? y/n" --timeout 5    # send + block 5 min
rcct peek                                       # what's pending? (no ack)
rcct get-messages                               # fetch + ack pending
rcct get-last --limit 20                        # last 20 incl acked
rcct ack <message-id>                           # manual ack
rcct wait-blocked --timeout 10                  # block waiting

rcct whoami                                     # who am I logged in as?
rcct link                                       # OAuth device-code flow
rcct logout                                     # clear credentials
rcct threads list                               # show all threads
rcct threads allow <name>                       # add to known set
rcct threads forget <name>                      # remove from known set
```

Existing-from-v0.6 aliases still work: `rcct send` = `rcct send-message`, `rcct check` = `rcct peek`, `rcct wait` = `rcct wait-blocked`.

The CLI talks to the running MCP over a Unix socket (`~/Library/Caches/remotecodetrol/mcp.sock`). Same Python codebase, single implementation, two surfaces. Whatever the MCP tools do, the CLI does.

---

## Configuration

All env vars are optional. Set in `~/.zshrc` / `~/.bashrc`:

| Variable | Default | What it does |
|---|---|---|
| `REMOTECODETROL_THREAD` | none | Default thread when no per-call override |
| `REMOTECODETROL_DEVICE_LABEL` | `Claude Code on <hostname>` | Shown in iOS Settings → Devices |
| `REMOTECODETROL_DEFAULT_TIMEOUT_MINUTES` | `10` | Default `wait_for_response` timeout |
| `REMOTECODETROL_API_BASE` | `https://us-central1-remotecodetrol.cloudfunctions.net/api` | Override for a forked backend |
| `RC_DISABLE_POLLING` | unset | Set to `1` to disable the background polling consumer. Tools fall through to direct per-call API requests. |
| `RC_DISABLE_LEADER` | unset | Set to `1` to disable per-host leader election (every Claude session polls independently). Default is leader-elected so only one MCP per host actually polls. |
| `RC_DISABLE_CLI_SOCKET` | unset | Set to `1` to disable the `rcct` CLI socket. Useful for tests; you lose CLI access. |

### Thread resolution priority

1. Per-call `thread="..."` parameter
2. `set_thread(name)` value (persisted to `~/.config/remotecodetrol/state.json`)
3. `REMOTECODETROL_THREAD` env var

If none are set, send/peek/ack error with a friendly "no thread set" message. Run `rcct threads list` then `rcct threads allow <name>` (which also sets active).

---

## Architecture notes (current state)

- **Polling, not streaming.** v0.6.0 replaced the SSE Cloud Function with a polling consumer. The Cloud Run cost of streaming at containerConcurrency=1 made it untenable; polling at ~60 s cadence (5 s busy, 2 s while actively waiting via `wait=True`, dropping to dormant 5-min cadence after 2 h idle) fits comfortably inside the Firestore + Cloud Functions free tier.
- **Leader-elected polling.** Each Claude Code session spawns its own MCP, but exactly one MCP per host actually runs the poll loop (POSIX `flock` on `~/Library/Caches/remotecodetrol/poll.lock`). Followers serve `peek_messages` via the leader's `pending.json` on disk — no per-session polling overhead.
- **No `peek_messages` cache.** v0.6.2 removed it after long threads exposed truncation issues (polling consumer fetches up to `peekMaxLimit`=100 per cycle; threads with > 100 unacked replies hid some). Every peek goes direct to the API.
- **Plugin name is `rc`.** v0.7.0 renamed from `remotecodetrol` for shorter slash commands. The install slug, marketplace name, and MCP tool prefix all derive from this.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `command not found: uvx` | `uv` not installed or not on PATH | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, restart shell |
| Plugin install fails on `claude plugin marketplace add` | Network / wrong URL | Verify the HTTPS URL: `https://github.com/zpriddy/ClaudeRemoteCodetrol.git`. The repo is public so no auth is needed. |
| Tool returns "Not authorized. Run /rc:link" | No stored token | Run `/rc:link`, authorize on phone |
| Push doesn't arrive on phone | iOS app's FCM token expired, or notifications muted | Open the iOS app once to refresh the FCM token; check iOS Settings → Notifications → RemoteCodetrol |
| `rcct` command not found in shell | `~/.local/bin` isn't on PATH | Add `export PATH="$HOME/.local/bin:$PATH"` to `~/.zshrc` |
| Tool calls return stale data after a code change | Older MCP process still in memory | `pkill -f remotecodetrol-mcp`; next call respawns |
| MCP not connected after install | Plugin update needs a fresh marketplace re-add | `claude plugin marketplace remove remotecodetrol && claude plugin marketplace add https://github.com/zpriddy/ClaudeRemoteCodetrol.git && claude plugin install rc@remotecodetrol` |

For deeper debugging, set `REMOTECODETROL_LOG_LEVEL=DEBUG` and run the MCP directly:

```bash
uvx --from ~/.claude/plugins/cache/remotecodetrol/remotecodetrol/<version>/mcp-server remotecodetrol-mcp
```

---

## Uninstall

```bash
claude plugin uninstall rc@remotecodetrol
claude plugin marketplace remove remotecodetrol

# Optional: wipe stored OAuth tokens
rm -f ~/Library/Application\ Support/RemoteCodetrol/tokens.json

# Optional: forget the active thread
rm -f ~/.config/remotecodetrol/state.json
```

---

## Privacy & data

- The plugin sends only what Claude explicitly passes via tool calls (message bodies, idempotency keys, thread names) plus the OAuth bearer token.
- OAuth tokens live at `~/Library/Application Support/RemoteCodetrol/tokens.json` (chmod 0600) — never on disk in plaintext outside that file.
- The active-thread state file at `~/.config/remotecodetrol/state.json` contains the thread name only.
- The backend is locked to an email allowlist (closed-group beta).
- All transport is HTTPS / TLS 1.2+.

---

## Source

The canonical repo is this one — `https://github.com/zpriddy/ClaudeRemoteCodetrol`. The Python MCP source lives under `plugins/remotecodetrol/mcp-server/`. PRs welcome.

---

## License

MIT.
