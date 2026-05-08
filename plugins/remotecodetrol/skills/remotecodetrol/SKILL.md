---
name: remotecodetrol
description: Use when sending status notifications to the user via the RemoteCodetrol iOS app, or when waiting for user input via mobile push. Especially useful during long-running tasks where the user may step away.
---

# Remote control via iOS push

When you need to notify the user or request input, use the `remotecodetrol`
MCP. The user receives a push on their iPhone and replies from the app.

## v0.3.0 — replies may arrive as injected context between turns

As of v0.3.0, the plugin streams replies in the background and surfaces
them via a `UserPromptSubmit` hook: when the user sends a reply on their
phone while you're idle, the next turn opens with that reply already
visible to you as `additionalContext` (no `peek_messages` call required).

When you see "Messages received from user via RemoteCodetrol while you
were idle:" injected into your context, the read/decide/communicate
rule below still applies — process each one, fold relevant content
into your reply, and **`ack_messages([...])` them even though you didn't
explicitly `peek_messages`**. Acking marks the user's reply as seen so
it doesn't re-surface on every future turn.

## Read & process pending replies before sending — ALWAYS

**Before every `send_message` call, peek the queue and *process* any
unacked replies. Never silently ack-and-discard.** Replies are crash-safe
and persist across Claude sessions, so users can send messages between
Claude's turns or even across separate sessions, and you might be the
first Claude that sees them.

The flow:

1. **`peek_messages()`** — see what's pending.
2. **For each reply, decide:**
   - **Relevant to the current task** → fold it into your reasoning + your
     reply. Reference it explicitly in your next `send_message` body so
     the user knows you read it ("Got your '<quoted snippet>' — yes, doing
     X now").
   - **Stale or out-of-scope** → still acknowledge it explicitly in your
     next `send_message` body. Don't pretend you didn't see it. Example:
     *"Saw your earlier reply '<snippet>' — that looks like context from
     a different task; flag if you want me to act on it."*
   - **Conversational / no action needed** → a short ack is enough
     ("Got '👍'") so the user knows the system delivered it.
3. **`ack_messages(message_ids=[...])`** — ack EVERYTHING you peeked,
   even the stale ones. The user has been informed; they don't need
   to see the same reply re-surface in a future turn.
4. **Then** call your new `send_message`.

The cardinal rule: **the user should always be able to look at the thread
and see that Claude saw every message they sent.** If a reply went in,
the next Claude message acknowledges it (briefly is fine).

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
  an empty list on timeout. Note: this auto-acks, so the responsibility to
  *acknowledge in your next send_message body* still applies — wrap the
  reply content into your reasoning visibly.
- **Manual path:** `peek_messages()` returns unacked replies. Process each
  per the rules above, then call `ack_messages(message_ids=[...])`.

Default poll interval is 300s and default timeout is 10 minutes; both are
overridable per-call and via `REMOTECODETROL_DEFAULT_POLL_INTERVAL_SECONDS` /
`REMOTECODETROL_DEFAULT_TIMEOUT_MINUTES`.

If the timeout fires with no reply, summarize current state in your final
message to the user and stop — don't spin forever.

## End of every interaction — final peek + ack

Before you stop the turn, do one last `peek_messages()`. If new replies
arrived while you were composing or working, process them per the rules
above (your final `send_message` is the place to acknowledge them). This
keeps the queue clean so the next session doesn't open with surprise
leftovers.

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
