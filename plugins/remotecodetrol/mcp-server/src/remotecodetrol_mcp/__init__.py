"""RemoteCodetrol MCP server.

Exposes a FastMCP server that lets Claude Code send messages to and receive
replies from the RemoteCodetrol iOS app via push notifications.

v0.6.x — polling consumer replaces SSE. Cost-optimized cadence (60s
idle → 300s with backoff, 5s busy, 2s while blocked) drives the same
in-memory cache + `UserPromptSubmit` hook surface as v0.5.x, but
without pinning Cloud Run instances. The `stream` Cloud Function is
gone; `api` is the only HTTP endpoint the MCP talks to now.

v0.5.x — selectable response buttons in `send_message`.

v0.4.x — single 14-day opaque token (replaces v0.3.x JWT pair), `rcct`
CLI over Unix domain socket, per-session `known_threads` allowlist, ASCII
QR rendering on link, and backend `email` field returned at issuance so
`whoami` shows the user's real address (v0.4.3+).
"""

__version__ = "0.6.0"
