"""v0.7.0: `get_messages` combined peek + ack tool.

Tests the happy path (peek returns N → ack all N → return all N) and
the empty path (peek returns 0 → no ack → return empty)."""

from __future__ import annotations

import json
import time as _time
from typing import Any

import httpx
import pytest

from remotecodetrol_mcp.auth import AuthClient, TokenStore
from remotecodetrol_mcp.client import APIClient
from remotecodetrol_mcp.config import Config
from remotecodetrol_mcp.streaming import StreamingState
from remotecodetrol_mcp.tools import ThreadState, register_tools


class FakeMCP:
    def tool(self, fn):
        return fn


def _config(tmp_path) -> Config:
    return Config(
        api_base="https://api.test.invalid",
        stream_url="https://stream.test.invalid",
        default_thread=None,
        device_label="test",
        default_poll_interval_seconds=1,
        default_timeout_minutes=1,
        mcp_token_ttl_sec=14 * 24 * 60 * 60,
        mcp_token_rotate_after_sec=7 * 24 * 60 * 60,
        known_threads_seed=(),
    )


async def _build_dispatchers(handler, tmp_path):
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    store = TokenStore(path=tmp_path / "tokens.json")
    store.set_active_email("user@example.com")
    store.store_token(
        email="user@example.com",
        token="valid-tok",
        expires_at=_time.time() + 14 * 24 * 60 * 60,
        rotates_at=_time.time() + 7 * 24 * 60 * 60,
    )
    config = _config(tmp_path)
    auth = AuthClient(config, http, store)
    api = APIClient(config, auth, http)
    state = ThreadState(config)
    streaming = StreamingState()
    streaming.add_known_thread("work")  # so the thread passes the allowlist check
    mcp = FakeMCP()
    dispatchers = register_tools(mcp, api, state, config, streaming=streaming)
    return dispatchers, http, streaming


@pytest.mark.asyncio
async def test_get_messages_happy_path(tmp_path):
    """Peek returns 2 messages → both get acked → tool returns both
    messages + acked=2."""
    requests: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                pass
        requests.append((request.method, request.url.path, body))

        if request.method == "GET" and "/threads/work/messages" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"id": "m1", "thread_id": "work", "body": "hi", "sender_type": "user"},
                        {"id": "m2", "thread_id": "work", "body": "yo", "sender_type": "user"},
                    ],
                    "cursor": None,
                },
            )
        if request.method == "POST" and request.url.path.endswith("/ack"):
            return httpx.Response(200, json={"acked": 2})
        return httpx.Response(404)

    dispatchers, http, streaming = await _build_dispatchers(handler, tmp_path)
    try:
        result = await dispatchers["get_messages"]({"thread": "work"})
        # Result is the GetMessagesResult Pydantic model — access by attr.
        assert result.acked == 2
        assert [m.id for m in result.messages] == ["m1", "m2"]
        # Verify the request sequence: one GET (peek) followed by one
        # POST (ack) with both ids.
        methods = [r[0] for r in requests]
        assert methods == ["GET", "POST"], (
            f"expected one peek then one ack, got: {methods}"
        )
        ack_body = requests[1][2]
        assert ack_body is not None
        assert sorted(ack_body["messageIds"]) == ["m1", "m2"]
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_get_messages_empty_skips_ack(tmp_path):
    """When peek returns no messages, the tool MUST NOT POST an empty
    ack — the backend rejects empty message_ids and there's nothing
    to ack anyway."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and "/threads/work/messages" in request.url.path:
            return httpx.Response(200, json={"messages": [], "cursor": None})
        if request.method == "POST" and request.url.path.endswith("/ack"):
            # Should never be reached when peek was empty.
            return httpx.Response(400, json={"error": "empty"})
        return httpx.Response(404)

    dispatchers, http, _ = await _build_dispatchers(handler, tmp_path)
    try:
        result = await dispatchers["get_messages"]({"thread": "work"})
        assert result.acked == 0
        assert result.messages == []
        # Verify we made ONLY the peek call, never the ack. The
        # APIClient prefixes routes with `/v1` — we match by suffix
        # so the test isn't coupled to the API versioning detail.
        assert len(requests) == 1
        assert requests[0].startswith("GET ")
        assert requests[0].endswith("/threads/work/messages")
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_get_messages_rejects_unknown_thread(tmp_path):
    """Threads not in known_threads must reject before the API call —
    same allowlist behavior as peek/ack."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("get_messages should reject before calling API")

    dispatchers, http, _ = await _build_dispatchers(handler, tmp_path)
    try:
        with pytest.raises(ValueError, match="known_threads"):
            await dispatchers["get_messages"]({"thread": "stranger"})
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_get_messages_prunes_cache_after_ack(tmp_path):
    """After a successful ack, the in-memory streaming cache must drop
    the acked messages so peek/hook don't re-surface them."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/threads/work/messages" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"id": "m1", "thread_id": "work", "body": "hi", "sender_type": "user"},
                    ],
                    "cursor": None,
                },
            )
        if request.method == "POST" and request.url.path.endswith("/ack"):
            return httpx.Response(200, json={"acked": 1})
        return httpx.Response(404)

    dispatchers, http, streaming = await _build_dispatchers(handler, tmp_path)
    try:
        # Pre-populate the cache so we can verify pruning.
        streaming.add_message({"id": "m1", "thread_id": "work", "body": "hi"})
        assert streaming.pending.get("work")  # was populated

        await dispatchers["get_messages"]({"thread": "work"})

        assert streaming.pending.get("work") is None or streaming.pending.get("work") == []
    finally:
        await http.aclose()
