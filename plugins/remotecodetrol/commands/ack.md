---
description: Ack (mark processed) specific message ids on a thread. Use after you've already peeked and acted.
---

The user invoked `/rc:ack`. Mark one or more pending replies as
processed so they stop appearing in peeks and in the
`UserPromptSubmit` hook on future prompts.

You usually don't call this directly — `/rc:get_messages` peeks AND
acks in one shot, which is what you want most of the time. Use
`/rc:ack` only when you peeked separately (via `/rc:peek`), acted on
some messages, and now want to clear them.

## Steps

1. **Get the message ids.** The user passes them as arguments after
   `/rc:ack`. If they didn't specify, ask — or first run `/rc:peek`
   to see what's pending.

2. **Pick the thread.** Default = active thread.

3. **Call the tool.** Invoke `mcp__plugin_rc_bridge__ack_messages`:
   - `message_ids`: array of doc ids (the `id` field from peek)
   - `thread`: only if the user requested an override

   CLI equivalent: `rcct ack <id1> <id2> ...`.

4. **Report.** "Acked N messages on thread X."

## Cross-session discipline

**Never ack messages on a thread you didn't peek from.** If
`/rc:peek` showed messages on multiple threads, only ack the ones on
*your* active thread. Other threads belong to other Claude sessions
(per the design doc's cross-session discipline) and acking them
silences someone else's work.
