"""Integration-style tests for the FastMCP tools.

We register tools onto a fresh FastMCP instance per test, point the API
client at an httpx MockTransport, and call tools through the FastMCP
`call_tool` interface to verify wire-compatible behavior.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import FastMCP

from remotecodetrol_mcp.auth import AuthClient, TokenBundle, TokenStore
from remotecodetrol_mcp.client import APIClient
from remotecodetrol_mcp.streaming import StreamingState
from remotecodetrol_mcp.tools import ThreadState, register_tools


pytestmark = pytest.mark.asyncio


def _bundle(access: str, email: str = "user@example.com") -> TokenBundle:
    return TokenBundle(
        access_token=access, refresh_token="r", expires_at=time.time() + 900, email=email
    )


async def _build_mcp(
    handler, fake_keyring, config, jwt_factory, tmp_path: Path,
    *, streaming: StreamingState | None = None,
):
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
    register_tools(mcp, api, state, config, streaming=streaming)
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
            # v0.3.9+: skip the new blocking-wait behavior. This test is
            # about the POST shape, not the wait. There's a dedicated
            # test for the wait path.
            wait=False,
            idempotency_key="k1",
        )
        # v0.3.0 snake_case response shape (spec Appendix A).
        assert out["message_id"] == "msg_123"
        assert out["thread"] == "alerts"
        # Old backend without bundling → empty pending list, count 0.
        assert out["pending_count"] == 0
        assert out["pending_messages"] == []
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

    streaming = StreamingState()
    streaming.sse_status = "connected"
    mcp, http, state = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming,
    )
    try:
        state.set("primary")
        out = await _call(mcp, "whoami")
        assert out["email"] == "user@example.com"
        assert out["active_thread"] == "primary"
        assert out["sse_status"] == "connected"
        assert out["plugin_version"]
        assert isinstance(out["pending_count_by_thread"], dict)
    finally:
        await http.aclose()


async def test_wait_for_response_returns_immediately_when_event_fires(
    fake_keyring, config, jwt_factory, tmp_path
):
    """v0.3.0: wait_for_response awaits state_change, doesn't poll."""
    import asyncio

    def h(_):
        return httpx.Response(404)  # never used

    streaming = StreamingState()
    streaming.sse_status = "connected"

    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming,
    )
    try:
        await _call(mcp, "set_thread", name="t")

        async def fire_after_short_delay():
            await asyncio.sleep(0.05)
            streaming.add_message({
                "id": "m9",
                "thread_id": "t",
                "body": "go",
                "sender_type": "user",
                "created_at": "2026-05-05T13:00:00Z",
            })

        producer = asyncio.create_task(fire_after_short_delay())
        out = await _call(
            mcp,
            "wait_for_response",
            timeout_minutes=1,
            poll_interval_seconds=1,  # v0.2.4 back-compat kwarg, ignored
        )
        await producer
        assert len(out["messages"]) == 1
        assert out["messages"][0]["id"] == "m9"
        assert out["source"] == "cache"
    finally:
        await http.aclose()


async def test_wait_for_response_timeout_returns_empty(
    fake_keyring, config, jwt_factory, tmp_path
):
    def h(_):
        return httpx.Response(200, json={"messages": [], "cursor": None})

    streaming = StreamingState()
    streaming.sse_status = "connected"
    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming,
    )
    try:
        await _call(mcp, "set_thread", name="t")
        # 0-minute timeout — returns immediately with empty list.
        out = await _call(
            mcp,
            "wait_for_response",
            timeout_minutes=0,
            poll_interval_seconds=1,  # ignored; v0.2.4 back-compat
        )
        assert out["messages"] == []
    finally:
        await http.aclose()


async def test_wait_for_response_falls_back_to_api_when_streaming_disabled(
    fake_keyring, config, jwt_factory, tmp_path
):
    seen: list[str] = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if "/messages" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {
                            "id": "m1",
                            "body": "hi",
                            "senderType": "user",
                            "createdAt": "2026-05-05T13:00:00Z",
                        }
                    ],
                    "cursor": None,
                },
            )
        return httpx.Response(200, json={})

    streaming = StreamingState()
    streaming.sse_status = "disabled"
    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming,
    )
    try:
        await _call(mcp, "set_thread", name="t")
        out = await _call(mcp, "wait_for_response", timeout_minutes=1)
        assert len(out["messages"]) == 1
        assert out["source"] == "api"
        assert any("/messages" in p for p in seen)
    finally:
        await http.aclose()


async def test_peek_uses_cache_when_fresh(
    fake_keyring, config, jwt_factory, tmp_path
):
    def h(request: httpx.Request) -> httpx.Response:
        # If we hit the API, the test fails — cache should be used.
        raise AssertionError(f"unexpected API call: {request.url.path}")

    streaming = StreamingState()
    streaming.sse_status = "connected"
    streaming.add_message({
        "id": "msg1",
        "thread_id": "t",
        "body": "cached",
        "sender_type": "user",
        "created_at": "2026-05-05T13:00:00Z",
    })

    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming,
    )
    try:
        await _call(mcp, "set_thread", name="t")
        peek = await _call(mcp, "peek_messages")
        assert len(peek["messages"]) == 1
        assert peek["messages"][0]["id"] == "msg1"
        assert peek["source"] == "cache"
    finally:
        await http.aclose()


async def test_peek_falls_back_to_api_when_cache_stale(
    fake_keyring, config, jwt_factory, tmp_path
):
    seen: list[str] = []

    def h(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "id": "from-api",
                        "body": "hi",
                        "senderType": "user",
                        "createdAt": "2026-05-05T13:00:00Z",
                    }
                ],
                "cursor": None,
            },
        )

    streaming = StreamingState()
    # Disconnected and last_event_at far in the past → cache is stale.
    streaming.sse_status = "disconnected"
    streaming.last_event_at = time.monotonic() - 999.0

    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming,
    )
    try:
        await _call(mcp, "set_thread", name="t")
        peek = await _call(mcp, "peek_messages")
        assert peek["messages"][0]["id"] == "from-api"
        assert peek["source"] == "api"
        assert any("/messages" in p for p in seen)
    finally:
        await http.aclose()


async def test_peek_all_threads_with_star(
    fake_keyring, config, jwt_factory, tmp_path
):
    def h(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"all-threads peek must be cache-only: {request.url.path}")

    streaming = StreamingState()
    streaming.sse_status = "connected"
    streaming.add_message({"id": "a1", "thread_id": "alpha", "body": "1"})
    streaming.add_message({"id": "b1", "thread_id": "beta", "body": "2"})

    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming,
    )
    try:
        peek = await _call(mcp, "peek_messages", thread="*")
        ids = sorted(m["id"] for m in peek["messages"])
        assert ids == ["a1", "b1"]
    finally:
        await http.aclose()


async def test_ack_prunes_cache_on_2xx(
    fake_keyring, config, jwt_factory, tmp_path
):
    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/ack" in request.url.path:
            return httpx.Response(200, json={})
        return httpx.Response(404)

    streaming = StreamingState()
    streaming.sse_status = "connected"
    streaming.add_message({"id": "msg1", "thread_id": "t", "body": "x"})
    streaming.add_message({"id": "msg2", "thread_id": "t", "body": "y"})

    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming,
    )
    try:
        await _call(mcp, "set_thread", name="t")
        ack = await _call(mcp, "ack_messages", message_ids=["msg1"])
        assert ack["acked"] == 1
        # Cache pruned: only msg2 remains.
        remaining = streaming.pending.get("t", [])
        assert [m["id"] for m in remaining] == ["msg2"]
    finally:
        await http.aclose()


async def test_ack_does_not_prune_on_failure(
    fake_keyring, config, jwt_factory, tmp_path
):
    from remotecodetrol_mcp.client import APIError

    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/ack" in request.url.path:
            return httpx.Response(500, text="boom")
        return httpx.Response(404)

    streaming = StreamingState()
    streaming.sse_status = "connected"
    streaming.add_message({"id": "msg1", "thread_id": "t", "body": "x"})

    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming,
    )
    try:
        await _call(mcp, "set_thread", name="t")
        with pytest.raises(Exception):
            await _call(mcp, "ack_messages", message_ids=["msg1"])
        # Cache is untouched on failure (§5).
        assert [m["id"] for m in streaming.pending.get("t", [])] == ["msg1"]
    finally:
        await http.aclose()


async def test_send_message_returns_bundled_pending(
    fake_keyring, config, jwt_factory, tmp_path
):
    def h(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "messageId": "out1",
                "pending_messages": [
                    {
                        "id": "in1",
                        "thread_id": "alerts",
                        "body": "reply!",
                        "sender_type": "user",
                        "created_at": "2026-05-05T12:00:00Z",
                    }
                ],
                "pending_count": 1,
            },
        )

    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path,
    )
    try:
        await _call(mcp, "set_thread", name="alerts")
        out = await _call(mcp, "send_message", body="hi")
        assert out["message_id"] == "out1"
        assert out["pending_count"] == 1
        assert out["pending_messages"][0]["id"] == "in1"
    finally:
        await http.aclose()


async def test_ack_messages_persists_state_file_after_proactive_prune(
    fake_keyring, config, jwt_factory, tmp_path
):
    """Regression for v0.3.1 → v0.3.2 hook bug.

    When tools.ack_messages succeeds against the backend, it proactively
    prunes the local cache (so subsequent peek doesn't see ghosts). It
    MUST also persist the state file — otherwise the UserPromptSubmit
    hook re-injects already-acked messages on the next prompt.

    The bug: only the SSE message.acked handler called persist, and that
    handler no-ops because tools.py beat it to the prune (remove_messages
    returns 0). Net: state file lagged the cache forever.
    """
    write_calls: list[dict] = []

    async def writer(state: StreamingState) -> None:
        # Capture the snapshot of `pending` at write time.
        write_calls.append({tid: list(msgs) for tid, msgs in state.pending.items()})

    streaming = StreamingState()
    streaming.writer = writer
    # Pre-populate cache as if the SSE consumer had received this message.
    streaming.pending["t"] = [{"id": "m1", "thread_id": "t", "body": "hi"}]

    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/ack" in request.url.path:
            return httpx.Response(200, json={})
        return httpx.Response(404)

    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming
    )
    try:
        await _call(mcp, "set_thread", name="t")
        ack = await _call(mcp, "ack_messages", message_ids=["m1"])
        assert ack["acked"] == 1
        # The cache mutation must have triggered a state-file write.
        assert len(write_calls) == 1, (
            "ack_messages must persist after proactive prune"
        )
        # The persisted snapshot must reflect the pruned state (no m1).
        assert "t" not in write_calls[0], (
            f"acked message should NOT appear in persisted state: {write_calls[0]}"
        )
    finally:
        await http.aclose()


async def test_send_message_blocks_for_reply_when_wait_true(
    fake_keyring, config, jwt_factory, tmp_path
):
    """v0.3.9 default: send_message(require_response=True) blocks until a
    reply lands in the cache, then returns it bundled. This is the
    behavioral fix for the "user steps away, hook never fires" failure
    mode — we make Claude wait inside the tool call rather than ending
    the turn."""

    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/messages" in request.url.path:
            # Send succeeds, no pending bundled (simulating "no reply yet").
            return httpx.Response(
                201, json={"messageId": "out1", "pending_messages": [], "pending_count": 0}
            )
        return httpx.Response(404)

    streaming = StreamingState()
    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming
    )
    try:
        await _call(mcp, "set_thread", name="t")

        # Start the send as a task so we can simulate a phone reply
        # mid-call. With wait=True (default) and require_response=True,
        # the tool will block in `_await_reply_for_thread` since there's
        # no pending in cache yet.
        send_task = asyncio.create_task(
            _call(
                mcp,
                "send_message",
                body="need decision",
                require_response=True,
                timeout_minutes=0.05,  # 3s — generous for a fake event
            )
        )

        # Give the send a moment to POST and enter the wait.
        await asyncio.sleep(0.1)

        # Simulate the SSE consumer adding a reply to the cache.
        streaming.pending["t"] = [
            {"id": "reply_1", "thread_id": "t", "body": "do it", "sender_type": "user"}
        ]
        streaming.bump()

        out = await asyncio.wait_for(send_task, timeout=2)
        assert out["message_id"] == "out1"
        assert out["pending_count"] == 1
        assert out["pending_messages"][0]["id"] == "reply_1"
        assert out["pending_messages"][0]["body"] == "do it"
    finally:
        await http.aclose()


async def test_send_message_skips_wait_when_explicitly_disabled(
    fake_keyring, config, jwt_factory, tmp_path
):
    """wait=False short-circuits the new blocking behavior. Caller can
    end the turn and rely on the hook (or call wait_for_response
    later)."""

    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/messages" in request.url.path:
            return httpx.Response(
                201, json={"messageId": "out2", "pending_messages": [], "pending_count": 0}
            )
        # If wait_for_response is called, this would 404 — and the test
        # would hang or error. Asserting NO GET is fired is the point.
        return httpx.Response(404)

    streaming = StreamingState()
    mcp, http, _ = await _build_mcp(
        h, fake_keyring, config, jwt_factory, tmp_path, streaming=streaming
    )
    try:
        await _call(mcp, "set_thread", name="t")
        out = await _call(
            mcp,
            "send_message",
            body="just an FYI",
            require_response=True,
            wait=False,  # opt out of blocking
        )
        assert out["message_id"] == "out2"
        assert out["pending_count"] == 0  # didn't wait → no reply yet
    finally:
        await http.aclose()
