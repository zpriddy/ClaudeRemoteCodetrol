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

3. **Decide if response options would help.**
   If the message is asking a question with a small, well-defined
   answer space (yes/no, multiple choice, "which of these…"), prefer
   `response_options` — the phone renders them as tappable buttons so
   the user replies in one tap instead of typing. **Use them liberally
   for any question whose answer is one of a few fixed shapes.** Don't
   use them for open-ended prompts ("what would you like?") or when
   the user clearly needs to write free text.

   Shape (pass alongside `body`):
   ```
   response_options=[
       {"id": "yes",  "label": "Yes, proceed"},
       {"id": "no",   "label": "No, hold off"},
       {"id": "show", "label": "Show me the diff first"},
   ]
   selection_mode="single"   # "single" = one tap submits; "multi" = checkbox + submit button
   ```

   Rules:
   - 2–5 options. The phone clips beyond 5.
   - `id` is URL-safe (alphanumeric + `-`/`_`), stable, and you'll see
     it back in the reply's `selectedOptionIds` — pick something you
     can pattern-match later.
   - `label` is what the user reads. Keep it short — buttons truncate.
   - The user can still type a free-text reply even with options
     visible; don't assume the answer must be one of the options.
   - For multi-select questions ("which of these features should I
     enable?"), use `selection_mode="multi"`.

4. **Call the tool.**
   Invoke `mcp__plugin_rc_bridge__send_message`:
   - `body`: the message
   - `response_options` + `selection_mode`: only when step 3 said yes
   - `require_response`: `false` (this is fire-and-forget). Set
     `true` only if the user explicitly wants to track that a reply
     is expected, OR you passed `response_options` (the buttons
     imply a reply is expected).
   - `thread`: only if the user requested a specific one
   - `wait`: `false` (always — this command never blocks)

   CLI equivalent: `rcct send "the body"` (add `--thread X` for
   override, `--require-response` if the user wants the awaiting-
   response badge in the app). The CLI does not currently expose
   `response_options` — invoke the tool directly when you need them.

5. **Report back.**
   Echo what you sent and on which thread. If you passed
   `response_options`, list the option labels too so the user knows
   what to look for on their phone. Note: this command does NOT
   wait for a reply — the user's reply will arrive via the
   `UserPromptSubmit` hook on the next turn.
