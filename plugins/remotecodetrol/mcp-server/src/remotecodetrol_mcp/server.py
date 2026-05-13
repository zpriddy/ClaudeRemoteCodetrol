"""FastMCP entry point for the RemoteCodetrol MCP server.

Run via the `remotecodetrol-mcp` console script or `python -m
remotecodetrol_mcp.server`. Exposes module-level `mcp` so introspection
tools (and tests) can list registered tools without spinning up the server.

Lifecycle (v0.6.0):
  - Tools are registered immediately at import time (so FastMCP's tool
    listing works without an event loop).
  - The PollingConsumer (was: SSE consumer) is spawned eagerly on
    `mcp.run()` startup if a token is already present in tokens.json.
    It polls `/v1/threads/{tid}/messages?unackedOnly=true` on a
    cost-optimized cadence (60s idle → 300s ceiling, 5s busy, 2s while
    a tool is blocked waiting). The `stream` Cloud Function was deleted
    in v0.6.0 — polling is the only consumer.
  - The CLI socket server (Unix domain socket dispatcher for `rcct`) is
    bound at startup alongside the polling consumer.
  - `known_threads` is seeded from REMOTECODETROL_KNOWN_THREADS env var
    at startup; tools augment it at runtime via set_thread / send_message.
  - `RC_DISABLE_POLLING=1` skips the polling consumer entirely; tools
    fall back to per-call direct API requests.
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
from .leader import LeaderElector, default_lockfile_path
from .polling import PollingConsumer, PollingPolicy
from .socket_server import SocketServer
from .state_file import make_writer
from .streaming import StreamingState
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

# v0.4.4+: also grandfather the persisted active_thread from state.json into
# known_threads at boot. Without this, a Claude Code restart leaves the user
# with an active_thread that's NOT in the allowlist, so peek_messages /
# ack_messages on it fail with "thread not in known_threads" until the user
# explicitly re-declares intent. Sending still worked (auto_add=True path)
# but read flows broke. Treating an already-set active_thread as a prior
# declaration of intent is the natural fix — the user already chose it
# in a prior session.
_persisted_active = _STATE.get()
if _persisted_active:
    _STREAMING.add_known_thread(_persisted_active)
    _STREAMING.active_thread = _persisted_active

_POLLING_CONSUMER: PollingConsumer | None = None
if not os.environ.get("RC_DISABLE_POLLING"):
    # v0.6.1: leader-elected polling — only one MCP per host runs the
    # poll loop, regardless of how many Claude sessions the user has
    # open. Losers (followers) skip polling entirely and serve
    # peek_messages from the leader's pending.json. RC_DISABLE_LEADER
    # skips election entirely (every MCP polls); useful for tests or
    # for diagnosing the leader path itself.
    _LEADER: LeaderElector | None = None
    if not os.environ.get("RC_DISABLE_LEADER"):
        _LEADER = LeaderElector(lockfile_path=default_lockfile_path())
    _POLLING_CONSUMER = PollingConsumer(
        _API,
        _STREAMING,
        policy=PollingPolicy(),
        state_file_writer=make_writer(),
        leader=_LEADER,
    )
else:
    _STREAMING.sse_status = "disabled"

# Tool registration also returns the dispatch map for the CLI socket
# (same code, two surfaces — see spec §4.4). The polling consumer is
# passed through so `send_message(wait=True)` and `wait_for_response`
# can toggle the tight-poll mode for the duration of a blocking call.
_DISPATCHERS = register_tools(
    mcp,
    _API,
    _STATE,
    CONFIG,
    streaming=_STREAMING,
    polling=_POLLING_CONSUMER,
)

_SOCKET_SERVER: SocketServer | None = None
if not os.environ.get("RC_DISABLE_CLI_SOCKET"):
    _SOCKET_SERVER = SocketServer(_DISPATCHERS)


async def _on_startup() -> None:
    """Spawn the polling consumer and bind the CLI socket.

    Polling consumer (v0.6.0+): always spawn when not disabled. The
    consumer is resilient to no-creds-at-runtime — until a token
    exists, polls return 401 and the loop just keeps trying at the
    idle interval. Once the user links, the next poll sees the fresh
    token and starts populating the cache.

    CLI socket: bind Unix domain socket for `rcct` to talk to. Failure
    here is logged but doesn't crash the MCP — Claude can still use the
    MCP tools directly via stdio.
    """
    if _POLLING_CONSUMER is not None:
        try:
            _POLLING_CONSUMER.start()
        except Exception as e:
            print(
                f"[remotecodetrol-mcp] failed to start polling consumer: {e}",
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
