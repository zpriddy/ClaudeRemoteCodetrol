"""Tests for the OAuth client and Keychain storage.

Covers the split device-code flow:
  - start_device_flow      → returns codes immediately, persists pending state
  - complete_device_flow_once → one-shot poll (None if pending, bundle on success)
  - get_access_token       → cache → refresh → complete-pending → NotAuthorizedError
  - logout                 → wipes everything
"""

from __future__ import annotations

import time

import httpx
import pytest

from remotecodetrol_mcp.auth import (
    AuthClient,
    AuthError,
    NotAuthorizedError,
    PENDING_FLOW_KEY,
    TokenStore,
    _email_from_access_token,
)
from remotecodetrol_mcp.state import read_state


pytestmark = pytest.mark.asyncio


async def test_token_store_roundtrip(fake_keyring, config):
    store = TokenStore(config.keychain_service)
    assert store.get_active_email() is None
    store.set_active_email("user@example.com")
    store.set_refresh_token("user@example.com", "rt-abc")
    assert store.get_active_email() == "user@example.com"
    assert store.get_refresh_token("user@example.com") == "rt-abc"
    store.clear("user@example.com")
    assert store.get_refresh_token("user@example.com") is None


async def test_refresh_happy_path(fake_keyring, config, jwt_factory):
    new_access = jwt_factory(sub="user@example.com", ttl_seconds=900)
    store = TokenStore(config.keychain_service)
    store.set_active_email("user@example.com")
    store.set_refresh_token("user@example.com", "old-refresh")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth/token")
        body = request.content.decode()
        assert "refresh_token=old-refresh" in body
        return httpx.Response(
            200,
            json={
                "access_token": new_access,
                "refresh_token": "new-refresh",
                "token_type": "Bearer",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, store)
        token = await client.get_access_token()

    assert token == new_access
    # Rotation: the new refresh token is stored.
    assert store.get_refresh_token("user@example.com") == "new-refresh"


async def test_start_device_flow_persists_and_returns_codes(
    fake_keyring, config, isolated_state
):
    """start_device_flow() returns user_code immediately and writes the
    device_code to state.json so a later tool call can complete it."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/oauth/device/code")
        return httpx.Response(
            200,
            json={
                "device_code": "DC-abc",
                "user_code": "WDJB-MJHT",
                "verification_uri": "https://remotecodetrol.web.app/authorize",
                "verification_uri_complete": "https://remotecodetrol.web.app/authorize?user_code=WDJB-MJHT",
                "interval": 5,
                "expires_in": 600,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http)
        info = await client.start_device_flow()

    assert info.user_code == "WDJB-MJHT"
    assert "WDJB-MJHT" in info.verification_uri  # complete URL preferred
    assert info.expires_in_seconds == 600

    pending = read_state().get(PENDING_FLOW_KEY)
    assert pending is not None
    assert pending["device_code"] == "DC-abc"
    assert pending["user_code"] == "WDJB-MJHT"
    assert pending["expires_at"] > time.time()


async def test_complete_device_flow_once_returns_none_when_pending(
    fake_keyring, config, isolated_state
):
    """A pending poll returns None — caller should retry later, not error."""
    from remotecodetrol_mcp.state import update_state

    update_state({
        PENDING_FLOW_KEY: {
            "device_code": "DC-abc",
            "user_code": "WDJB-MJHT",
            "verification_uri": "x",
            "interval": 5,
            "expires_at": time.time() + 600,
        }
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "authorization_pending"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http)
        result = await client.complete_device_flow_once()

    assert result is None
    # Pending state should still be on disk so the next call can retry.
    assert read_state().get(PENDING_FLOW_KEY) is not None


async def test_complete_device_flow_once_success_stores_tokens(
    fake_keyring, config, jwt_factory, isolated_state
):
    """When the user has authorized, the poll returns tokens which are
    stored in keychain and the pending state is cleared."""
    from remotecodetrol_mcp.state import update_state

    update_state({
        PENDING_FLOW_KEY: {
            "device_code": "DC-abc",
            "user_code": "WDJB-MJHT",
            "verification_uri": "x",
            "interval": 5,
            "expires_at": time.time() + 600,
        }
    })
    access = jwt_factory(sub="user@example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "refresh_token": "rt-final",
                "token_type": "Bearer",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http)
        bundle = await client.complete_device_flow_once()

    assert bundle is not None
    assert bundle.email == "user@example.com"
    assert client.store.get_active_email() == "user@example.com"
    assert client.store.get_refresh_token("user@example.com") == "rt-final"
    assert read_state().get(PENDING_FLOW_KEY) is None  # cleared on success


async def test_complete_device_flow_once_terminal_error_clears_pending(
    fake_keyring, config, isolated_state
):
    """access_denied / expired_token are terminal — pending is wiped so
    the next link() starts cleanly."""
    from remotecodetrol_mcp.state import update_state

    update_state({
        PENDING_FLOW_KEY: {
            "device_code": "DC-abc",
            "user_code": "WDJB-MJHT",
            "verification_uri": "x",
            "interval": 5,
            "expires_at": time.time() + 600,
        }
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "access_denied"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http)
        with pytest.raises(AuthError, match="access_denied"):
            await client.complete_device_flow_once()

    assert read_state().get(PENDING_FLOW_KEY) is None


async def test_get_access_token_with_no_credentials_raises_not_authorized(
    fake_keyring, config, isolated_state
):
    """First-time call (no refresh, no pending flow) MUST NOT block —
    it must raise NotAuthorizedError so the caller can invoke link()."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"should not call network without credentials; got {request.url}"
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http)
        with pytest.raises(NotAuthorizedError, match="link"):
            await client.get_access_token()


async def test_get_access_token_with_pending_flow_completes_it(
    fake_keyring, config, jwt_factory, isolated_state
):
    """If a pending device flow exists AND the user has authorized,
    get_access_token transparently completes it on the next call."""
    from remotecodetrol_mcp.state import update_state

    update_state({
        PENDING_FLOW_KEY: {
            "device_code": "DC-abc",
            "user_code": "WDJB-MJHT",
            "verification_uri": "x",
            "interval": 5,
            "expires_at": time.time() + 600,
        }
    })
    access = jwt_factory(sub="user@example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "refresh_token": "rt-final",
                "token_type": "Bearer",
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http)
        token = await client.get_access_token()

    assert token == access


async def test_get_access_token_with_pending_flow_still_pending_raises(
    fake_keyring, config, isolated_state
):
    """If the user hasn't authorized yet, get_access_token raises
    NotAuthorizedError — including the user_code so the caller can show
    a useful message."""
    from remotecodetrol_mcp.state import update_state

    update_state({
        PENDING_FLOW_KEY: {
            "device_code": "DC-abc",
            "user_code": "WDJB-MJHT",
            "verification_uri": "x",
            "interval": 5,
            "expires_at": time.time() + 600,
        }
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "authorization_pending"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http)
        with pytest.raises(NotAuthorizedError) as exc:
            await client.get_access_token()
        assert exc.value.pending_user_code == "WDJB-MJHT"


async def test_logout_clears_everything(
    fake_keyring, config, jwt_factory, isolated_state
):
    """logout() clears cache, keychain, AND any pending device flow."""
    from remotecodetrol_mcp.state import update_state

    store = TokenStore(config.keychain_service)
    store.set_active_email("user@example.com")
    store.set_refresh_token("user@example.com", "rt-x")
    update_state({
        PENDING_FLOW_KEY: {
            "device_code": "DC", "user_code": "X-Y", "verification_uri": "u",
            "interval": 5, "expires_at": time.time() + 60,
        }
    })

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("logout should not hit the network")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, store)
        client.logout()

    assert store.get_active_email() is None
    assert store.get_refresh_token("user@example.com") is None
    assert read_state().get(PENDING_FLOW_KEY) is None


async def test_email_extraction(jwt_factory):
    token = jwt_factory(sub="someone@example.com")
    assert _email_from_access_token(token) == "someone@example.com"


async def test_cached_token_skips_network(fake_keyring, config, jwt_factory):
    """A still-valid cached access token should not trigger any HTTP calls."""
    fresh = jwt_factory(sub="user@example.com", ttl_seconds=900)
    store = TokenStore(config.keychain_service)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"should not call network; got {request.url}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AuthClient(config, http, store)
        # Pre-populate cache via private attribute.
        from remotecodetrol_mcp.auth import TokenBundle, _expiry_from_access_token

        client._cached = TokenBundle(
            access_token=fresh,
            refresh_token="x",
            expires_at=_expiry_from_access_token(fresh),
            email="user@example.com",
        )
        token = await client.get_access_token()
    assert token == fresh
