"""FastMCP entry point for the RemoteCodetrol MCP server.

Run via the `remotecodetrol-mcp` console script or `python -m
remotecodetrol_mcp.server`. Exposes module-level `mcp` so introspection
tools (and tests) can list registered tools without spinning up the server.

Lifecycle (v0.3.0):
  - Tools are registered immediately at import time (so FastMCP's tool
    listing works without an event loop).
  - The SSE consumer is spawned eagerly on `mcp.run()` startup if a token
    is already present in the keychain (§5: eager startup is required for
    between-turn awareness).
  - `RC_DISABLE_STREAMING=1` skips the SSE consumer entirely; tools fall
    back to direct API calls only.
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

# Streaming state lives for the lifetime of the MCP process.
_STREAMING = StreamingState()
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

register_tools(mcp, _API, _STATE, CONFIG, streaming=_STREAMING)


async def _on_startup() -> None:
    """Spawn the SSE consumer unconditionally.

    v0.3.7: removed the prior `_have_credentials()` gate. Pre-0.3.7,
    if there were no creds at MCP startup we skipped the spawn — but
    that meant a fresh install had to:
      1. Start MCP (no creds, consumer not spawned)
      2. Run /remotecodetrol:link (token written to file)
      3. Restart Claude Code so a NEW MCP process spawns the consumer
    The "restart twice" friction we hit through v0.3.0–0.3.6.

    The streaming.py run loop is already resilient to no-creds-at-runtime
    (v0.3.3): it raises `_NoTokenSentinel`, the loop catches it, sets
    `sse_status = waiting_for_link`, sleeps `WAITING_FOR_LINK_RETRY_S`
    (10s), and retries. Once the user links, the next retry sees the
    fresh refresh token and connects normally — all in-process, no
    second restart required.

    The cost of unconditional spawn is one ~10s no-op cycle while
    waiting for the user to link. Cheap.
    """
    if _SSE_CONSUMER is None:
        return
    try:
        _SSE_CONSUMER.start()
    except Exception as e:
        print(
            f"[remotecodetrol-mcp] failed to start SSE consumer: {e}",
            file=sys.stderr,
            flush=True,
        )


def main() -> None:
    """Console-script entry point. Runs over stdio — what Claude Code expects.

    FastMCP's `run()` manages its own event loop; we hook into the same
    loop via `asyncio.get_event_loop()` once it's running. To avoid digging
    into FastMCP internals, we kick off the SSE consumer the first time
    asyncio is awaited — by patching the FastMCP run method to schedule
    `_on_startup` before relinquishing control.
    """
    logging.basicConfig(
        level=os.environ.get("REMOTECODETROL_LOG_LEVEL", "WARNING"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # Schedule startup on the loop FastMCP creates. FastMCP exposes its
    # async machinery via `run_async` (when available) which we can wrap.
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

    # Fallback for older FastMCP versions: best-effort startup hook by
    # calling _on_startup once a fresh event loop is created. The tools
    # remain functional even if SSE never starts.
    mcp.run()


if __name__ == "__main__":
    main()
