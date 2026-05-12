"""ASCII QR-code rendering for terminal display.

Renders a QR code as a string of two-character-wide blocks suitable for
display in monospace terminals. iPhone Camera and the in-app scanner both
read these reliably when displayed in a code block.

Renders inverted (white modules on dark background) by default — this
scans correctly in dark-themed terminals (the common case). For light
terminals, callers can pass `dark_terminal=False`.
"""

from __future__ import annotations

import io


def render_qr_ascii(data: str, *, dark_terminal: bool = True) -> str:
    """Render `data` as an ASCII QR code suitable for terminal display.

    Uses error-correction level M (~15%), which tolerates terminal-rendering
    artifacts while keeping the size compact. Border = 2 modules per the
    QR spec recommendation.

    `qrcode` is a pure-Python dep (no Pillow needed for ASCII output).
    """
    # Local import so the module fails-soft if `qrcode` isn't available
    # (e.g., during partial install). Tools can fall back to the user_code
    # text-only display.
    import qrcode  # type: ignore[import-untyped]

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)

    buf = io.StringIO()
    # invert=True → dark modules become space-equivalents and light modules
    # become solid blocks → renders as white-on-dark (which scans in dark
    # terminals). invert=False is the inverse.
    qr.print_ascii(out=buf, invert=dark_terminal)
    return buf.getvalue()
