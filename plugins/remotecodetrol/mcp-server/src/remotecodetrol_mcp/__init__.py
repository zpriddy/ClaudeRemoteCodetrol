"""RemoteCodetrol MCP server.

Exposes a FastMCP server that lets Claude Code send messages to and receive
replies from the RemoteCodetrol iOS app via push notifications.

v0.3.5 — streaming relay: the MCP holds a long-lived SSE connection to
the backend (see streaming.py + state_file.py) so push events flow into
Claude's context via a UserPromptSubmit hook between turns. Polling-based
v0.2.4 callers continue to work; the `poll_interval_seconds` kwarg on
``wait_for_response`` is accepted and ignored.
"""

__version__ = "0.3.5"
