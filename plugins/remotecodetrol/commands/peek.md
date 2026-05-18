---
description: Look at pending replies on the active thread WITHOUT acking them. Read-only check.
---

The user invoked `/rc:peek`. Show what unacked replies are sitting on
the active thread, without consuming them. Use this when you want a
read-only look — e.g. "is there anything new?" before deciding what to
do next.

If you intend to *act on* the messages (the common case), use
`/rc:get_messages` instead — it peeks AND acks in one tool call so you
don't have to do the bookkeeping.

## Steps

1. **Pick the thread.** Default = active thread. Override only if the
   user requested.

2. **Call the tool.** Invoke `mcp__plugin_rc_bridge__peek_messages`:
   - `thread`: only if the user requested an override
   - (No other arguments; this is a simple read)

   CLI equivalent: `rcct peek` (or `rcct check` — same thing).

3. **Report what you see.** For each message: thread, who sent it,
   the body preview, and the message id. Make it clear these are
   **not acked** — they'll appear again on the next peek or on the
   next prompt via the `UserPromptSubmit` hook.

4. **Don't ack.** Leave the messages on the server. Acking happens via
   `/rc:ack` (manual) or `/rc:get_messages` (combined peek+ack).
