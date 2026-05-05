---
name: remotecodetrol
description: Use when sending status notifications to the user via the RemoteCodetrol iOS app, or when waiting for user input via mobile push. Especially useful during long-running tasks where the user may step away.
---

# Remote control via iOS push

When you need to notify the user or request input, use the `remotecodetrol`
MCP. The user receives a push on their iPhone and replies from the app.

## Drain pending replies before sending — ALWAYS

**Before every `send_message` call, drain the user-reply queue first.** The
queue is crash-safe — replies sit unacked across Claude sessions until
explicitly acked. If you `send_message` without draining first, your next
`wait_for_response` will return a stale reply from a previous session
(weird from the user's perspective: they reply, but you "echo" something
they sent hours ago).

Concrete pattern:

```
peek_messages()
  → if non-empty: process the replies, then ack_messages(message_ids=[...])
send_message(...)
  → if you need a reply, follow up with wait_for_response(...)
```

If a peek returns multiple messages, process all of them — usually the
most recent one is the relevant one, but treat the older ones as context
the user sent thinking you'd already see them.

## When to send

- **Before destructive actions** — force-push, dropping data, irreversible
  refactors. Use `require_response=True`.
- **After long-running tasks** — build done, tests pass/fail, deploy
  finished. Fire-and-forget (`require_response=False`) is fine.
- **When stuck on a decision** the user should make. Use `require_response=True`.

Keep messages tight: a one-line status plus the question. Markdown is rendered.

## Polling for a reply

After `send_message(..., require_response=True)`, enter polling mode:

- **Easy path:** call `wait_for_response(timeout_minutes=...)`. It loops
  `peek_messages` -> `ack_messages` for you and returns the user's reply, or
  an empty list on timeout.
- **Manual path:** `peek_messages()` returns unacked replies (crash-safe — they
  remain returnable until you ack). After processing, call
  `ack_messages(message_ids=[...])` so they don't show up again.

Default poll interval is 300s and default timeout is 10 minutes; both are
overridable per-call and via `REMOTECODETROL_DEFAULT_POLL_INTERVAL_SECONDS` /
`REMOTECODETROL_DEFAULT_TIMEOUT_MINUTES`.

If the timeout fires with no reply, summarize current state in your final
message to the user and stop — don't spin forever.

## End of every interaction — ack everything pending

After you've sent your final reply for the turn, do one last
`peek_messages()`. If there's anything unacked, ack it. This keeps the
queue clean so the next session doesn't open with a stale "echo of an old
reply" surprise.

## Picking the thread

Threads are addressed by name. Resolution priority:

1. Per-call `thread="..."` parameter on `send_message` / `peek_messages` / etc.
2. Whatever was last passed to `set_thread(name)` (persists across MCP restarts).
3. The `REMOTECODETROL_THREAD` env var.

If none of those are set, `send_message` errors. Call `list_threads()` to see
what's available, then `set_thread(...)` once at the start of the session.

## First-run authorization

If `whoami()` raises a "Not authorized" error, the user needs to run
`/remotecodetrol:link`. That returns a `user_code` — show it to them with
clear instructions to enter it in the iOS app's Settings → "Authorize new
device". Once they tap Confirm, your next tool call (any tool — `whoami`
is fine) detects the completed authorization and proceeds.

Don't loop / poll waiting for them to authorize — let them run it on
their schedule and resume on their next prompt.
