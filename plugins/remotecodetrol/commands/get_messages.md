---
description: Fetch all pending replies on the active thread AND ack them. The most common "consume new messages" command.
---

The user invoked `/rc:get_messages`. Combined peek + ack: read all
unacked replies on the active thread, ack them on the server, and
return them so you can act on them. This is the most common "what's
new, let me handle it" operation.

## When to use which

- **`/rc:get_messages`** — you intend to *act* on the replies. Peek +
  ack in one round-trip. Use this 90% of the time.
- **`/rc:peek`** — you want a read-only look without consuming them
  (e.g. "is there anything new?" before deciding).
- **`/rc:ack`** — you already peeked separately and want to clear
  specific ids manually.

## Steps

1. **Pick the thread.** Default = active thread. Override only if the
   user requested.

2. **Call the tool.** Invoke `mcp__plugin_rc_bridge__get_messages`:
   - `thread`: only if the user requested an override

   CLI equivalent: `rcct get-messages`.

3. **Interpret the result.**
   - `messages`: the replies. If empty, tell the user "nothing
     new" and stop.
   - `acked`: count of messages just acked. Should equal
     `len(messages)` on success.

4. **Act on each reply.** Fold the content into your response, take
   any actions the user requested, and continue the original task.
   The messages are already acked — they won't reappear in future
   peeks or in the next `UserPromptSubmit` injection.

## Safety property

If the underlying peek succeeds but the ack fails (e.g. network
blip), the tool surfaces the error to you and the messages stay
unacked on the server. They'll reappear on the next peek. This is
the safer-than-the-alternative direction: a lost ack means "user
sees their reply repeated", not "Claude silently loses the reply".
