"""Shared test fixtures.

We mock `keyring` globally so tests never touch the real macOS Keychain.
"""

from __future__ import annotations

import secrets
import sys
import time
import warnings
from typing import Any

import jwt
import pytest

warnings.filterwarnings("ignore", category=Warning, module="jwt")

from remotecodetrol_mcp.config import Config


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.store.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self.store[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            # Mirror keyring's API: raise on missing.
            import keyring.errors
            raise keyring.errors.PasswordDeleteError("missing")
        del self.store[(service, account)]


@pytest.fixture
def fake_keyring(monkeypatch: pytest.MonkeyPatch) -> FakeKeyring:
    fake = FakeKeyring()
    import keyring
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    return fake


@pytest.fixture
def config() -> Config:
    return Config(
        api_base="https://api.test.invalid",
        stream_url="https://stream.test.invalid",
        default_thread=None,
        device_label="test-device",
        keychain_service="com.remotecodetrol.test",
        default_poll_interval_seconds=1,
        default_timeout_minutes=1,
    )


def make_jwt(sub: str = "user@example.com", ttl_seconds: int = 900) -> str:
    """Sign an unverified JWT we can decode client-side.

    `jti` makes each token unique even when called back-to-back with the
    same `sub`/`exp` (HMAC signing is deterministic across identical claims).
    """
    return jwt.encode(
        {
            "sub": sub,
            "exp": int(time.time()) + ttl_seconds,
            "jti": secrets.token_hex(8),
        },
        "test-secret-please-ignore-rfc-warning",
        algorithm="HS256",
    )


@pytest.fixture
def jwt_factory() -> Any:
    return make_jwt


@pytest.fixture
def isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Point the shared state.py at a tmp file so tests don't read or
    pollute the user's real ~/.config/remotecodetrol/state.json."""
    from remotecodetrol_mcp import state as state_mod
    fake_path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", fake_path)
    return fake_path
