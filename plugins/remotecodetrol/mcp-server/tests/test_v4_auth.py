"""Tests for the v0.4.0 single-token auth model.

Covers:
  - TokenStore: file perms, schema-version-4 enforcement, pre-v4 discard,
    round-trip persistence, clear_all preserving schema.
  - AuthClient: NotAuthorizedError when empty, returns valid stored token,
    synchronous rotation when expired, non-blocking past-day-7 rotation,
    complete_link_force persistence, start_device_flow deep_link shape.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import pytest

from remotecodetrol_mcp.auth import (
    AuthClient,
    DeviceFlowInfo,
    NotAuthorizedError,
    SCHEMA_VERSION,
    StoredToken,
    TokenStore,
)


# ---------- TokenStore ----------


def test_token_store_fresh_write_creates_dirs_and_perms(tmp_path):
    """Fresh write: parent dir 0700, file 0600, schema_version 4."""
    nested = tmp_path / "sub" / "deeper"
    target = nested / "tokens.json"
    store = TokenStore(path=target)
    store.store_token(
        email="user@example.com",
        token="opaque-abc",
        expires_at=time.time() + 14 * 24 * 60 * 60,
        rotates_at=time.time() + 7 * 24 * 60 * 60,
    )

    assert target.exists()
    file_perms = target.stat().st_mode & 0o777
    assert file_perms == 0o600, f"expected 0600, got {oct(file_perms)}"
    parent_perms = target.parent.stat().st_mode & 0o777
    assert parent_perms == 0o700, f"expected 0700, got {oct(parent_perms)}"

    raw = json.loads(target.read_text())
    assert raw["schema_version"] == SCHEMA_VERSION
    assert "user@example.com" in raw["tokens"]


def test_token_store_discards_pre_v4_no_schema_version(tmp_path):
    """Pre-v4 file with no schema_version key → atomically rewritten as v4 empty."""
    target = tmp_path / "tokens.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # Pre-v4 shape: bare email→jwt mapping under "tokens".
    target.write_text(
        json.dumps({"tokens": {"u@example.com": "an-old-jwt-string"}})
    )

    store = TokenStore(path=target)
    # The first read triggers the discard rewrite.
    assert store.get_token("u@example.com") is None

    rewritten = json.loads(target.read_text())
    assert rewritten == {"schema_version": SCHEMA_VERSION, "tokens": {}}


def test_token_store_discards_pre_v4_with_wrong_schema_version(tmp_path):
    """File with schema_version != 4 is also discarded."""
    target = tmp_path / "tokens.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"schema_version": 3, "tokens": {"u": "x"}}))

    store = TokenStore(path=target)
    assert store.get_token("u") is None
    rewritten = json.loads(target.read_text())
    assert rewritten == {"schema_version": SCHEMA_VERSION, "tokens": {}}


def test_token_store_round_trip(tmp_path):
    """store_token then get_token returns the same StoredToken."""
    target = tmp_path / "tokens.json"
    store = TokenStore(path=target)
    issued = time.time()
    store.store_token(
        email="user@example.com",
        token="opaque-tok",
        expires_at=issued + 14 * 24 * 60 * 60,
        rotates_at=issued + 7 * 24 * 60 * 60,
        issued_at=issued,
    )
    got = store.get_token("user@example.com")
    assert got is not None
    assert isinstance(got, StoredToken)
    assert got.token == "opaque-tok"
    assert got.issued_at == pytest.approx(issued, abs=1.0)
    assert got.expires_at == pytest.approx(issued + 14 * 24 * 60 * 60, abs=1.0)
    assert got.rotates_at == pytest.approx(issued + 7 * 24 * 60 * 60, abs=1.0)
    assert got.last_rotation_attempt_at is None

    # Second instance reads from disk (no shared memory).
    store2 = TokenStore(path=target)
    got2 = store2.get_token("user@example.com")
    assert got2 is not None
    assert got2.token == "opaque-tok"


def test_token_store_clear_all_preserves_schema(tmp_path):
    """clear_all empties tokens but preserves schema_version."""
    target = tmp_path / "tokens.json"
    store = TokenStore(path=target)
    store.store_token("u@example.com", "tok", time.time() + 100, time.time() + 50)
    store.set_active_email("u@example.com")
    assert store.get_token("u@example.com") is not None

    store.clear_all()
    raw = json.loads(target.read_text())
    assert raw == {"schema_version": SCHEMA_VERSION, "tokens": {}}


# ---------- AuthClient ----------


@pytest.fixture
def auth_store(tmp_path):
    """A v4 TokenStore at a per-test path."""
    return TokenStore(path=tmp_path / "tokens.json")


async def test_get_access_token_no_token_raises_not_authorized(config, auth_store):
    """Empty store → NotAuthorizedError, no network calls."""

    def handler(request):
        raise AssertionError(f"unexpected HTTP call: {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, auth_store)
        with pytest.raises(NotAuthorizedError, match="link"):
            await client.get_access_token()


async def test_get_access_token_returns_stored_when_valid(config, auth_store):
    """Valid token (well within TTL) is returned without rotation."""
    now = time.time()
    auth_store.set_active_email("user@example.com")
    auth_store.store_token(
        email="user@example.com",
        token="valid-tok",
        expires_at=now + 14 * 24 * 60 * 60,
        rotates_at=now + 7 * 24 * 60 * 60,
        issued_at=now,
    )

    def handler(request):
        raise AssertionError(f"unexpected HTTP call: {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, auth_store)
        tok = await client.get_access_token()
    assert tok == "valid-tok"


async def test_get_access_token_synchronous_rotation_when_expired(
    config, auth_store
):
    """When expires_at is past, rotation happens synchronously and the new
    token is persisted + returned."""
    now = time.time()
    auth_store.set_active_email("user@example.com")
    # expires_at is 5 seconds in the past — well past leeway.
    auth_store.store_token(
        email="user@example.com",
        token="expired-tok",
        expires_at=now - 5,
        rotates_at=now - 7 * 24 * 60 * 60,
        issued_at=now - 14 * 24 * 60 * 60,
    )

    rotate_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/rotate"):
            auth_header = request.headers.get("Authorization", "")
            rotate_calls.append(auth_header)
            return httpx.Response(
                200,
                json={
                    "token": "fresh-tok",
                    "expires_in": 14 * 24 * 60 * 60,
                    "rotates_after": 7 * 24 * 60 * 60,
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, auth_store)
        tok = await client.get_access_token()

    assert tok == "fresh-tok"
    assert rotate_calls == ["Bearer expired-tok"]
    # Persisted to disk:
    persisted = auth_store.get_token("user@example.com")
    assert persisted is not None
    assert persisted.token == "fresh-tok"


async def test_get_access_token_does_not_block_on_background_rotation(
    config, auth_store
):
    """When past day-7 rotation window, get_access_token returns the
    still-valid current token immediately even if /rotate would fail."""
    now = time.time()
    auth_store.set_active_email("user@example.com")
    # rotates_at: 1 hour ago (past day-7 window), expires_at: 7 days from now.
    auth_store.store_token(
        email="user@example.com",
        token="aging-tok",
        expires_at=now + 7 * 24 * 60 * 60,
        rotates_at=now - 60 * 60,
        issued_at=now - 8 * 24 * 60 * 60,
    )

    # /rotate handler that always errors — we must NOT see this fail the call.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/rotate"):
            return httpx.Response(500, text="server boom")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, auth_store)
        tok = await client.get_access_token()

        # Returned the current token immediately — no blocking.
        assert tok == "aging-tok"

        # Drain any spawned background tasks so they don't leak into other tests.
        # The background task is fire-and-forget; we wait briefly for it to
        # complete / fail silently.
        pending = [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
        ]
        for t in pending:
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass


async def test_complete_link_force_authorized_persists_token(config, auth_store):
    """authorized response → token persisted to disk."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth/check-link")
        body = json.loads(request.content)
        assert body["device_code"] == "DC-abc"
        return httpx.Response(
            200,
            json={
                "status": "authorized",
                "token": "freshly-issued-tok",
                "expires_in": 14 * 24 * 60 * 60,
                "rotates_after": 7 * 24 * 60 * 60,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, auth_store)
        result = await client.complete_link_force("DC-abc")

    assert result["status"] == "authorized"
    persisted = auth_store.get_token(auth_store.get_active_email() or "default")
    assert persisted is not None
    assert persisted.token == "freshly-issued-tok"


async def test_complete_link_force_pending_does_not_persist(config, auth_store):
    """Pending response leaves the store untouched."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "pending"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, auth_store)
        result = await client.complete_link_force("DC-abc")

    assert result["status"] == "pending"
    assert auth_store.get_active_email() is None


async def test_start_device_flow_returns_deep_link_with_user_code(
    config, auth_store
):
    """deep_link is `remotecodetrol://authorize?code=<user_code>`."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth/device/code")
        return httpx.Response(
            200,
            json={
                "device_code": "DC-xyz",
                "user_code": "ABCD-1234",
                "verification_uri": "https://remotecodetrol.web.app/authorize",
                "interval": 5,
                "expires_in": 600,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, auth_store)
        info = await client.start_device_flow()

    assert isinstance(info, DeviceFlowInfo)
    assert info.user_code == "ABCD-1234"
    assert info.deep_link == "remotecodetrol://authorize?code=ABCD-1234"
    assert info.device_code == "DC-xyz"
    assert info.expires_in_seconds == 600
    assert info.interval_seconds == 5
