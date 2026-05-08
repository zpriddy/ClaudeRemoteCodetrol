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

## Waiting for a reply — `send_message` is non-blocking by default (v0.3.10+)

`send_message(..., require_response=True)` returns immediately. The
session stays responsive while the user thinks. Replies are delivered
via TWO complementary mechanisms — both work without Claude doing
anything special:

1. **The UserPromptSubmit hook** (primary delivery mechanism). On every
   prompt submitted to this session — whether the user typed it OR
   external automation injected it (tmux pipes, cron, watch-mode,
   parallel-task scripts) — the hook reads the MCP's pending state
   file and injects pending replies as `additionalContext`. Claude
   sees the reply at the top of its next turn without calling any tool.

2. **Bundled `pending_messages` in tool responses.** Every
   `send_message` returns includes `pending_messages` from the cache.
   Already-pending replies show up at the top of the response.

Why this is safe: the hook fires on ANY prompt source. If you have
tmux/cron/external-script automation feeding prompts in, the hook
delivers replies as part of those prompts. If the user is fully
away with no automation, the reply still lands in the cache + state
file; the next time anything triggers a prompt to this session, it
gets injected.

**Default pattern:**

```
result = send_message(
    body="should I deploy?",
    require_response=True,
)
# Tool returns immediately. Claude continues its turn.
# If `result.pending_messages` is empty, no reply yet — that's fine,
# Claude can do other work, end the turn, etc. Reply will arrive via
# the hook on a future prompt.
```

**Use `wait=True` only when ALL of:**
- Claude truly cannot do any other useful work until the reply arrives.
- No external prompt automation is expected to feed this session.
- Freezing the session for the wait duration is acceptable.

```
result = send_message(
    body="confirm: drop database X?",
    require_response=True,
    wait=True,  # blocks the session inside this tool call
    timeout_minutes=5,
)
```

`wait_for_response` exists for the "I sent earlier with wait=False
and now I genuinely need to block-wait" case. Rare.

If a `wait=True` call times out with no reply, summarize current state
and stop — don't spin forever. Default timeout is 10 minutes;
override with `timeout_minutes`.

## Set up a self-poll loop for long waits with no other prompt source

The hook injects pending replies on **any** UserPromptSubmit. If the
session has external prompt automation (tmux pipes, cron, watch-mode,
parallel scripts), those keep the hook firing — replies get delivered
naturally. Claude doesn't need to do anything special.

**But if Claude is ending its turn and nothing else will prompt the
session for a long time** — no user typing, no automation — the reply
sits in the cache + state file forever. To fix this without blocking
(which would freeze the session), Claude should set up a self-poll
loop using Claude Code's `/loop` skill:

```
/loop 5m peek_messages
```

This re-invokes `peek_messages` every 5 minutes in the same session.
Each invocation:
1. Triggers the UserPromptSubmit hook (pending replies inject as
   `additionalContext`).
2. Returns the cache state to Claude (a no-op cost when nothing's
   pending — local lookup, zero network).

When the reply arrives, the next loop iteration delivers it. Claude
processes it, acks, and **cancels the loop** (`/cancel-ralph` or the
equivalent) so it doesn't keep polling forever.

**When to set up a self-poll:**
- Sending with `require_response=True, wait=False` (default).
- Claude is ending the turn after sending.
- The reply might take longer than the user's typical response cadence
  (e.g., overnight wait, user is in a meeting).
- No external automation is expected to prompt the session.

**Loop interval choice:**
- `5m` — good default for "user might reply in minutes-to-hours"
- `30s` — for time-critical decisions where you'd otherwise have used `wait=True`
- self-paced (omit interval) — Claude decides when to re-check

**Don't set up a loop when:**
- An external prompt source is already feeding the session.
- The user is actively at the terminal (their typing IS the cron).
- `wait=True` would be more appropriate (decision is blocking
  everything else anyway).

For one-shot deferred checks rather than recurring, use the
`schedule` skill:

```
Skill: schedule
Args: in 1 hour, peek_messages on remotecodetrol
```

Either way, the session stays responsive between checks — no blocking,
no dependency on the user's manual prompts.

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
