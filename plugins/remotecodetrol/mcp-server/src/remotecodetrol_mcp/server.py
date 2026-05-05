"""FastMCP entry point for the RemoteCodetrol MCP server.

Run via the `remotecodetrol-mcp` console script or `python -m
remotecodetrol_mcp.server`. Exposes module-level `mcp` so introspection
tools (and tests) can list registered tools without spinning up the server.
"""

from __future__ import annotations

import httpx
from fastmcp import FastMCP

from .auth import AuthClient
from .client import APIClient
from .config import load_config
from .tools import ThreadState, register_tools


CONFIG = load_config()
mcp: FastMCP = FastMCP("remotecodetrol")

# A single long-lived httpx client so connections + DNS get reused.
_HTTP = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
_AUTH = AuthClient(CONFIG, _HTTP)
_API = APIClient(CONFIG, _AUTH, _HTTP)
_STATE = ThreadState(CONFIG)

register_tools(mcp, _API, _STATE, CONFIG)


def main() -> None:
    """Console-script entry point. Runs over stdio — what Claude Code expects."""
    mcp.run()


if __name__ == "__main__":
    main()
