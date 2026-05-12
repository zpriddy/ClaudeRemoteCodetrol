"""v0.4.0 known_threads allowlist behavior.

Two layers:
  1. StreamingState methods (add_known_thread, forget_known_thread,
     replace_snapshot/add_message filtering).
  2. Tool enforcement via the dispatchers map returned by register_tools
     (set_thread / send_message auto-add; peek_messages / ack_messages
     reject unknown threads).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from remotecodetrol_mcp.auth import AuthClient, TokenStore
from remotecodetrol_mcp.client import APIClient
from remotecodetrol_mcp.streaming import StreamingState
from remotecodetrol_mcp.tools import ThreadState, register_tools


# ---------- StreamingState.known_threads ----------


def test_add_known_thread_first_time_returns_true():
    s = StreamingState()
    assert s.add_known_thread("foo") is True
    assert s.is_thread_known("foo")


def test_add_known_thread_second_time_returns_false():
    s = StreamingState()
    s.add_known_thread("foo")
    assert s.add_known_thread("foo") is False


def test_forget_known_thread_returns_true_when_known():
    s = StreamingState()
    s.add_known_thread("foo")
    assert s.forget_known_thread("foo") is True
    assert not s.is_thread_known("foo")


def test_forget_known_thread_returns_false_when_unknown():
    s = StreamingState()
    assert s.forget_known_thread("never-added") is False


def test_forget_known_thread_clears_active_thread_when_match():
    s = StreamingState()
    s.add_known_thread("foo")
    s.active_thread = "foo"
    s.forget_known_thread("foo")
    assert s.active_thread is None


def test_forget_known_thread_does_not_clear_active_when_different():
    s = StreamingState()
    s.add_known_thread("foo")
    s.add_known_thread("bar")
    s.active_thread = "bar"
    s.forget_known_thread("foo")
    assert s.active_thread == "bar"


def test_forget_known_thread_drops_pending_for_thread():
    s = StreamingState()
    s.add_known_thread("foo")
    s.add_message({"id": "m1", "thread_id": "foo", "body": "x"})
    assert "foo" in s.pending
    s.forget_known_thread("foo")
    assert "foo" not in s.pending


def test_is_thread_known_reflects_state():
    s = StreamingState()
    assert not s.is_thread_known("foo")
    s.add_known_thread("foo")
    assert s.is_thread_known("foo")
    s.forget_known_thread("foo")
    assert not s.is_thread_known("foo")


def test_replace_snapshot_filters_unknown_threads_when_allowlist_set():
    s = StreamingState()
    s.add_known_thread("allowed")
    msgs = [
        {"id": "m1", "thread_id": "allowed", "body": "yes"},
        {"id": "m2", "thread_id": "denied", "body": "no"},
    ]
    s.replace_snapshot(msgs)
    assert "allowed" in s.pending
    assert "denied" not in s.pending


def test_add_message_drops_unknown_thread_when_allowlist_set():
    s = StreamingState()
    s.add_known_thread("allowed")
    accepted = s.add_message({"id": "m1", "thread_id": "allowed", "body": "y"})
    assert accepted is True
    rejected = s.add_message({"id": "m2", "thread_id": "denied", "body": "n"})
    assert rejected is False
    assert "denied" not in s.pending


def test_empty_known_threads_accepts_everything():
    """Back-compat: when allowlist is empty, both filters accept all."""
    s = StreamingState()
    assert len(s.known_threads) == 0
    # add_message accepts any thread
    assert s.add_message({"id": "m1", "thread_id": "anything", "body": "x"}) is True
    # replace_snapshot keeps every entry
    s2 = StreamingState()
    s2.replace_snapshot([
        {"id": "a", "thread_id": "t1", "body": "1"},
        {"id": "b", "thread_id": "t2", "body": "2"},
    ])
    assert set(s2.pending.keys()) == {"t1", "t2"}


# ---------- Tool enforcement via dispatchers ----------


class FakeMCP:
    """FastMCP stand-in: @mcp.tool used as a bare decorator returns fn."""

    def tool(self, fn):
        return fn


async def _build_dispatchers(
    handler, config, tmp_path, *, streaming: StreamingState | None = None,
    pre_authorize: bool = True,
):
    """Wire APIClient + ThreadState + AuthClient with a v4 token already
    installed (so api.post / api.get don't trip NotAuthorizedError) and
    return the dispatchers dict."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    store = TokenStore(path=tmp_path / "tokens.json")
    if pre_authorize:
        import time
        store.set_active_email("user@example.com")
        store.store_token(
            email="user@example.com",
            token="valid-tok",
            expires_at=time.time() + 14 * 24 * 60 * 60,
            rotates_at=time.time() + 7 * 24 * 60 * 60,
        )
    auth = AuthClient(config, http, store)
    api = APIClient(config, auth, http)
    state = ThreadState(config)
    mcp = FakeMCP()
    streaming = streaming if streaming is not None else StreamingState()
    dispatchers = register_tools(mcp, api, state, config, streaming=streaming)
    return dispatchers, http, streaming, state


async def test_set_thread_adds_to_known_threads(config, tmp_path):
    def h(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    dispatchers, http, streaming, _ = await _build_dispatchers(h, config, tmp_path)
    try:
        result = await dispatchers["set_thread"]({"name": "foo"})
        assert result.active_thread == "foo"
        assert streaming.is_thread_known("foo")
    finally:
        await http.aclose()


async def test_send_message_auto_adds_to_known_threads(config, tmp_path):
    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/messages" in request.url.path:
            return httpx.Response(
                200,
                json={"messageId": "out1", "pending_messages": [], "pending_count": 0},
            )
        return httpx.Response(404)

    dispatchers, http, streaming, _ = await _build_dispatchers(h, config, tmp_path)
    try:
        assert not streaming.is_thread_known("bar")
        result = await dispatchers["send_message"]({"body": "hi", "thread": "bar"})
        assert result.message_id == "out1"
        assert streaming.is_thread_known("bar")
    finally:
        await http.aclose()


async def test_peek_messages_rejects_unknown_thread(config, tmp_path):
    def h(_: httpx.Request) -> httpx.Response:
        raise AssertionError("peek_messages should reject before calling API")

    dispatchers, http, streaming, _ = await _build_dispatchers(h, config, tmp_path)
    try:
        # `baz` not in known_threads.
        with pytest.raises(ValueError, match="known_threads"):
            await dispatchers["peek_messages"]({"thread": "baz"})
    finally:
        await http.aclose()


async def test_ack_messages_rejects_unknown_thread(config, tmp_path):
    def h(_: httpx.Request) -> httpx.Response:
        raise AssertionError("ack_messages should reject before calling API")

    dispatchers, http, streaming, _ = await _build_dispatchers(h, config, tmp_path)
    try:
        with pytest.raises(ValueError, match="known_threads"):
            await dispatchers["ack_messages"]({
                "message_ids": ["m1"],
                "thread": "baz",
            })
    finally:
        await http.aclose()


async def test_peek_messages_works_after_set_thread_declares_intent(
    config, tmp_path
):
    """Sanity: declaring intent via set_thread unblocks peek on that thread."""
    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and "/messages" in request.url.path:
            return httpx.Response(200, json={"messages": [], "cursor": None})
        return httpx.Response(404)

    streaming = StreamingState()
    # cache_is_fresh will be False (sse_status default) so it'll go to API.
    dispatchers, http, _, _ = await _build_dispatchers(
        h, config, tmp_path, streaming=streaming
    )
    try:
        await dispatchers["set_thread"]({"name": "alpha"})
        result = await dispatchers["peek_messages"]({"thread": "alpha"})
        assert result.messages == []
    finally:
        await http.aclose()
