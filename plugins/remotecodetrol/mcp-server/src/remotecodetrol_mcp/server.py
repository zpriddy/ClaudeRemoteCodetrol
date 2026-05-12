"""FastMCP entry point for the RemoteCodetrol MCP server.

Run via the `remotecodetrol-mcp` console script or `python -m
remotecodetrol_mcp.server`. Exposes module-level `mcp` so introspection
tools (and tests) can list registered tools without spinning up the server.

Lifecycle (v0.4.0):
  - Tools are registered immediately at import time (so FastMCP's tool
    listing works without an event loop).
  - The SSE consumer is spawned eagerly on `mcp.run()` startup if a token
    is already present in tokens.json.
  - The CLI socket server (Unix domain socket dispatcher for `rcct`) is
    bound at startup alongside the SSE consumer.
  - `known_threads` is seeded from REMOTECODETROL_KNOWN_THREADS env var
    at startup; tools augment it at runtime via set_thread / send_message.
  - `RC_DISABLE_STREAMING=1` skips the SSE consumer entirely; tools fall
    back to direct API calls only.
  - `RC_DISABLE_CLI_SOCKET=1` skips the CLI socket (use for tests where
    multiple concurrent MCPs would clobber the socket path).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import httpx
from fastmcp import FastMCP

from .auth import AuthClient
from .client import APIClient
from .config import load_config
from .socket_server import SocketServer
from .state_file import make_writer
from .streaming import SseConsumer, StreamingState
from .tools import ThreadState, register_tools


CONFIG = load_config()
mcp: FastMCP = FastMCP("remotecodetrol")

# A single long-lived httpx client so connections + DNS get reused.
_HTTP = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
_AUTH = AuthClient(CONFIG, _HTTP)
_API = APIClient(CONFIG, _AUTH, _HTTP)
_STATE = ThreadState(CONFIG)

# Streaming state lives for the lifetime of the MCP process. Seed
# known_threads from env (REMOTECODETROL_KNOWN_THREADS=foo,bar) per
# spec §5.2. Runtime additions via set_thread/send_message extend this set.
_STREAMING = StreamingState()
for _seed in CONFIG.known_threads_seed:
    _STREAMING.add_known_thread(_seed)

_SSE_CONSUMER: SseConsumer | None = None
if not os.environ.get("RC_DISABLE_STREAMING"):
    _SSE_CONSUMER = SseConsumer(
        CONFIG,
        _AUTH,
        _HTTP,
        _STREAMING,
        state_file_writer=make_writer(),
    )
else:
    _STREAMING.sse_status = "disabled"

# Tool registration also returns the dispatch map for the CLI socket
# (same code, two surfaces — see spec §4.4).
_DISPATCHERS = register_tools(mcp, _API, _STATE, CONFIG, streaming=_STREAMING)

_SOCKET_SERVER: SocketServer | None = None
if not os.environ.get("RC_DISABLE_CLI_SOCKET"):
    _SOCKET_SERVER = SocketServer(_DISPATCHERS)


async def _on_startup() -> None:
    """Spawn the SSE consumer and bind the CLI socket.

    SSE consumer (v0.3.7+): always spawn — the consumer's run loop is
    already resilient to no-creds-at-runtime via _NoTokenSentinel; once
    the user links, the next retry sees the fresh token and connects.

    CLI socket: bind Unix domain socket for `rcct` to talk to. Failure
    here is logged but doesn't crash the MCP — Claude can still use the
    MCP tools directly via stdio.
    """
    if _SSE_CONSUMER is not None:
        try:
            _SSE_CONSUMER.start()
        except Exception as e:
            print(
                f"[remotecodetrol-mcp] failed to start SSE consumer: {e}",
                file=sys.stderr,
                flush=True,
            )

    if _SOCKET_SERVER is not None:
        try:
            await _SOCKET_SERVER.start()
        except Exception as e:
            print(
                f"[remotecodetrol-mcp] failed to bind CLI socket: {e}",
                file=sys.stderr,
                flush=True,
            )


def main() -> None:
    """Console-script entry point. Runs over stdio — what Claude Code expects."""
    logging.basicConfig(
        level=os.environ.get("REMOTECODETROL_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    if hasattr(mcp, "run_async"):
        async def _runner() -> None:
            await _on_startup()
            await mcp.run_async()
        try:
            asyncio.run(_runner())
            return
        except Exception:
            # Fall through to the sync path so a FastMCP version mismatch
            # doesn't take the server down entirely.
            pass

    # Fallback for older FastMCP versions: best-effort startup hook.
    mcp.run()


if __name__ == "__main__":
    main()
