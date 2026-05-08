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

## Stay in checking mode once activated — until user says stop or 2h idle

**This is a hard rule, not case-by-case.**

When EITHER of these happens, you enter **CHECKING MODE**:
- You make any `send_message(..., require_response=True)` call.
- The user explicitly asks you to notify them on their phone (e.g.
  "ping me when X is done", "let me know via the app").

Once in checking mode, **you MUST keep a recurring poll running**
for replies. Do not end the session assuming the hook will deliver.
Do not skip the loop because the user is at the terminal — terminal
interaction can stop at any time; the loop can't.

### The contract

1. **At activation** (first `require_response=True` send, or first
   "notify me" request), immediately set up a recurring poll via
   the built-in `loop` skill:

   ```
   Skill: loop
   Args: 1m peek messages from remotecodetrol on the active thread.
         Process any pending per the read/decide/communicate/ack
         rule. If `~/Library/Caches/remotecodetrol/pending.json`
         has `updated_at` older than 2 hours AND no pending
         replies, that's the idle timeout — send a final
         "timing out, no reply for 2h" via send_message and
         CronDelete this loop's job. Otherwise end turn.
   ```

   Use the **built-in `loop` skill**, NOT `ralph-loop:ralph-loop`
   (different mechanism — Ralph is fire-on-exit with no interval).
   `1m` is the minimum cron granularity and the right default;
   anything shorter silently rounds up.

2. **Keep polling until ONE of** these stop conditions:
   - **User explicitly says stop.** Phrases like "stop polling",
     "we're done", "cancel the loop", "no more notifications",
     equivalent. Cancel via `CronDelete` and confirm the cancel.
   - **2 hours pass since the last activity** on the active thread.
     Track via `pending.json` `updated_at` (the SSE consumer
     rewrites this file on every cache mutation, so it advances
     whenever a reply lands or is acked). When `updated_at` is
     > 2h old AND `pending` is empty: 2h of silence. Send a final
     timeout summary, then `CronDelete`.

3. **A new reply RESETS the 2h clock** automatically. The SSE
   consumer updates `pending.json.updated_at` on every change, so
   the next iteration sees a fresh timestamp.

4. **A new `send_message(require_response=True)` mid-loop** does
   NOT need a second loop — the existing one already covers any
   pending on the active thread. Don't stack loops.

5. **Cancel the loop only on** the two stop conditions above.
   Don't cancel because "the user is at the terminal" or "the hook
   will catch it" or "I'm tired of seeing iterations." Those
   assumptions are exactly what break the user's mental model
   that "I asked Claude to watch my phone, so it's watching."

### Constraints (from CronCreate)

- **1-minute minimum cadence.** Cron granularity is 1 minute; any
  finer requires `wait=True` instead of a loop.
- **Session-scoped by default** — the cron dies when Claude Code
  exits. For waits that need to survive restarts, use the
  `schedule` skill (cloud-based, durable) or pass `durable=true`
  to CronCreate directly.
- **Auto-expires after 7 days** — backstop against truly stuck
  loops; the 2h idle rule should fire long before this.

### When NOT in checking mode

If you've never sent `require_response=True` and the user hasn't
asked for phone notification, you don't need a loop. A
fire-and-forget `send_message(require_response=False)` is just a
status update — no reply expected, no polling needed.

For one-shot deferred reminders ("remind me in an hour"), use the
`schedule` skill rather than a recurring loop.

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
