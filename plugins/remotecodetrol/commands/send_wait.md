---
description: Send a message and block until the user replies (or timeout). Use when you cannot continue without the answer.
---

The user invoked `/rc:send_wait`. Send their message to the iPhone AND
block this tool call until they reply or the timeout elapses. Use this
when you genuinely cannot do anything useful until the answer comes
back — otherwise prefer `/rc:send_message` (fire-and-forget) so the
session stays responsive.

## When to use vs send_message

- **`/rc:send_message`** — preferred. Send and continue. Reply lands
  via the `UserPromptSubmit` hook on the next turn.
- **`/rc:send_wait`** — use when you're truly stuck. Blocks the whole
  Claude session inside this tool call. Freezes nothing else (other
  Claude sessions / tmux / cron-driven prompts keep flowing) but
  stalls this turn until you get the reply or timeout.

## Steps

1. **Determine the message body** from the user's arguments. If empty,
   ask. Markdown renders on the phone.

2. **Pick the thread.** Default = active thread. Override only if the
   user said so.

3. **Pick the timeout.** Default = server config (~10 min). The user
   can pass an explicit timeout in their arguments (e.g.
   `/rc:send_wait timeout=5 the body`); honour it.

4. **Strongly consider response options.**
   `/rc:send_wait` is the case where `response_options` is *most*
   valuable — you're blocking until the user answers, and a tap is
   faster than typing. If the question has a small, well-defined
   answer space (yes/no, A/B/C, "which of these"), **default to
   passing `response_options`**.

   Shape (pass alongside `body`):
   ```
   response_options=[
       {"id": "yes",  "label": "Yes, proceed"},
       {"id": "no",   "label": "No, abort"},
       {"id": "wait", "label": "Wait, let me check something"},
   ]
   selection_mode="single"   # "single" = one tap submits; "multi" = checkbox + submit
   ```

   Rules:
   - 2–5 options. `id` is URL-safe + stable so you can match it in
     the reply's `selectedOptionIds`. `label` is short.
   - The user can still type a free-text answer; don't assume the
     reply is one of the options.
   - Use `selection_mode="multi"` for questions where multiple
     answers are valid ("which of these to skip?").
   - Skip `response_options` only when the question genuinely needs
     free text (a name, a length, a justification).

5. **Call the tool.** Invoke `mcp__plugin_rc_bridge__send_message`:
   - `body`: the message
   - `response_options` + `selection_mode`: when step 4 said yes
   - `require_response`: `true`
   - `wait`: `true`
   - `timeout_minutes`: as specified, else omit (use server default)
   - `thread`: only if the user requested

   CLI equivalent: `rcct send-wait "the body" [--timeout 5]
   [--thread X]`. The CLI does not expose `response_options` —
   invoke the tool directly when you need them.

6. **Interpret the result.**
   - The response's `pending_messages` contains the user's reply
     (typically one message; could be more if they sent multiple
     between your send and the reply window).
   - If you passed `response_options`, the reply message has
     `selectedOptionIds` populated with the ids the user tapped
     (matches your option `id` values). If `selectedOptionIds` is
     empty/null, the user typed a free-text reply instead — read
     the `body` of the reply.
   - If `pending_messages` is empty, the wait timed out — surface
     that clearly: "no reply within Nm — what would you like to do?"
     and stop. **Do not loop.**

7. **Act on the reply.** Fold the content into your next response and
   continue the user's task. If the user picked one of your options,
   echo back what they chose so they know you got it ("OK, proceeding
   with X.").
