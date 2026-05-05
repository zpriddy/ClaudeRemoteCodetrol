# RemoteCodetrol — Claude Code plugin

> Let Claude Code **send you push notifications on your iPhone** and **wait for your reply** — useful before destructive ops, after long-running tasks, or any time it's stuck on a decision and you're away from your terminal.

This plugin bundles three things into one Claude Code install:

- **9 MCP tools** Claude can call: `send_message`, `peek_messages`, `ack_messages`, `wait_for_response`, `set_thread`, `list_threads`, `whoami`, `link`, `logout`
- **An operational skill** — `remotecodetrol` — that tells Claude *when* to use the tools (before destructive actions, after long tasks, etc.). Auto-loads with the plugin.
- **A `/remotecodetrol:link` slash command** for the one-time OAuth device-code authorization

The companion **iOS app** (RemoteCodetrol) renders Claude's messages with markdown + code blocks and lets you reply — replies surface to Claude on its next `peek_messages` call.

---

## How it works (one-paragraph mental model)

Claude calls `send_message("Tests pass — should I deploy?", require_response=True)`. The MCP hits the backend, which writes to Firestore, fans out to FCM, and lands a push on your phone within ~5 seconds. You read the push, open the iOS app, type "yes" or "rebase first," tap send. Claude is now polling — in `wait_for_response` it sees your reply on its next ~5-min poll, acks it, and continues with your input as the next user prompt. The push is the *interruption* signal; the polling loop is how Claude *receives* your answer.

---

## Prerequisites

Before installing, make sure you have:

1. **The RemoteCodetrol iOS app** installed on your iPhone (TestFlight invite from the project admin), signed in with an Apple ID that's been added to the allowlist. You should be able to sign in and reach the empty thread list view, *not* the "Account isn't approved yet" screen.
2. **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** ≥ recent version (any version that supports plugins).
3. **[`uv`](https://docs.astral.sh/uv/)** on `PATH` — the plugin runs the MCP server via `uvx`, which uses uv's ephemeral env cache. Install:
   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
4. **macOS** — the MCP stores OAuth tokens in macOS Keychain via `keyring`. Linux/Windows are likely fine (`keyring` has cross-platform backends) but haven't been tested.

---

## Install

```bash
claude plugin marketplace add github:zpriddy/ClaudeRemoteCodetrol
claude plugin install remotecodetrol@remotecodetrol
```

That's it for install. Verify:

```bash
claude plugin list
# Should show:  remotecodetrol@remotecodetrol  Status: ✔ enabled

claude mcp list
# Should show:  plugin:remotecodetrol:bridge  ✓ Connected
```

If `mcp list` shows `✗ Failed`, jump to **Troubleshooting** below.

### Updating later

```bash
claude plugin uninstall remotecodetrol@remotecodetrol
claude plugin install remotecodetrol@remotecodetrol
```

(Reinstall is the cleanest update path. Some Claude Code versions also support `claude plugin update`.)

If the running MCP process is serving stale code after a reinstall:
```bash
pkill -f remotecodetrol-mcp
```
The next tool call will respawn the MCP cleanly with the new code.

---

## First-run authorization

The plugin doesn't ship credentials — it uses an OAuth 2.0 device-code flow against the backend. Run the slash command:

```
/remotecodetrol:link
```

Claude calls the `link` tool. You'll see something like:

> **Open the RemoteCodetrol iOS app → Settings → "Authorize new device" → enter code `WDJB-MJHT` → tap Confirm.**

Do that. The `user_code` is valid for 10 minutes. Once you tap Confirm in the app, Claude can call any other tool (e.g., `whoami` to verify) — the next call detects the completed authorization and silently exchanges the device code for tokens. The refresh token is stored in macOS Keychain (`com.remotecodetrol.mcp` service), so subsequent Claude Code sessions are silent until you `logout` or revoke the device from the iOS app.

---

## Daily use

Once linked, the plugin "just works" — talk to Claude in natural language and it'll call the right tools based on the operational skill. Useful things to ask Claude:

| Ask Claude | Tool used |
|---|---|
| "Set my remotecodetrol thread to `release-prep`" | `set_thread` |
| "List my remotecodetrol threads" | `list_threads` |
| "Send a status update to `release-prep`: tests pass — no need to wait" | `send_message(require_response=False)` |
| "Send a question to `release-prep` and wait up to 5 minutes for my reply" | `send_message(require_response=True)` + `wait_for_response(timeout_minutes=5)` |
| "Who am I logged in as on remotecodetrol?" | `whoami` |
| "Log me out of remotecodetrol" | `logout` |
| "Re-authorize remotecodetrol" | `/remotecodetrol:link` (slash command) |

Or you can call tools directly through Claude's natural-language interface — the operational skill auto-loads at session start so Claude already knows the rules of when to push and when to poll.

---

## Tool reference

All tools live under the `mcp__plugin_remotecodetrol_bridge__` namespace. Claude calls them by name; you mostly don't.

### `link()` *(slash command: `/remotecodetrol:link`)*
Start the OAuth device-code flow. Returns `user_code` and `verification_uri` for you to enter in the iOS app. After you authorize, the next tool call completes the link silently. If already linked, returns `status: "already_linked"` with your email — no action needed.

### `whoami()`
Returns `{ email, default_thread }`. Forces a token validation pass. Useful for confirming the link is healthy and seeing what thread you're targeting.

### `logout()`
Clears all credentials: cached access token, keychain refresh token, and any pending device-code state. The next tool call requires a fresh `link()`.

### `set_thread(name)`
Sets the active thread for subsequent send/peek/ack calls in this and future sessions. Persisted to `~/.config/remotecodetrol/state.json`. Read fresh on every tool call so multiple Claude Code sessions stay in sync if one of them changes the thread.

### `list_threads()`
Returns all threads owned by the current user with their `lastMessageAt` timestamp. Threads are namespaced per user, so two different users can each have a thread named `alerts` without collision.

### `send_message(body, require_response=False, thread=None, idempotency_key=None)`
Sends a markdown-rendered message to the iOS app on the named thread (or the active thread if none given). The push arrives within ~5 seconds. Use `require_response=True` to signal that Claude is awaiting input — this surfaces in the iOS UI as an "Awaiting your response" badge.

`idempotency_key` is optional but recommended in retry-prone code: send the same key twice and the backend returns 409 instead of duplicating.

### `peek_messages(since_cursor=None, thread=None)`
Returns user replies on the named thread that haven't been acked yet. **Crash-safe**: messages stay returnable until you `ack_messages` them, so a Claude crash mid-processing doesn't drop replies. The optional `since_cursor` advances past previously-seen replies for paginated reading.

### `ack_messages(message_ids: list[str], thread=None)`
Marks one or more user messages as processed. Idempotent — already-acked IDs are no-ops. After acking, those messages won't show up in subsequent `peek_messages` calls. The iOS app's checkmark on each message animates from gray (sent) to blue (acked) when this runs.

### `wait_for_response(timeout_minutes=10, poll_interval_seconds=300, thread=None)`
The convenience helper for the polling pattern: loops `peek_messages` → `ack_messages` → return as soon as a reply arrives, OR returns an empty list on timeout. Use this when Claude needs a synchronous "wait for the user's answer" semantic. On timeout it returns gracefully (empty `messages` list) rather than throwing — Claude can decide whether to push again, summarize state and stop, or proceed with a default.

---

## Configuration

All env vars are optional with sensible defaults. Set them in your shell rc (`~/.zshrc`, `~/.bashrc`):

| Variable | Default | What it does |
|---|---|---|
| `REMOTECODETROL_THREAD` | *(none)* | Default thread for `send_message`/`peek`/`ack` when no per-call `thread=` is given. Lower priority than `set_thread` (which writes to disk). |
| `REMOTECODETROL_DEVICE_LABEL` | `Claude Code on <hostname>` | Shown in iOS Settings → Devices so you can revoke this MCP install later. Useful if you authorize from multiple machines (e.g. `MacBook Pro (work)` vs `Mac mini (home)`). |
| `REMOTECODETROL_DEFAULT_TIMEOUT_MINUTES` | `10` | Default `wait_for_response` timeout. Override per call with `wait_for_response(timeout_minutes=...)`. |
| `REMOTECODETROL_DEFAULT_POLL_INTERVAL_SECONDS` | `300` (5 min) | Default poll cadence inside `wait_for_response`. Lower for snappier UX (more API hits); higher for cheaper at-rest cost. |
| `REMOTECODETROL_API_BASE` | `https://us-central1-remotecodetrol.cloudfunctions.net/api` | Override only if you're running a fork of the backend (e.g. self-hosted). |
| `REMOTECODETROL_KEYCHAIN_SERVICE` | `com.remotecodetrol.mcp` | Keychain service name used for refresh-token storage. Override if you want to keep credentials for multiple backends side-by-side. |

Example `.zshrc` snippet:

```bash
export REMOTECODETROL_THREAD=primary
export REMOTECODETROL_DEVICE_LABEL="MacBook Pro (work)"
export REMOTECODETROL_DEFAULT_TIMEOUT_MINUTES=15
```

### Thread resolution priority

Multiple sources can specify which thread a tool call targets. Resolution order:

1. **Per-call `thread="..."` parameter** — wins if Claude passes it explicitly
2. **`set_thread(name)` call earlier in this or a prior session** — persisted to `~/.config/remotecodetrol/state.json`, read on every call
3. **`REMOTECODETROL_THREAD` env var** — fallback default

If none are set, `send_message`/`peek`/`ack` error with a friendly "no thread set" message. Run `list_threads()` then `set_thread(...)` to fix.

---

## Slash commands

### `/remotecodetrol:link`

The OAuth device-code flow entry point. Equivalent to asking Claude to call the `link` tool, but the slash command bundles the right "show the user_code, wait for them to authorize, then verify with whoami" prompt.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `command not found: uvx` | uv not installed or not on PATH | `curl -LsSf https://astral.sh/uv/install.sh \| sh`, restart shell |
| `claude plugin marketplace add github:...` fails | Network / GitHub rate limit / wrong slug | Re-run; verify the org/repo exists; or `gh repo clone zpriddy/ClaudeRemoteCodetrol && claude plugin marketplace add ./ClaudeRemoteCodetrol` (local install fallback) |
| Tool returns "**Not authorized. Run /remotecodetrol:link**" | No keychain token | Run `/remotecodetrol:link`, authorize on phone |
| Tool returns "**Authorization is still pending...**" | You called a tool before tapping Confirm in the iOS app | Tap Confirm, then retry the call. The next call detects the completion. |
| Push doesn't arrive on phone | iOS app not opened recently (FCM token may have expired), or notifications muted, or APNs misconfigured | Open the iOS app once to refresh the FCM token. Check iOS Settings → Notifications → RemoteCodetrol → Allow. |
| Tool calls return stale data after a code change | An older MCP process is still in memory | `pkill -f remotecodetrol-mcp`; next call respawns from the latest installed version |
| Plugin reload doesn't pick up code changes | Plugins are cached by version | `claude plugin uninstall ... && claude plugin install ...` |
| Account isn't approved yet (in iOS app) | Email isn't on the allowlist OR the `allowlisted` custom claim isn't yet set | Tell the project admin your email or your Apple "Hide My Email" relay address — they'll add it and you'll need to sign out + sign in to refresh the token |

For deeper debugging, you can run the MCP server standalone to see its stderr:

```bash
# Find the cached install path:
ls ~/.claude/plugins/cache/remotecodetrol/remotecodetrol/*/mcp-server

# Run it directly (Ctrl+C to stop):
uvx --from ~/.claude/plugins/cache/remotecodetrol/remotecodetrol/<version>/mcp-server remotecodetrol-mcp
```

---

## Uninstall

```bash
claude plugin uninstall remotecodetrol@remotecodetrol
claude plugin marketplace remove remotecodetrol

# Optional: wipe the stored OAuth refresh token from Keychain
keyring delete com.remotecodetrol.mcp <your-email>
```

To forget the active thread:
```bash
rm ~/.config/remotecodetrol/state.json
```

---

## Privacy & data

- The plugin sends only what Claude explicitly passes via tool calls (message bodies, idempotency keys, thread names) plus the OAuth bearer token.
- Refresh tokens live in macOS Keychain. They never travel to disk in plaintext.
- The active-thread state file at `~/.config/remotecodetrol/state.json` contains the thread name only — no credentials.
- The backend is locked to allowlisted users (closed-group beta).
- All transport is HTTPS / TLS 1.2+.

---

## Source

The canonical repo is this one — `github.com/zpriddy/ClaudeRemoteCodetrol`. The Python MCP source lives under `plugins/remotecodetrol/mcp-server/`. PRs welcome.

---

## License

MIT.
