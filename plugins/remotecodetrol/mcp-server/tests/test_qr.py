"""Smoke tests for ASCII QR rendering.

`render_qr_ascii` is a thin wrapper around the `qrcode` library — we just
verify it returns non-empty output for both a trivial input and a realistic
device-link URL. If the optional `qrcode` dep isn't installed, the whole
module is skipped.
"""

from __future__ import annotations

import pytest

# Skip the entire module if qrcode isn't installed (it's an optional dep).
pytest.importorskip("qrcode")

from remotecodetrol_mcp.qr import render_qr_ascii


# Common heavy block characters that qrcode's print_ascii emits.
_BLOCK_CHARS = ("█", "▀", "▄", "░", "▓", " ")


def _has_block_character(s: str) -> bool:
    """True if `s` contains at least one printable QR module character.

    qrcode renders modules using upper/lower half blocks (▀▄) plus full
    blocks (█); we accept any of these as evidence of a real QR.
    """
    return any(c in s for c in ("█", "▀", "▄", "▓"))


def test_render_qr_ascii_returns_nonempty_string():
    out = render_qr_ascii("hello")
    assert isinstance(out, str)
    assert len(out) > 0
    assert _has_block_character(out)


def test_render_qr_ascii_handles_real_world_url():
    """Real deep-link URL must not crash and must produce QR output."""
    out = render_qr_ascii("remotecodetrol://authorize?code=ABCD-1234")
    assert isinstance(out, str)
    assert len(out) > 0
