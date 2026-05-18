---
description: Send a message to the user's iPhone via RemoteCodetrol. Fire-and-forget (no waiting for a reply).
---

The user invoked `/rc:send_message`. Send the message they specified to
their iPhone via the RemoteCodetrol app. **Do not wait for a reply** —
this is the fire-and-forget variant. Use `/rc:send_wait` instead when
the user needs you to block on the reply.

## Steps

1. **Determine the message body.**
   The user's arguments after `/rc:send_message` are the message body
   (markdown is rendered on the phone). If they didn't include a body,
   ask them what to send. Keep messages tight — one line of status
   plus the question. Code-block formatting (\`\`\`) works.

2. **Pick the thread.**
   By default, use the active thread (don't pass `thread`). If the user
   said something like "on the deploy thread", pass `thread="deploy"`.

3. **Call the tool.**
   Invoke `mcp__plugin_rc_bridge__send_message`:
   - `body`: the message
   - `require_response`: `false` (this is fire-and-forget). Set
     `true` only if the user explicitly wants to track that a reply
     is expected.
   - `thread`: only if the user requested a specific one
   - `wait`: `false` (always — this command never blocks)

   CLI equivalent: `rcct send "the body"` (add `--thread X` for
   override, `--require-response` if the user wants the awaiting-
   response badge in the app).

4. **Report back.**
   Echo what you sent and on which thread. Note: this command does NOT
   wait for a reply — the user's reply will arrive via the
   `UserPromptSubmit` hook on the next turn.
