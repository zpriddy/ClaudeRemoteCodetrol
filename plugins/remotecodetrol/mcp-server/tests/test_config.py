"""Tests for config.load_config() — particularly stream_url derivation.

The stream_url is derived from api_base when no explicit override is set.
The derivation rule (`strip /api suffix, append /stream`) is fragile in the
sense that a bug here causes the SSE consumer to silently reconnect-loop
forever — the same failure mode we shipped in 0.3.0 and had to hotfix.
These tests pin the behavior so it doesn't regress.
"""

from __future__ import annotations

import pytest

from remotecodetrol_mcp.config import (
    DEFAULT_API_BASE,
    _derive_stream_url,
    load_config,
)


def test_derive_strips_api_suffix_then_appends_stream() -> None:
    """Default Cloud Functions deployment: api function at /api → stream at /stream."""
    derived = _derive_stream_url(
        "https://us-central1-remotecodetrol.cloudfunctions.net/api"
    )
    assert derived == "https://us-central1-remotecodetrol.cloudfunctions.net/stream"


def test_derive_strips_trailing_slash_before_check() -> None:
    """`api_base` may have a trailing slash; treat it the same."""
    derived = _derive_stream_url(
        "https://us-central1-remotecodetrol.cloudfunctions.net/api/"
    )
    assert derived == "https://us-central1-remotecodetrol.cloudfunctions.net/stream"


def test_derive_no_api_suffix_just_appends_stream() -> None:
    """Local-dev / custom deployment: api_base ends without /api → append /stream."""
    derived = _derive_stream_url("http://localhost:8080")
    assert derived == "http://localhost:8080/stream"


def test_derive_preserves_path_prefix_before_api() -> None:
    """Reverse-proxied deployment with a path prefix: keep the prefix."""
    derived = _derive_stream_url("https://example.com/services/api")
    assert derived == "https://example.com/services/stream"


def test_load_config_uses_default_api_base_and_derived_stream_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env vars set → use DEFAULT_API_BASE and derive stream_url from it."""
    for var in (
        "REMOTECODETROL_API_BASE",
        "REMOTECODETROL_STREAM_URL",
        "REMOTECODETROL_THREAD",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = load_config()
    assert cfg.api_base == DEFAULT_API_BASE
    assert cfg.stream_url == _derive_stream_url(DEFAULT_API_BASE)
    assert cfg.stream_url.endswith("/stream")
    assert "/api/" not in cfg.stream_url  # the bug we hotfixed


def test_load_config_explicit_stream_url_overrides_derivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REMOTECODETROL_STREAM_URL takes precedence over derivation."""
    monkeypatch.setenv(
        "REMOTECODETROL_API_BASE", "https://us-central1-foo.cloudfunctions.net/api"
    )
    monkeypatch.setenv(
        "REMOTECODETROL_STREAM_URL", "https://custom.example.com/streamy"
    )
    cfg = load_config()
    assert cfg.stream_url == "https://custom.example.com/streamy"


def test_load_config_custom_api_base_derives_matching_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom api_base with /api suffix → stream URL on the same host."""
    monkeypatch.setenv("REMOTECODETROL_API_BASE", "https://staging.example.com/api")
    monkeypatch.delenv("REMOTECODETROL_STREAM_URL", raising=False)
    cfg = load_config()
    assert cfg.api_base == "https://staging.example.com/api"
    assert cfg.stream_url == "https://staging.example.com/stream"


def test_load_config_api_v1_property_is_correct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: api_v1 keeps appending /v1 to api_base regardless of stream_url."""
    monkeypatch.setenv("REMOTECODETROL_API_BASE", "https://example.com/api")
    monkeypatch.delenv("REMOTECODETROL_STREAM_URL", raising=False)
    cfg = load_config()
    assert cfg.api_v1 == "https://example.com/api/v1"
