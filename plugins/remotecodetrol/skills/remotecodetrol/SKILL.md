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
were idle:" injected into your context:

- Each line is tagged with its thread, e.g. `[thread:test] (2m ago) "..."`.
- Process the entries on **your active thread** per the rules below —
  fold relevant content into your reply and call `ack_messages([...])`
  on those messages.
- Entries on **other threads** belong to other Claude Code sessions
  for the same user. **Do not ack them.** Mention briefly in your
  reply that you saw them but they're not yours to handle (see the
  "Cross-session discipline" section below).

## Read & process pending replies before sending — ALWAYS

**Before every `send_message` call, peek your active thread and *process*
any unacked replies.** Replies are crash-safe and persist across Claude
sessions; users can send messages between Claude's turns or across
separate sessions, and you might be the first Claude that sees them.

The flow:

1. **`peek_messages()`** (no `thread=` argument — use your active thread).
2. **For each reply, decide:**
   - **Relevant to the current task** → fold it into your reasoning + your
     reply. Reference it explicitly in your next `send_message` body so
     the user knows you read it ("Got your '<quoted snippet>' — yes, doing
     X now").
   - **Conversational / no action needed** → a short ack is enough
     ("Got '👍'") so the user knows the system delivered it.
3. **`ack_messages(message_ids=[...])`** — ack the messages you processed.
   See the **Cross-session discipline** section below for the strict rule
   on what NOT to ack.
4. **Then** call your new `send_message`.

The cardinal rule: **the user should always be able to look at the thread
and see that Claude saw every message they sent on this thread.** If a
reply landed on your thread, the next Claude message acknowledges it.

## When to send

- **Before destructive actions** — force-push, dropping data, irreversible
  refactors. Use `require_response=True`.
- **After long-running tasks** — build done, tests pass/fail, deploy
  finished. Fire-and-forget (`require_response=False`) is fine.
- **When stuck on a decision** the user should make. Use `require_response=True`.

Keep messages tight: a one-line status plus the question. Markdown is rendered.

## Waiting for a reply — `send_message` blocks by default (v0.3.9+)

`send_message(..., require_response=True)` **blocks until the reply
arrives** (or `timeout_minutes` elapses). One tool call sends the
message AND awaits the answer. The user can step fully away from the
terminal — even leave their Mac entirely — and Claude will resume
within ~1s of their phone reply landing in Firestore. The MCP's SSE
consumer is essentially a cron loop waiting for the reply event;
the `send_message` tool just doesn't return until that loop fires.

**Default pattern (recommended for almost every case):**

```
result = send_message(
    body="should I deploy?",
    require_response=True,
)
# When this returns, result.pending_messages has the reply.
# Process it and continue.
```

That's it. No `wait_for_response`, no hook dependency, no
"tell-the-user-to-type-here" instructions. Claude is paused inside
the tool call; on reply, the tool returns and Claude continues.

**`wait_for_response` is rarely needed now.** It exists for the case
where you sent earlier with `wait=False` (or `require_response=False`)
and now want to block-wait separately.

**`wait=False` opts out of blocking** for pure-status messages:
```
send_message(body="build done", require_response=False)
# Returns immediately, no waiting.
```

Or when you want to send-without-blocking but still expect a reply
("FYI, I'm doing X — interrupt if needed"):
```
send_message(body="...", require_response=True, wait=False)
# Returns immediately; reply will land in cache and the hook will
# inject it on the user's next prompt (when they're back at terminal).
```

**The UserPromptSubmit hook is a backstop, not the primary mechanism.**
If a reply arrives while Claude is between turns (e.g., after a `wait=False`
send, or unsolicited chat from the user), the next prompt the user types
in the terminal will arrive with the reply pre-injected as
`additionalContext`. Useful when it works; **do not rely on it as a
substitute for blocking** — if the user is fully away from the terminal,
the hook never fires.

If a `send_message` blocking call times out with no reply, summarize
current state in your final message and stop — don't spin forever.
Default timeout is 10 minutes; override with `timeout_minutes`.

## Cross-session discipline — only ack your active thread

If multiple Claude Code sessions are running for the same user, each
sees the **same** SSE event stream and may receive the same messages
in `pending_messages` bundles. **Acking a message removes it from every
session's cache and marks it processed in Firestore**, so an aggressive
ack from one session can break the workflow of another session that's
waiting on the same reply.

The rule:

- **Default to peeking only your active thread.** Omit the `thread=`
  argument on `peek_messages` — it resolves to whatever `set_thread`
  / env / call-time chose.
- **`peek_messages(thread="*")` is diagnostic-only.** If you see
  messages from threads you aren't working on, mention them in your
  next `send_message` body ("noticed 'X' on the GasCity thread —
  different session's territory, leaving it") and **do NOT call
  `ack_messages` on them.**
- **Bundled `pending_messages` from `send_message`** may include
  threads other than your active one. Same rule: acknowledge in
  your reply body if relevant context, ack only the entries on your
  active thread.
- A message on your active thread is yours to process. A message on
  another thread belongs to whichever Claude Code session is set to
  that thread.

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
