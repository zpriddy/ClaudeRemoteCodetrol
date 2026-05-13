"""v0.6.0 polling consumer — next_interval algorithm + consumer state machine."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from remotecodetrol_mcp.auth import AuthClient, TokenStore
from remotecodetrol_mcp.client import APIClient
from remotecodetrol_mcp.polling import (
    PollingConsumer,
    PollingPolicy,
    next_interval,
)
from remotecodetrol_mcp.streaming import StreamingState


# ---------- next_interval: pure function tests ----------


def test_next_interval_waiting_overrides_everything():
    """When a tool is actively blocked (wait=True), use waiting cadence
    even if there's pending. Caller has signaled max urgency."""
    p = PollingPolicy(interval_busy=5.0, interval_waiting=2.0)
    assert (
        next_interval(p, has_pending=True, is_waiting=True, current_idle_interval=60.0)
        == 2.0
    )
    assert (
        next_interval(p, has_pending=False, is_waiting=True, current_idle_interval=300.0)
        == 2.0
    )


def test_next_interval_busy_when_pending():
    p = PollingPolicy(interval_busy=5.0, interval_idle_start=60.0)
    assert (
        next_interval(p, has_pending=True, is_waiting=False, current_idle_interval=60.0)
        == 5.0
    )


def test_next_interval_idle_grows_by_backoff_factor():
    """Empty polls multiply the current interval until the ceiling."""
    p = PollingPolicy(
        interval_idle_start=60.0, interval_idle_max=300.0, backoff_factor=2.0
    )
    # 60 → 120 → 240 → 300 (capped)
    assert next_interval(p, has_pending=False, is_waiting=False, current_idle_interval=60.0) == 120.0
    assert next_interval(p, has_pending=False, is_waiting=False, current_idle_interval=120.0) == 240.0
    assert next_interval(p, has_pending=False, is_waiting=False, current_idle_interval=240.0) == 300.0
    # Already at ceiling — stays put.
    assert next_interval(p, has_pending=False, is_waiting=False, current_idle_interval=300.0) == 300.0


def test_next_interval_option_b_defaults():
    """Pin the v0.6.0 cost-optimized defaults so a future tuning
    accident gets caught."""
    p = PollingPolicy()
    assert p.interval_busy == 5.0
    assert p.interval_idle_start == 60.0
    assert p.interval_idle_max == 300.0
    assert p.backoff_factor == 2.0
    assert p.interval_waiting == 2.0


# ---------- PollingConsumer: integration with the mock backend ----------


def _make_api(handler, tmp_path) -> tuple[APIClient, httpx.AsyncClient]:
    import time as _time

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
    from remotecodetrol_mcp.config import Config

    config = Config(
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
    auth = AuthClient(config, http, store)
    return APIClient(config, auth, http), http


@pytest.mark.asyncio
async def test_polling_consumer_populates_cache_from_api(tmp_path, monkeypatch):
    """One full poll cycle fetches messages for every known thread and
    deposits them into the cache."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if "/threads/work/messages" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"id": "m1", "thread_id": "work", "body": "hi"},
                    ],
                    "cursor": None,
                },
            )
        return httpx.Response(200, json={"messages": [], "cursor": None})

    api, http = _make_api(handler, tmp_path)
    state = StreamingState()
    state.add_known_thread("work")
    state.add_known_thread("other")

    consumer = PollingConsumer(
        api, state, policy=PollingPolicy(interval_idle_start=0.05, interval_idle_max=0.1)
    )
    try:
        consumer.start()
        # Give it enough wall-clock for one full cycle. The first cycle
        # always runs immediately on start().
        await asyncio.sleep(0.2)
        # `work` should now be in pending; `other` returned empty.
        assert "work" in state.pending
        assert state.pending["work"][0]["id"] == "m1"
        # Both threads were polled.
        assert any("/threads/work/messages" in p for p in seen_paths)
        assert any("/threads/other/messages" in p for p in seen_paths)
    finally:
        await consumer.stop()
        await http.aclose()


@pytest.mark.asyncio
async def test_polling_consumer_set_waiting_tightens_cadence(tmp_path):
    """set_waiting(True) should drop the next sleep to interval_waiting.
    We verify by counting polls in a fixed window."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        poll_count += 1
        return httpx.Response(200, json={"messages": [], "cursor": None})

    api, http = _make_api(handler, tmp_path)
    state = StreamingState()
    state.add_known_thread("t")

    # Idle = 1s (slow), waiting = 50ms (fast). In 250ms we expect ≥3
    # polls in waiting mode vs ≤1 in idle.
    policy = PollingPolicy(
        interval_idle_start=1.0,
        interval_idle_max=1.0,
        interval_waiting=0.05,
        backoff_factor=1.0,  # don't grow during this test
    )
    consumer = PollingConsumer(api, state, policy=policy)
    try:
        consumer.set_waiting(True)
        consumer.start()
        await asyncio.sleep(0.25)
        assert poll_count >= 3, f"expected ≥3 polls in waiting mode, got {poll_count}"
    finally:
        await consumer.stop()
        await http.aclose()


@pytest.mark.asyncio
async def test_polling_consumer_normalizes_camelcase(tmp_path):
    """Backend-emitted camelCase (responseOptions, repliedTo, etc.)
    must land in the cache as snake_case so the rest of the code
    doesn't have to handle both shapes."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "id": "m1",
                        "threadId": "t",
                        "senderType": "claude",
                        "body": "x",
                        "responseOptions": [{"id": "a", "label": "A"}],
                        "selectionMode": "single",
                    }
                ],
                "cursor": None,
            },
        )

    api, http = _make_api(handler, tmp_path)
    state = StreamingState()
    state.add_known_thread("t")
    consumer = PollingConsumer(
        api, state, policy=PollingPolicy(interval_idle_start=0.05)
    )
    try:
        consumer.start()
        await asyncio.sleep(0.15)
        cached = state.pending.get("t", [])
        assert cached, "expected the message in cache"
        msg = cached[0]
        # snake_case keys present after normalization
        assert msg["thread_id"] == "t"
        assert msg["sender_type"] == "claude"
        assert msg["response_options"] == [{"id": "a", "label": "A"}]
        assert msg["selection_mode"] == "single"
    finally:
        await consumer.stop()
        await http.aclose()
