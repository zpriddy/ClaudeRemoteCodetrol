"""Unix-domain-socket dispatch behavior for v0.4.0 CLI surface.

We exercise the SocketServer end-to-end: bind a socket at a tmp path, open
a real `asyncio.open_unix_connection` against it, write/read JSON lines,
and assert on the wire response.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

from remotecodetrol_mcp.socket_server import SocketServer


@pytest.fixture
def short_tmp_path():
    """A tmpdir under /tmp (≤ 100 chars) so AF_UNIX paths fit the 104-char
    sun_path cap on macOS.

    pytest's `tmp_path` lives under /private/var/folders/... which exceeds
    that limit when combined with a filename — bind fails with EINVAL
    "AF_UNIX path too long". Per Python's `socket` docs and POSIX
    sun_path[108] / Darwin sun_path[104], we explicitly pick a short
    parent.
    """
    d = Path(tempfile.mkdtemp(prefix="rcct-sock-", dir="/tmp"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


async def _request(socket_path: Path, payload: str | dict) -> dict:
    """Send one request line, read one response line, return decoded JSON."""
    reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
    try:
        if isinstance(payload, dict):
            line = (json.dumps(payload) + "\n").encode("utf-8")
        else:
            # Raw string (for malformed-JSON tests).
            line = (payload + "\n").encode("utf-8")
        writer.write(line)
        await writer.drain()
        resp_line = await reader.readline()
        return json.loads(resp_line.decode("utf-8"))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def test_dispatcher_echo_returns_args(tmp_path):
    """Happy path: a known cmd dispatches and returns ok=true with the result."""

    async def echo(args: dict) -> Any:
        return args

    sock_path = tmp_path / "mcp.sock"
    server = SocketServer({"echo": echo}, path=sock_path)
    await server.start()
    try:
        resp = await _request(sock_path, {"cmd": "echo", "args": {"x": 1}})
        assert resp == {"ok": True, "result": {"x": 1}}
    finally:
        await server.stop()


async def test_dispatcher_unknown_command(tmp_path):
    async def echo(args: dict) -> Any:
        return args

    sock_path = tmp_path / "mcp.sock"
    server = SocketServer({"echo": echo}, path=sock_path)
    await server.start()
    try:
        resp = await _request(sock_path, {"cmd": "nope", "args": {}})
        assert resp["ok"] is False
        assert resp["error"] == "unknown_command"
        assert "nope" in resp["message"]
    finally:
        await server.stop()


async def test_dispatcher_invalid_json(tmp_path):
    async def echo(args: dict) -> Any:
        return args

    sock_path = tmp_path / "mcp.sock"
    server = SocketServer({"echo": echo}, path=sock_path)
    await server.start()
    try:
        resp = await _request(sock_path, "{not valid json")
        assert resp["ok"] is False
        assert resp["error"] == "invalid_json"
    finally:
        await server.stop()


async def test_dispatcher_value_error_returns_invalid_argument(tmp_path):
    async def boom(args: dict) -> Any:
        raise ValueError("bad arg shape")

    sock_path = tmp_path / "mcp.sock"
    server = SocketServer({"boom": boom}, path=sock_path)
    await server.start()
    try:
        resp = await _request(sock_path, {"cmd": "boom", "args": {}})
        assert resp["ok"] is False
        assert resp["error"] == "invalid_argument"
        assert "bad arg shape" in resp["message"]
    finally:
        await server.stop()


async def test_dispatcher_unexpected_exception_returns_internal(tmp_path):
    """Sanity: any non-ValueError surfaces as 'internal'."""

    async def crash(args: dict) -> Any:
        raise RuntimeError("kaboom")

    sock_path = tmp_path / "mcp.sock"
    server = SocketServer({"crash": crash}, path=sock_path)
    await server.start()
    try:
        resp = await _request(sock_path, {"cmd": "crash", "args": {}})
        assert resp["ok"] is False
        assert resp["error"] == "internal"
    finally:
        await server.stop()


async def test_stale_socket_is_unlinked_on_start(tmp_path):
    """Pre-create the socket path as a regular file → start() unlinks it
    and binds fresh."""
    sock_path = tmp_path / "mcp.sock"
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    # Drop a stub file at the socket path. The probe will hit ENOTSOCK or
    # ECONNREFUSED depending on platform; either way the file should not
    # block us.
    sock_path.write_text("stale")
    assert sock_path.exists() and sock_path.is_file()

    async def echo(args: dict) -> Any:
        return args

    server = SocketServer({"echo": echo}, path=sock_path)
    await server.start()
    try:
        # If the bind succeeded, the path is now a real socket. We don't
        # check S_ISSOCK directly (test-only Pythonism); instead, verify
        # we can actually round-trip a request.
        resp = await _request(sock_path, {"cmd": "echo", "args": {"k": "v"}})
        assert resp == {"ok": True, "result": {"k": "v"}}
    finally:
        await server.stop()
