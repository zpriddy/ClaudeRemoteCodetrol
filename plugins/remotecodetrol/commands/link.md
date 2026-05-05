---
description: Link this Claude Code session to your RemoteCodetrol iOS app via OAuth device-code flow.
---

The user wants to authorize this Claude Code session against the
RemoteCodetrol backend so the MCP can send pushes to their iPhone.

Steps:

1. Call the **`link`** MCP tool (`mcp__plugin_remotecodetrol_bridge__link`)
   with no arguments.

2. Inspect the result:
   - If `status == "already_linked"`: you're done — show the user the
     `email` and tell them they're already authorized.
   - If `status == "pending_authorization"`: extract `user_code` and
     `verification_uri` and show them to the user in this format:

     > **Open RemoteCodetrol on your iPhone → Settings → "Authorize new
     > device" → enter code `<user_code>` → tap Confirm.**
     >
     > (Or visit `<verification_uri>` from your phone.)

3. Wait briefly for the user to confirm they've authorized, then call
   **`whoami`** to verify. On success, show them the email returned.
   If `whoami` errors with "still pending," either wait ~5 seconds
   and call `whoami` again, or ask the user to confirm they finished
   tapping "Confirm" in the iOS app.

Do NOT call `send_message`, `peek_messages`, or other tools as part of
this command — those are for actual messaging. This command only links.
