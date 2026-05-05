"""Tests for the bearer-auth HTTP client + auto-refresh-on-401."""

from __future__ import annotations

import httpx
import pytest

from remotecodetrol_mcp.auth import AuthClient, TokenBundle, TokenStore
from remotecodetrol_mcp.client import APIClient, APIError


pytestmark = pytest.mark.asyncio


def _bundle(access: str, email: str = "user@example.com") -> TokenBundle:
    import time
    return TokenBundle(
        access_token=access, refresh_token="r", expires_at=time.time() + 900, email=email,
    )


async def test_get_passes_bearer(fake_keyring, config, jwt_factory):
    access = jwt_factory()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json={"threads": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        auth = AuthClient(config, http, TokenStore(config.keychain_service))
        auth._cached = _bundle(access)
        api = APIClient(config, auth, http)
        data = await api.get("/threads")

    assert data == {"threads": []}
    assert seen == [f"Bearer {access}"]


async def test_401_triggers_refresh_and_retry(fake_keyring, config, jwt_factory):
    """A 401 should drop the cache, force re-auth, and retry once."""
    first_access = jwt_factory(sub="u@example.com", ttl_seconds=900)
    second_access = jwt_factory(sub="u@example.com", ttl_seconds=900)
    store = TokenStore(config.keychain_service)
    store.set_active_email("u@example.com")
    store.set_refresh_token("u@example.com", "rt")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/threads"):
            auth = request.headers.get("authorization", "")
            calls.append(auth)
            if auth == f"Bearer {first_access}":
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json={"threads": []})
        if path.endswith("/oauth/token"):
            return httpx.Response(
                200,
                json={
                    "access_token": second_access,
                    "refresh_token": "rt2",
                    "token_type": "Bearer",
                },
            )
        raise AssertionError(f"unexpected: {path}")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        auth = AuthClient(config, http, store)
        auth._cached = _bundle(first_access, email="u@example.com")
        api = APIClient(config, auth, http)
        data = await api.get("/threads")

    assert data == {"threads": []}
    assert calls == [f"Bearer {first_access}", f"Bearer {second_access}"]


async def test_non_401_error_raises_apierror(fake_keyring, config, jwt_factory):
    access = jwt_factory()

    def handler(_):
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        auth = AuthClient(config, http, TokenStore(config.keychain_service))
        auth._cached = _bundle(access)
        api = APIClient(config, auth, http)
        with pytest.raises(APIError) as ei:
            await api.get("/threads")
    assert ei.value.status == 500
