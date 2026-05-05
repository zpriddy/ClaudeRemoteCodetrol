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
    default_thread: str | None
    device_label: str
    keychain_service: str
    default_poll_interval_seconds: int
    default_timeout_minutes: int

    @property
    def api_v1(self) -> str:
        return f"{self.api_base.rstrip('/')}/v1"


def _default_device_label() -> str:
    try:
        host = socket.gethostname()
    except OSError:
        host = "mac"
    return f"Claude Code on {host}"


def load_config() -> Config:
    """Build a Config from environment variables (with defaults)."""
    return Config(
        api_base=os.environ.get("REMOTECODETROL_API_BASE", DEFAULT_API_BASE),
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
