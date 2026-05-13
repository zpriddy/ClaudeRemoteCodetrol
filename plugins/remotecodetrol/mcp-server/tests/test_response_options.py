"""v0.5.0 selectable response buttons — local validation, Pydantic round-trip,
and wire payload shape from send_message."""

from __future__ import annotations

import json

import httpx
import pytest

from remotecodetrol_mcp.auth import AuthClient, TokenStore
from remotecodetrol_mcp.client import APIClient
from remotecodetrol_mcp.streaming import StreamingState, _normalize_message
from remotecodetrol_mcp.tools import (
    MAX_RESPONSE_OPTIONS,
    Message,
    ResponseOption,
    ThreadState,
    _validate_response_options,
    register_tools,
)


class FakeMCP:
    def tool(self, fn):
        return fn


async def _build_dispatchers(handler, config, tmp_path):
    """Wire APIClient + ThreadState + AuthClient with a pre-authorized v4
    token so api.post doesn't trip NotAuthorizedError. Mirror of the helper
    in test_known_threads.py — duplicated here because pytest doesn't add
    the tests dir to sys.path on collection, so cross-test imports fail.
    """
    import time as _time

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
    auth = AuthClient(config, http, store)
    api = APIClient(config, auth, http)
    state = ThreadState(config)
    mcp = FakeMCP()
    streaming = StreamingState()
    dispatchers = register_tools(mcp, api, state, config, streaming=streaming)
    return dispatchers, http, streaming, state


# ---------- _validate_response_options ----------


def test_validate_accepts_valid_pair():
    opts = [ResponseOption(id="a", label="Option A"), ResponseOption(id="b", label="Option B")]
    _validate_response_options(opts, "single")


def test_validate_accepts_none_none():
    _validate_response_options(None, None)


def test_validate_rejects_mode_without_options():
    with pytest.raises(ValueError, match="requires response_options"):
        _validate_response_options(None, "single")


def test_validate_rejects_options_without_mode():
    with pytest.raises(ValueError, match="required when response_options"):
        _validate_response_options(
            [ResponseOption(id="a", label="A")], None
        )


def test_validate_rejects_too_many_options():
    too_many = [
        ResponseOption(id=f"o{i}", label=f"Option {i}")
        for i in range(MAX_RESPONSE_OPTIONS + 1)
    ]
    with pytest.raises(ValueError, match="at most"):
        _validate_response_options(too_many, "single")


def test_validate_rejects_duplicate_ids():
    dups = [ResponseOption(id="a", label="One"), ResponseOption(id="a", label="Two")]
    with pytest.raises(ValueError, match="unique"):
        _validate_response_options(dups, "single")


def test_validate_rejects_invalid_id_chars():
    bad = [ResponseOption(id="has space", label="Bad")]
    with pytest.raises(ValueError, match="invalid characters"):
        _validate_response_options(bad, "single")


def test_validate_rejects_newline_in_label():
    bad = [ResponseOption(id="a", label="Line one\nLine two")]
    with pytest.raises(ValueError, match="newlines"):
        _validate_response_options(bad, "single")


# ---------- Message Pydantic round-trip ----------


def test_message_round_trips_response_options_snake_case():
    """SSE-style snake_case payload: fields should survive the v0.4.5 trap
    (the one where Pydantic silently dropped Spec-2 wire keys)."""
    payload = {
        "id": "msg-1",
        "body": "A or B?",
        "response_options": [
            {"id": "a", "label": "Option A", "color": "accent"},
            {"id": "b", "label": "Option B"},
        ],
        "selection_mode": "single",
    }
    msg = Message.model_validate(payload)
    assert msg.response_options is not None
    assert [o.id for o in msg.response_options] == ["a", "b"]
    assert msg.response_options[0].color == "accent"
    assert msg.selection_mode == "single"


def test_message_round_trips_selected_option_ids():
    payload = {
        "id": "reply-1",
        "body": "Option A",
        "replied_to": "msg-1",
        "selected_option_ids": ["a"],
    }
    msg = Message.model_validate(payload)
    assert msg.selected_option_ids == ["a"]
    assert msg.replied_to == "msg-1"


def test_streaming_normalize_maps_camelcase_to_snake_case():
    raw = {
        "id": "msg-1",
        "body": "A or B?",
        "responseOptions": [{"id": "a", "label": "Option A"}],
        "selectionMode": "single",
        "selectedOptionIds": ["a"],
    }
    out = _normalize_message(raw)
    assert out["response_options"] == [{"id": "a", "label": "Option A"}]
    assert out["selection_mode"] == "single"
    assert out["selected_option_ids"] == ["a"]
    # Original keys must remain so we don't break any older consumer that
    # still reads camelCase directly.
    assert out["responseOptions"] == out["response_options"]


# ---------- send_message dispatcher emits correct wire payload ----------


async def test_send_message_emits_response_options_in_camelcase(config, tmp_path):
    """The MCP tool accepts snake_case args; the backend POST must carry
    camelCase (`responseOptions`, `selectionMode`) since that's the
    Firestore-canonical shape the routes write to."""
    captured: dict = {}

    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/messages" in request.url.path:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"messageId": "out1", "pending_messages": [], "pending_count": 0},
            )
        return httpx.Response(404)

    dispatchers, http, _, _ = await _build_dispatchers(h, config, tmp_path)
    try:
        await dispatchers["send_message"]({
            "body": "A or B?",
            "thread": "t",
            "response_options": [
                {"id": "a", "label": "Option A", "color": "accent"},
                {"id": "b", "label": "Option B"},
            ],
            "selection_mode": "single",
        })
        wire = captured["body"]
        assert wire["responseOptions"] == [
            {"id": "a", "label": "Option A", "color": "accent"},
            {"id": "b", "label": "Option B"},
        ]
        assert wire["selectionMode"] == "single"
    finally:
        await http.aclose()


async def test_send_message_rejects_invalid_options_before_network(config, tmp_path):
    """Local validation fires before the HTTP call so the user gets a clear
    error message rather than a generic 400 from the backend."""

    def h(_: httpx.Request) -> httpx.Response:
        raise AssertionError("send_message should not hit the backend on bad input")

    dispatchers, http, _, _ = await _build_dispatchers(h, config, tmp_path)
    try:
        with pytest.raises(ValueError, match="required when response_options"):
            await dispatchers["send_message"]({
                "body": "missing mode",
                "thread": "t",
                "response_options": [{"id": "a", "label": "A"}],
            })
    finally:
        await http.aclose()


async def test_send_message_omits_fields_when_unset(config, tmp_path):
    """Plain sends — without response_options — must NOT carry empty arrays
    on the wire, so the backend stores plain messages without phantom fields."""
    captured: dict = {}

    def h(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/messages" in request.url.path:
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={"messageId": "out1", "pending_messages": [], "pending_count": 0},
            )
        return httpx.Response(404)

    dispatchers, http, _, _ = await _build_dispatchers(h, config, tmp_path)
    try:
        await dispatchers["send_message"]({"body": "plain", "thread": "t"})
        wire = captured["body"]
        assert "responseOptions" not in wire
        assert "selectionMode" not in wire
    finally:
        await http.aclose()
