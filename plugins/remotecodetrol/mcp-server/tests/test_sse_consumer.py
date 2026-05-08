"""SSE consumer dispatch logic.

Rather than spin up a full HTTP server (which complicates running tests
in any environment), we exercise the consumer's `_dispatch` method
directly with synthetic SseEvents and verify the cache + state-file
side effects. The wire-level parsing is covered by test_sse_parser.py;
this file covers the layer above (parsed event → cache mutation →
state-file write).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from remotecodetrol_mcp.auth import AuthClient, TokenBundle, TokenStore
from remotecodetrol_mcp.state_file import make_writer
from remotecodetrol_mcp.streaming import (
    SseConsumer,
    SseEvent,
    StreamingState,
    _AuthRevokedSentinel,
    _RetryHintSentinel,
)


pytestmark = pytest.mark.asyncio


def _bundle() -> TokenBundle:
    return TokenBundle(
        access_token="tok",
        refresh_token="r",
        expires_at=time.time() + 900,
        email="user@example.com",
    )


def _make_consumer(config, fake_keyring, state, *, state_file: Path | None = None):
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    http = httpx.AsyncClient(transport=transport)
    store = TokenStore(config.keychain_service)
    auth = AuthClient(config, http, store)
    auth._cached = _bundle()
    writer = make_writer(state_file) if state_file else None
    consumer = SseConsumer(config, auth, http, state, state_file_writer=writer)
    return consumer, http


async def test_snapshot_event_replaces_cache(fake_keyring, config):
    state = StreamingState()
    state.add_message({"id": "old", "thread_id": "t", "body": "stale"})
    consumer, http = _make_consumer(config, fake_keyring, state)
    try:
        evt = SseEvent(
            event="state.snapshot",
            id="s1",
            data=json.dumps({
                "pending": [
                    {"id": "n1", "thread_id": "t", "body": "fresh"},
                    {"id": "n2", "thread_id": "u", "body": "other"},
                ]
            }),
            retry_ms=None,
        )
        await consumer._dispatch(evt)
        assert "old" not in [m["id"] for m in state.pending.get("t", [])]
        assert {tid for tid in state.pending} == {"t", "u"}
    finally:
        await http.aclose()


async def test_message_created_appends(fake_keyring, config):
    state = StreamingState()
    consumer, http = _make_consumer(config, fake_keyring, state)
    try:
        evt = SseEvent(
            event="message.created",
            id="m1",
            data=json.dumps({"id": "m1", "thread_id": "t", "body": "hi"}),
            retry_ms=None,
        )
        await consumer._dispatch(evt)
        assert state.pending["t"][0]["id"] == "m1"
    finally:
        await http.aclose()


async def test_message_acked_removes(fake_keyring, config):
    state = StreamingState()
    state.add_message({"id": "m1", "thread_id": "t", "body": "x"})
    state.add_message({"id": "m2", "thread_id": "t", "body": "y"})
    consumer, http = _make_consumer(config, fake_keyring, state)
    try:
        evt = SseEvent(
            event="message.acked",
            id="a1",
            data=json.dumps({"thread_id": "t", "message_ids": ["m1"]}),
            retry_ms=None,
        )
        await consumer._dispatch(evt)
        assert [m["id"] for m in state.pending["t"]] == ["m2"]
    finally:
        await http.aclose()


async def test_state_file_written_on_dispatch(fake_keyring, config, tmp_path):
    state = StreamingState()
    state_file = tmp_path / "pending.json"
    consumer, http = _make_consumer(config, fake_keyring, state, state_file=state_file)
    try:
        evt = SseEvent(
            event="message.created",
            id="m1",
            data=json.dumps({
                "id": "m1",
                "thread_id": "t",
                "body": "hello",
                "created_at": "2026-05-05T12:00:00Z",
            }),
            retry_ms=None,
        )
        await consumer._dispatch(evt)
        payload = json.loads(state_file.read_text())
        assert payload["pending"][0]["id"] == "m1"
        assert payload["schema_version"] == 1
    finally:
        await http.aclose()


async def test_error_auth_revoked_raises_sentinel(fake_keyring, config):
    state = StreamingState()
    consumer, http = _make_consumer(config, fake_keyring, state)
    try:
        evt = SseEvent(
            event="error",
            id="e1",
            data=json.dumps({"code": "auth_revoked", "retry": 0}),
            retry_ms=None,
        )
        with pytest.raises(_AuthRevokedSentinel):
            await consumer._dispatch(evt)
    finally:
        await http.aclose()


async def test_error_other_raises_retry_hint(fake_keyring, config):
    state = StreamingState()
    consumer, http = _make_consumer(config, fake_keyring, state)
    try:
        evt = SseEvent(
            event="error",
            id="e1",
            data=json.dumps({"code": "internal", "retry": 5000}),
            retry_ms=None,
        )
        with pytest.raises(_RetryHintSentinel) as ei:
            await consumer._dispatch(evt)
        assert ei.value.retry_ms == 5000
    finally:
        await http.aclose()


async def test_undecodable_data_logged_and_ignored(fake_keyring, config, caplog):
    state = StreamingState()
    consumer, http = _make_consumer(config, fake_keyring, state)
    try:
        evt = SseEvent(
            event="message.created",
            id="m1",
            data="{not-json",
            retry_ms=None,
        )
        # Should NOT raise — defensive parse failure.
        await consumer._dispatch(evt)
        assert state.pending == {}
    finally:
        await http.aclose()


async def test_unknown_event_ignored(fake_keyring, config):
    state = StreamingState()
    consumer, http = _make_consumer(config, fake_keyring, state)
    try:
        evt = SseEvent(event="something.weird", id="x", data="{}", retry_ms=None)
        await consumer._dispatch(evt)
        assert state.pending == {}
    finally:
        await http.aclose()


async def test_connected_event_does_not_mutate(fake_keyring, config):
    state = StreamingState()
    consumer, http = _make_consumer(config, fake_keyring, state)
    try:
        evt = SseEvent(
            event="connected",
            id="c1",
            data=json.dumps({"server_time": "2026-05-05T12:00:00Z"}),
            retry_ms=None,
        )
        await consumer._dispatch(evt)
        assert state.pending == {}
    finally:
        await http.aclose()


async def test_run_waits_for_link_on_no_token(fake_keyring, config, monkeypatch):
    """No token in keychain → status `waiting_for_link`, consumer keeps
    running so the user's /remotecodetrol:link can write a token without
    requiring a Claude Code restart.

    Regression for the v0.3.2-and-earlier double-restart bug, where this
    case set status=auth_failed and exited the consumer permanently.
    """
    # Make the no-token retry sleep tiny so the test is fast.
    import remotecodetrol_mcp.streaming as streaming_mod
    monkeypatch.setattr(streaming_mod, "WAITING_FOR_LINK_RETRY_S", 0.05)

    state = StreamingState()
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    http = httpx.AsyncClient(transport=transport)
    store = TokenStore(config.keychain_service)
    auth = AuthClient(config, http, store)
    # Don't seed any credentials — get_access_token will raise
    # NotAuthorizedError, which the consumer should NOT treat as terminal.
    consumer = SseConsumer(config, auth, http, state)
    try:
        run_task = asyncio.create_task(consumer.run())
        # Give the consumer a couple of retry cycles to reach
        # waiting_for_link state.
        for _ in range(10):
            await asyncio.sleep(0.02)
            if state.sse_status == "waiting_for_link":
                break

        assert state.sse_status == "waiting_for_link", (
            f"expected waiting_for_link, got {state.sse_status} — "
            "regression of double-restart bug"
        )
        assert not run_task.done(), (
            "consumer must keep running while waiting for link "
            "(it used to exit permanently in v0.3.2 and earlier)"
        )

        # Now stop the consumer to clean up. It should exit cleanly.
        await consumer.stop()
        async with asyncio.timeout(2):
            await run_task
    finally:
        await http.aclose()
