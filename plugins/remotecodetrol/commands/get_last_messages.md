---
description: Context recovery — fetch the last N user messages on the thread INCLUDING already-acked ones. Does NOT ack anything.
---

The user invoked `/rc:get_last_messages`. Fetch recent thread history
including messages you've already acked. Use this when you've lost
context — e.g. session restart, you don't remember what the user
asked earlier, or you want to see what they were discussing before
the current task.

## When to use which

- **`/rc:get_last_messages`** — look at recent HISTORY including acked
  messages. Read-only, doesn't ack. Use for "what was the user asking
  about?" recovery.
- **`/rc:peek`** — only UNACKED messages, no ack. Use for "what's new?"
- **`/rc:get_messages`** — only UNACKED messages + ack them. The "consume
  new replies" pattern.

## Steps

1. **Pick the limit.** Default is 20 messages back. The user can pass
   a different number in their arguments (e.g.
   `/rc:get_last_messages limit=50 the body`). Server caps at 100.

2. **Pick the thread.** Default = active thread.

3. **Call the tool.** Invoke `mcp__plugin_rc_bridge__get_last_messages`:
   - `limit`: as specified (default 20)
   - `thread`: only if the user requested

   CLI equivalent: `rcct get-last --limit 20 [--thread X]`.

4. **Report the messages chronologically** (oldest → newest). Each
   message has `id`, `body`, `created_at`, and possibly `claude_acked_at`
   if you already acked it. The CLI marks acked ones with `✓` and
   unacked with `•`.

5. **Do NOT ack.** This is a read-only operation. If you process some
   of these messages and want to ack them, use `/rc:ack <ids>` or
   `/rc:get_messages` separately.

## Use cases

- "Remind me what the user asked earlier" — look at recent history.
- After a session restart: re-establish context without re-running
  the whole `UserPromptSubmit` hook.
- Auditing a long thread for a specific question or decision.
