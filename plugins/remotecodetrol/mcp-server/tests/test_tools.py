"""Integration-style tests for the FastMCP tools.

We register tools onto a fresh FastMCP instance per test, point the API
client at an httpx MockTransport, and call tools through the FastMCP
`call_tool` interface to verify wire-compatible behavior.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import FastMCP

from remotecodetrol_mcp.auth import AuthClient, TokenBundle, TokenStore
from remotecodetrol_mcp.client import APIClient
from remotecodetrol_mcp.tools import ThreadState, register_tools


pytestmark = pytest.mark.asyncio


def _bundle(access: str, email: str = "user@example.com") -> TokenBundle:
    return TokenBundle(
        access_token=access, refresh_token="r", expires_at=time.time() + 900, email=email
    )


async def _build_mcp(handler, fake_keyring, config, jwt_factory, tmp_path: Path):
    # State file lives in state.py now (shared between auth + tools).
    # Patch its module-level STATE_PATH directly so reads/writes are
    # isolated to a tmp file for this test.
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    store = TokenStore(config.keychain_service)
    auth = AuthClient(config, http, store)
    auth._cached = _bundle(jwt_factory(sub="user@example.com"))
    api = APIClient(config, auth, http)
    state = ThreadState(config)

    mcp = FastMCP("test-rcct")
    register_tools(mcp, api, state, config)
    return mcp, http, state


async def _call(mcp: FastMCP, _tool: str, **kwargs):
    result = await mcp.call_tool(_tool, kwargs)
    # FastMCP returns a ToolResult; grab the structured payload.
    return result.structured_content if hasattr(result, "structured_content") else result


async def test_thread_state_visible_across_instances(
    fake_keyring, config, isolated_state
):
    """Two ThreadState instances pointing at the same state.json — one
    writes, the other should read the new value WITHOUT being re-init'd.

    Regression test for the cross-session staleness bug where ThreadState
    cached `_active` at construction and never re-read.
    """
    a = ThreadState(config)
    b = ThreadState(config)

    assert a.get() is None
    assert b.get() is None

    a.set("blah")
    # b never had set() called — it must pick up the change from disk.
    assert b.get() == "blah"

    a.set("test")
    assert b.get() == "test"


async def test_set_thread_persists(fake_keyring, config, jwt_factory, tmp_path):
    def h(_):
        return httpx.Response(200, json={})

    mcp, http, state = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        out = await _call(mcp, "set_thread", name="dev")
        assert out["active_thread"] == "dev"
        assert state.get() == "dev"

        # Re-read from disk to confirm persistence.
        import remotecodetrol_mcp.state as state_mod
        on_disk = json.loads(state_mod.STATE_PATH.read_text())
        assert on_disk["active_thread"] == "dev"
    finally:
        await http.aclose()


async def test_send_message_uses_active_thread(
    fake_keyring, config, jwt_factory, tmp_path
):
    captured: dict = {}

    def h(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"messageId": "msg_123"})

    mcp, http, state = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        await _call(mcp, "set_thread", name="alerts")
        out = await _call(
            mcp,
            "send_message",
            body="hello",
            require_response=True,
            idempotency_key="k1",
        )
        assert out["messageId"] == "msg_123"
        assert out["thread"] == "alerts"
        assert captured["path"].endswith("/v1/threads/alerts/messages")
        assert captured["body"] == {
            "body": "hello",
            "requireResponse": True,
            "idempotencyKey": "k1",
        }
    finally:
        await http.aclose()


async def test_send_message_errors_without_thread(
    fake_keyring, config, jwt_factory, tmp_path
):
    def h(_):
        return httpx.Response(200, json={"messageId": "x"})

    mcp, http, _ = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        with pytest.raises(Exception) as ei:
            await _call(mcp, "send_message", body="hi")
        assert "thread" in str(ei.value).lower()
    finally:
        await http.aclose()


async def test_peek_parses_backend_wire_format_with_messageId(
    fake_keyring, config, jwt_factory, tmp_path
):
    """Regression: the backend returns `messageId` (not `id`) and `ackedAt`
    on each message. The Message Pydantic model must alias both correctly
    or every message gets rejected and Claude sees zero replies even
    though the user sent some."""

    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/messages" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "messageId": "m1",  # ← backend's actual field name
                            "body": "yo",
                            "senderType": "user",
                            "senderId": "uid",
                            "createdAt": "2026-05-05T12:00:00Z",
                            "ackedAt": None,
                        },
                        {
                            "messageId": "m2",
                            "body": "still here",
                            "senderType": "user",
                            "senderId": "uid",
                            "createdAt": "2026-05-05T12:01:00Z",
                            "ackedAt": None,
                        },
                    ],
                    "cursor": "2026-05-05T12:01:00Z",
                },
            )
        return httpx.Response(404)

    mcp, http, _ = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        await _call(mcp, "set_thread", name="t")
        peek = await _call(mcp, "peek_messages")
        # Pydantic accepted the messageId-keyed input (would 8-error
        # without the alias). FastMCP serializes back by_alias=True so
        # the output dict still has messageId — that's fine; Claude
        # reads whichever field name is present.
        assert len(peek["messages"]) == 2
        m0 = peek["messages"][0]
        m_id = m0.get("messageId") or m0.get("id")
        assert m_id == "m1"
        assert m0["body"] == "yo"
    finally:
        await http.aclose()


async def test_peek_and_ack(fake_keyring, config, jwt_factory, tmp_path):
    captured: list = []

    def h(request: httpx.Request) -> httpx.Response:
        captured.append((request.method, request.url.path, request.url.query.decode()))
        if request.method == "GET" and "/messages" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "id": "m1",
                            "body": "hi",
                            "senderType": "user",
                            "createdAt": "2026-05-05T12:00:00Z",
                        }
                    ],
                    "cursor": "2026-05-05T12:00:00Z",
                },
            )
        if request.method == "POST" and "/ack" in request.url.path:
            assert json.loads(request.content) == {"messageIds": ["m1"]}
            return httpx.Response(200, json={})
        return httpx.Response(404)

    mcp, http, _ = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        await _call(mcp, "set_thread", name="t")
        peek = await _call(mcp, "peek_messages")
        assert len(peek["messages"]) == 1
        m0 = peek["messages"][0]
        # Mock returned `id` (legacy) — alias accepts it; output is by-alias.
        assert (m0.get("messageId") or m0.get("id")) == "m1"

        ack = await _call(mcp, "ack_messages", message_ids=["m1"])
        assert ack["acked"] == 1
    finally:
        await http.aclose()


async def test_thread_override_param(fake_keyring, config, jwt_factory, tmp_path):
    seen: list[str] = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"messageId": "abc"})

    mcp, http, _ = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        await _call(
            mcp,
            "send_message",
            body="x",
            thread="explicit",
        )
        assert seen[-1].endswith("/v1/threads/explicit/messages")
    finally:
        await http.aclose()


async def test_list_threads(fake_keyring, config, jwt_factory, tmp_path):
    def h(_):
        return httpx.Response(
            200,
            json={
                "threads": [
                    {"name": "alerts", "lastMessageAt": "2026-05-04T10:00:00Z"},
                    {"name": "dev"},
                ]
            },
        )

    mcp, http, _ = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        result = await _call(mcp, "list_threads")
        # FastMCP wraps list returns in {"result": [...]} structured content.
        items = result["result"] if isinstance(result, dict) and "result" in result else result
        assert len(items) == 2
        assert items[0]["name"] == "alerts"
    finally:
        await http.aclose()


async def test_whoami(fake_keyring, config, jwt_factory, tmp_path):
    def h(_):
        return httpx.Response(404)

    mcp, http, state = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        state.set("primary")
        out = await _call(mcp, "whoami")
        assert out["email"] == "user@example.com"
        assert out["default_thread"] == "primary"
    finally:
        await http.aclose()


async def test_wait_for_response_returns_messages(
    fake_keyring, config, jwt_factory, tmp_path, monkeypatch
):
    poll_counter = {"n": 0}

    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            poll_counter["n"] += 1
            if poll_counter["n"] < 2:
                return httpx.Response(200, json={"messages": [], "cursor": None})
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "id": "m9",
                            "body": "go",
                            "senderType": "user",
                            "createdAt": "2026-05-05T13:00:00Z",
                        }
                    ],
                    "cursor": None,
                },
            )
        return httpx.Response(200, json={})  # ack

    async def _no_sleep(_):
        return None

    monkeypatch.setattr("remotecodetrol_mcp.tools.asyncio.sleep", _no_sleep)
    mcp, http, _ = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        await _call(mcp, "set_thread", name="t")
        out = await _call(
            mcp,
            "wait_for_response",
            timeout_minutes=1,
            poll_interval_seconds=1,
        )
        assert len(out["messages"]) == 1
        m0 = out["messages"][0]
        assert (m0.get("messageId") or m0.get("id")) == "m9"
    finally:
        await http.aclose()


async def test_wait_for_response_timeout_returns_empty(
    fake_keyring, config, jwt_factory, tmp_path, monkeypatch
):
    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [], "cursor": None})

    async def _no_sleep(_):
        return None

    # Force monotonic to advance past the deadline so the loop exits.
    real_monotonic = time.monotonic
    start = real_monotonic()
    fake_clock = {"t": start}

    def fake_monotonic():
        fake_clock["t"] += 100
        return fake_clock["t"]

    monkeypatch.setattr("remotecodetrol_mcp.tools.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("remotecodetrol_mcp.tools.time.monotonic", fake_monotonic)

    mcp, http, _ = await _build_mcp(h, fake_keyring, config, jwt_factory, tmp_path)
    try:
        await _call(mcp, "set_thread", name="t")
        out = await _call(
            mcp,
            "wait_for_response",
            timeout_minutes=1,
            poll_interval_seconds=1,
        )
        assert out["messages"] == []
    finally:
        await http.aclose()
