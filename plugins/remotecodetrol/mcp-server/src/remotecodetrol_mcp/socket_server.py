"""Unix-domain-socket dispatch for the `rcct` CLI (v0.4.0+).

The MCP process binds a single 0600-permissioned socket and accepts
single-request connections. Each connection: read one JSON line, dispatch
to the matching tool function, write one JSON line response, close.

Wire format:

    Request:  {"cmd": "send_message", "args": {"body": "hi"}}\n
    Response: {"ok": true, "result": {...}}\n
              {"ok": false, "error": "<error_kind>", "message": "<human>"}\n

The `cmd` strings map 1:1 to MCP tool names. The `args` dict maps to the
tool's keyword arguments. Tools are reused as-is — no separate
implementation. See spec §4.4.

Socket location: ~/Library/Caches/remotecodetrol/mcp.sock (macOS) /
${XDG_RUNTIME_DIR:-~/.cache}/remotecodetrol/mcp.sock (linux). Stale
sockets from a prior crashed MCP are detected (connect attempt → ECONNREFUSED
→ unlink + bind fresh).
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import socket as socket_mod
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable


logger = logging.getLogger("remotecodetrol_mcp.socket_server")


def default_socket_path() -> Path:
    """Stable path for the dispatch socket.

    macOS: ~/Library/Caches/remotecodetrol/mcp.sock
    Linux: $XDG_RUNTIME_DIR/remotecodetrol/mcp.sock if set, else
           ~/.cache/remotecodetrol/mcp.sock
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "remotecodetrol"
    else:
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        base = (Path(runtime) if runtime else Path.home() / ".cache") / "remotecodetrol"
    return base / "mcp.sock"


# A dispatch handler takes the raw `args` dict and returns the result payload
# (or raises). The CLI subcommand layer pre-validates required args; the MCP
# tool layer does its own validation too.
Dispatcher = Callable[[dict[str, Any]], Awaitable[Any]]


class SocketServer:
    """Owns the AF_UNIX listener + dispatch loop.

    Construct with `SocketServer(dispatchers, path=...)` then `await server.start()`
    to bind and start serving in the background. Returns the asyncio.Server
    so callers can stop it later.
    """

    def __init__(
        self,
        dispatchers: dict[str, Dispatcher],
        *,
        path: Path | None = None,
    ):
        self.dispatchers = dispatchers
        self.path = path or default_socket_path()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> asyncio.AbstractServer:
        """Bind the socket and start serving. Idempotent (calling twice
        returns the same server)."""
        if self._server is not None:
            return self._server
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Best-effort 0700 on the cache dir.
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._handle_stale_socket()
        self._server = await asyncio.start_unix_server(
            self._handle_conn, path=str(self.path)
        )
        try:
            os.chmod(self.path, 0o600)
        except OSError as e:
            logger.warning("socket chmod 0600 failed at %s: %s", self.path, e)
        logger.info("CLI socket listening at %s", self.path)
        return self._server

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:
            pass
        try:
            self.path.unlink()
        except (OSError, FileNotFoundError):
            pass
        self._server = None

    # ---- internals ----

    def _handle_stale_socket(self) -> None:
        """If a socket file exists at our path, decide whether to unlink it.

        Stale = file exists but no process is listening (connect raises
        ECONNREFUSED). Live = some other MCP holds it; we should NOT
        unlink, but we also can't bind — so log and let bind fail loudly.
        """
        if not self.path.exists():
            return
        probe = socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM)
        try:
            probe.connect(str(self.path))
            # Successfully connected → another MCP owns this socket. Leave
            # it alone; our bind will fail and the caller can decide.
            probe.close()
            logger.warning(
                "socket at %s appears to be in use by another MCP process",
                self.path,
            )
        except OSError as e:
            probe.close()
            if e.errno in (errno.ECONNREFUSED, errno.ENOENT):
                # Stale. Safe to unlink.
                try:
                    self.path.unlink()
                    logger.info("removed stale socket at %s", self.path)
                except OSError as ue:
                    logger.warning("failed to unlink stale socket %s: %s", self.path, ue)
            else:
                logger.warning("socket probe failed at %s: %s", self.path, e)

    async def _handle_conn(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line)
            except json.JSONDecodeError as e:
                await self._respond_error(writer, "invalid_json", str(e))
                return
            cmd = req.get("cmd")
            args = req.get("args") or {}
            if not isinstance(cmd, str) or not isinstance(args, dict):
                await self._respond_error(
                    writer, "invalid_request", "cmd:str + args:dict required"
                )
                return
            handler = self.dispatchers.get(cmd)
            if handler is None:
                await self._respond_error(
                    writer, "unknown_command", f"no dispatcher for '{cmd}'"
                )
                return
            try:
                result = await handler(args)
            except ValueError as e:
                await self._respond_error(writer, "invalid_argument", str(e))
                return
            except Exception as e:
                logger.exception("dispatch %s raised", cmd)
                await self._respond_error(writer, "internal", str(e))
                return
            await self._respond_ok(writer, result)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _respond_ok(writer: asyncio.StreamWriter, result: Any) -> None:
        body = json.dumps({"ok": True, "result": _coerce_jsonable(result)})
        writer.write((body + "\n").encode("utf-8"))
        await writer.drain()

    @staticmethod
    async def _respond_error(
        writer: asyncio.StreamWriter, error: str, message: str
    ) -> None:
        body = json.dumps({"ok": False, "error": error, "message": message})
        writer.write((body + "\n").encode("utf-8"))
        await writer.drain()


def _coerce_jsonable(obj: Any) -> Any:
    """Convert pydantic models, dataclasses, and other common shapes into
    JSON-serialisable structures.

    Pydantic v2 models expose `model_dump()`. Lists/tuples/dicts get
    recursively coerced. Anything else falls through to the default
    json.dumps codec (will raise on unsupported types).
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, (list, tuple)):
        return [_coerce_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _coerce_jsonable(v) for k, v in obj.items()}
    return obj
