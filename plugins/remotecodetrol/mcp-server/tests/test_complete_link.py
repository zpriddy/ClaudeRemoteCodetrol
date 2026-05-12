"""Tests for the v0.4.0 `complete_link` MCP tool.

Covers the four state transitions:
  - no v4_pending_device_code in state.json → status="invalid"
  - backend authorized → token persisted, pending cleared, status="authorized"
  - backend pending → status="pending", pending KEPT for next call
  - backend 410 expired → pending cleared, status="expired"
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from remotecodetrol_mcp.auth import AuthClient, TokenStore
from remotecodetrol_mcp.client import APIClient
from remotecodetrol_mcp.streaming import StreamingState
from remotecodetrol_mcp.tools import ThreadState, register_tools


class FakeMCP:
    def tool(self, fn):
        return fn


async def _wire(handler, config, tmp_path, *, pre_pending: str | None = None):
    """Build a dispatchers map with state.STATE_PATH redirected and an
    optional v4_pending_device_code already in state.json."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"
    if pre_pending:
        state_mod.write_state({"v4_pending_device_code": pre_pending})

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    store = TokenStore(path=tmp_path / "tokens.json")
    auth = AuthClient(config, http, store)
    api = APIClient(config, auth, http)
    state = ThreadState(config)
    streaming = StreamingState()
    dispatchers = register_tools(
        FakeMCP(), api, state, config, streaming=streaming
    )
    return dispatchers, http, store


async def test_complete_link_invalid_when_no_pending(config, tmp_path):
    """No v4_pending_device_code → status='invalid' with hint to call link()."""

    def h(_: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call should be made when no pending")

    dispatchers, http, _ = await _wire(h, config, tmp_path, pre_pending=None)
    try:
        result = await dispatchers["complete_link"]({})
        assert result.status == "invalid"
        assert "link" in result.message.lower()
    finally:
        await http.aclose()


async def test_complete_link_authorized_persists_and_clears(config, tmp_path):
    """authorized → token stored, pending cleared, returns email."""

    def h(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/check-link"):
            body = json.loads(request.content)
            assert body["device_code"] == "DC-pending"
            return httpx.Response(
                200,
                json={
                    "status": "authorized",
                    "token": "freshly-issued",
                    "expires_in": 14 * 24 * 60 * 60,
                    "rotates_after": 7 * 24 * 60 * 60,
                },
            )
        # whoami() inside complete_link calls api.auth.whoami() → triggers
        # get_access_token then returns the active_email. No HTTP needed
        # for v4 whoami (the email comes from the local store, not the
        # backend).
        return httpx.Response(404)

    dispatchers, http, store = await _wire(
        h, config, tmp_path, pre_pending="DC-pending"
    )
    try:
        result = await dispatchers["complete_link"]({})
        assert result.status == "authorized"
        # email from the store; AuthClient defaults to "default" when no
        # active email was set pre-link.
        assert result.email is not None

        # Token persisted.
        persisted = store.get_token(store.get_active_email() or "default")
        assert persisted is not None
        assert persisted.token == "freshly-issued"

        # Pending state cleared.
        import remotecodetrol_mcp.state as state_mod
        assert state_mod.read_state().get("v4_pending_device_code") is None
    finally:
        await http.aclose()


async def test_complete_link_pending_keeps_state(config, tmp_path):
    """pending → status='pending', pending kept on disk."""

    def h(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/check-link"):
            return httpx.Response(200, json={"status": "pending"})
        return httpx.Response(404)

    dispatchers, http, store = await _wire(
        h, config, tmp_path, pre_pending="DC-still-waiting"
    )
    try:
        result = await dispatchers["complete_link"]({})
        assert result.status == "pending"
        assert "pending" in result.message.lower() or "confirm" in result.message.lower()

        # Pending state preserved for the next call.
        import remotecodetrol_mcp.state as state_mod
        assert state_mod.read_state().get("v4_pending_device_code") == "DC-still-waiting"

        # No token persisted on pending.
        assert store.get_token(store.get_active_email() or "default") is None
    finally:
        await http.aclose()


async def test_complete_link_expired_clears_state(config, tmp_path):
    """Backend 410 with expired → pending cleared, status='expired'."""

    def h(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/check-link"):
            return httpx.Response(410, json={"status": "expired"})
        return httpx.Response(404)

    dispatchers, http, _ = await _wire(
        h, config, tmp_path, pre_pending="DC-old"
    )
    try:
        result = await dispatchers["complete_link"]({})
        assert result.status == "expired"

        # Pending state cleared.
        import remotecodetrol_mcp.state as state_mod
        assert state_mod.read_state().get("v4_pending_device_code") is None
    finally:
        await http.aclose()
