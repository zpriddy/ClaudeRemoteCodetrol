"""RemoteCodetrol MCP server.

Exposes a FastMCP server that lets Claude Code send messages to and receive
replies from the RemoteCodetrol iOS app via push notifications.

v0.4.x — single 14-day opaque token (replaces v0.3.x JWT pair), `rcct`
CLI over Unix domain socket, per-session `known_threads` allowlist, ASCII
QR rendering on link, and backend `email` field returned at issuance so
`whoami` shows the user's real address (v0.4.3+).

v0.3.x lineage (streaming relay) is still the underlying transport:
long-lived SSE connection to the backend feeds an in-memory cache and a
`UserPromptSubmit` hook that surfaces pending replies between turns.
"""

__version__ = "0.4.5"
