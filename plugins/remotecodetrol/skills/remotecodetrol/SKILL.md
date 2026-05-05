---
name: remotecodetrol
description: Use when sending status notifications to the user via the RemoteCodetrol iOS app, or when waiting for user input via mobile push. Especially useful during long-running tasks where the user may step away.
---

# Remote control via iOS push

When you need to notify the user or request input, use the `remotecodetrol`
MCP. The user receives a push on their iPhone and replies from the app.

## When to send

- **Before destructive actions** — force-push, dropping data, irreversible
  refactors. Use `require_response=True`.
- **After long-running tasks** — build done, tests pass/fail, deploy
  finished. Fire-and-forget (`require_response=False`) is fine.
- **When stuck on a decision** the user should make. Use `require_response=True`.

Keep messages tight: a one-line status plus the question. Markdown is rendered.

## Polling for a reply

After `send_message(..., require_response=True)`, enter polling mode:

- Easy path: call `wait_for_response(timeout_minutes=...)`. It loops
  `peek_messages` -> `ack_messages` for you and returns the user's reply, or
  an empty list on timeout.
- Manual path: `peek_messages()` returns unacked replies (crash-safe — they
  remain returnable until you ack). After processing, call
  `ack_messages(message_ids=[...])` so they don't show up again.

Default poll interval is 300s and default timeout is 10 minutes; both are
overridable per-call and via `REMOTECODETROL_DEFAULT_POLL_INTERVAL_SECONDS` /
`REMOTECODETROL_DEFAULT_TIMEOUT_MINUTES`.

If the timeout fires with no reply, summarize current state in your final
message to the user and stop — don't spin forever.

## Picking the thread

Threads are addressed by name. Resolution priority:

1. Per-call `thread="..."` parameter on `send_message` / `peek_messages` / etc.
2. Whatever was last passed to `set_thread(name)` (persists across MCP restarts).
3. The `REMOTECODETROL_THREAD` env var.

If none of those are set, `send_message` errors. Call `list_threads()` to see
what's available, then `set_thread(...)` once at the start of the session.

## First-run authorization

The first call triggers an OAuth device-code flow: the MCP prints a
`https://remotecodetrol.web.app/authorize?user_code=XXXX-XXXX` URL on stderr.
The user opens it on their phone (or types the code in the app's Settings ->
Authorize new device). Token is stored in macOS Keychain; subsequent runs are
silent until the refresh token is revoked.
