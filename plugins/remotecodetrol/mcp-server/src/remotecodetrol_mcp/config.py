"""Environment-variable-driven configuration for the MCP server.

All values are optional with sensible defaults so the user can install the
plugin and just go. Per-call parameters in tools.py override these.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass


DEFAULT_API_BASE = "https://us-central1-remotecodetrol.cloudfunctions.net/api"
DEFAULT_KEYCHAIN_SERVICE = "com.remotecodetrol.mcp"
DEFAULT_POLL_INTERVAL_SECONDS = 300
DEFAULT_TIMEOUT_MINUTES = 10


@dataclass(frozen=True)
class Config:
    api_base: str
    stream_url: str
    default_thread: str | None
    device_label: str
    keychain_service: str
    default_poll_interval_seconds: int
    default_timeout_minutes: int

    @property
    def api_v1(self) -> str:
        return f"{self.api_base.rstrip('/')}/v1"


def _derive_stream_url(api_base: str) -> str:
    """Derive the SSE stream endpoint URL from `api_base`.

    The stream endpoint is deployed as a SEPARATE Cloud Function (named
    `stream`) — not as a route under the api function. Cloud Functions maps
    each function name to its own URL path, so:

        api function     → https://<region>-<project>.cloudfunctions.net/api
        stream function  → https://<region>-<project>.cloudfunctions.net/stream

    Both share a host. The api_base typically ends in `/api`; we strip that
    suffix (if present) and append `/stream` to get the stream URL.

    For non-cloudfunctions deployments (e.g. a single Express server hosting
    everything behind a reverse proxy), users can set
    `REMOTECODETROL_STREAM_URL` explicitly to bypass derivation.
    """
    base = api_base.rstrip("/")
    if base.endswith("/api"):
        base = base[: -len("/api")]
    return f"{base}/stream"


def _default_device_label() -> str:
    try:
        host = socket.gethostname()
    except OSError:
        host = "mac"
    return f"Claude Code on {host}"


def load_config() -> Config:
    """Build a Config from environment variables (with defaults)."""
    api_base = os.environ.get("REMOTECODETROL_API_BASE", DEFAULT_API_BASE)
    stream_url = os.environ.get("REMOTECODETROL_STREAM_URL") or _derive_stream_url(
        api_base
    )
    return Config(
        api_base=api_base,
        stream_url=stream_url,
        default_thread=os.environ.get("REMOTECODETROL_THREAD") or None,
        device_label=os.environ.get(
            "REMOTECODETROL_DEVICE_LABEL", _default_device_label()
        ),
        keychain_service=os.environ.get(
            "REMOTECODETROL_KEYCHAIN_SERVICE", DEFAULT_KEYCHAIN_SERVICE
        ),
        default_poll_interval_seconds=int(
            os.environ.get(
                "REMOTECODETROL_DEFAULT_POLL_INTERVAL_SECONDS",
                str(DEFAULT_POLL_INTERVAL_SECONDS),
            )
        ),
        default_timeout_minutes=int(
            os.environ.get(
                "REMOTECODETROL_DEFAULT_TIMEOUT_MINUTES",
                str(DEFAULT_TIMEOUT_MINUTES),
            )
        ),
    )
