---
description: Link this Claude Code session to your RemoteCodetrol iOS app via OAuth device-code flow.
---

The user wants to authorize this Claude Code session against the
RemoteCodetrol backend so the MCP can send pushes to their iPhone.

Steps:

1. Call the **`link`** MCP tool (`mcp__plugin_remotecodetrol_bridge__link`)
   with no arguments. CLI equivalent: `rcct link`.

2. Inspect the result:
   - If `status == "already_linked"`: you're done — show the user the
     `email` and tell them they're already authorized.
   - If `status == "pending_authorization"`: the response carries
     `qr_ascii`, `user_code`, `deep_link`, and `verification_uri`. Show
     **both** the QR (in a fenced code block, monospace, so it scans)
     and the `user_code` as backup. Use this format:

     ````
     Open RemoteCodetrol on your iPhone → Settings → "Authorize new
     device" → in-app scanner. Scan this QR:

     ```
     <paste the qr_ascii field exactly as returned, no edits>
     ```

     Or enter code **`<user_code>`** manually. (Web fallback:
     `<verification_uri>`.)
     ````

     The QR encodes `remotecodetrol://authorize?code=<user_code>` —
     the iOS app extracts the code from it.

3. **Do not poll.** After showing the code, end the turn. The flow is
   human-gated; calling `whoami()` immediately just returns
   `authorization_pending` and burns rate-limit budget. Wait at least
   30 seconds before any status-check tool call.

   **Exception:** if the user explicitly says "I confirmed" / "I tapped
   it" / "done" or equivalent, call **`complete_link`**
   (`mcp__plugin_remotecodetrol_bridge__complete_link`) — CLI:
   `rcct check-link`. This bypasses both client-side and server-side
   polling cooldowns and returns `authorized | pending | expired |
   denied | invalid` immediately. On `authorized`, show the email.

Do NOT call `send_message`, `peek_messages`, or other tools as part of
this command — those are for actual messaging. This command only links.
