---
description: Block waiting for new replies on the active thread (default 10 min). One-shot — does not loop.
---

The user invoked `/rc:wait_blocked`. Block the Claude session until a
new reply arrives on the active thread, or the timeout elapses. This
is the "I already sent something earlier and now I need to wait for the
answer" command — not paired with a send.

## When to use

- After an earlier `/rc:send_message` (fire-and-forget) where you've
  decided you can't continue without the reply after all.
- When external automation just sent something via `rcct send-message`
  and now Claude wants to wait for the user.

If you're sending AND waiting in one shot, use `/rc:send_wait` instead.

## Steps

1. **Pick the timeout.** The user can pass `timeout=N` to override the
   default. Default = 10 minutes (long enough to step away, short
   enough that you don't strand the session forever).

2. **Call the tool.** Invoke
   `mcp__plugin_rc_bridge__wait_for_response`:
   - `timeout_minutes`: 10 (or user override)
   - `thread`: only if the user specified one; otherwise use the
     active thread (don't pass)

   CLI equivalent: `rcct wait-blocked --timeout 10` (or just
   `rcct wait`).

3. **Interpret the result.**
   - If `messages` is non-empty: the user replied. Fold into your
     response. **Always ack** the messages you've processed via
     `mcp__plugin_rc_bridge__ack_messages` so they don't re-surface
     on the next prompt.
   - If `messages` is empty: timed out. Tell the user "no reply
     within Nm — what would you like to do?" and stop. **Do not
     loop.**

## Polling behavior under the hood

While this tool blocks, the MCP's polling consumer tightens its cadence
to ~2 s so a new reply surfaces nearly real-time. Cost during the wait
is bounded (10 min × 2 s = 300 polls); the consumer relaxes back to
its dormant cadence once the wait ends.
