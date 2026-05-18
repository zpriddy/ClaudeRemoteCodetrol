"""`rcct` CLI — talks to a running MCP via Unix domain socket (v0.4.0+).

Single-request-per-connection wire protocol (see socket_server.py). The CLI
opens the socket, writes one JSON line, reads one JSON line, prints a
human-readable response, exits.

If the socket isn't responding (no MCP running, or the path is stale and
nothing is listening), the CLI fails with a hint — it does NOT auto-spawn
an MCP. Spawning a second MCP would create two writers on tokens.json
(race condition risk) and two SSE consumers (duplicate event delivery).
The MCP is expected to already be running inside the active Claude Code
session.

Exit codes:
  0 — success
  1 — server returned ok=false (validation, auth, etc.)
  2 — connection failure (no MCP running)
  3 — usage error / unknown subcommand
  4 — protocol error (server returned malformed response)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket as socket_mod
import sys
from pathlib import Path
from typing import Any

from .socket_server import default_socket_path


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rcct",
        description=(
            "RemoteCodetrol CLI — fast counterpart to the MCP tools. "
            "Sends commands over Unix domain socket to the MCP process "
            "running in your active Claude Code session."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human-readable text.",
    )
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help=(
            "Override the socket path. Defaults to "
            "~/Library/Caches/remotecodetrol/mcp.sock (macOS) / "
            "$XDG_RUNTIME_DIR/remotecodetrol/mcp.sock (linux)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # `send` / `send-message` are the same command. The `send-message`
    # alias was added in v0.7.0 to match the slash command naming
    # (/rc:send_message).
    for send_name in ("send", "send-message"):
        p_send = sub.add_parser(send_name, help="Send a message to the user")
        p_send.add_argument("body", help="Message body (markdown rendered)")
        p_send.add_argument("--thread", help="Override active thread")
        p_send.add_argument(
            "--require-response",
            action="store_true",
            help="Mark the message as expecting a reply",
        )
        p_send.add_argument(
            "--idempotency-key", help="Idempotency key for de-dup on the server"
        )

    # v0.7.0: send + block until reply, in one call. Maps to
    # send_message(require_response=True, wait=True). The CLI exposes
    # `--timeout` so callers can override the default; without it we use
    # the server's `default_timeout_minutes` config (usually 10).
    p_send_wait = sub.add_parser(
        "send-wait",
        help="Send a message and block until the user replies (one round-trip)",
    )
    p_send_wait.add_argument("body", help="Message body (markdown rendered)")
    p_send_wait.add_argument("--thread", help="Override active thread")
    p_send_wait.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Minutes to wait for the reply before giving up. Default: server config.",
    )
    p_send_wait.add_argument(
        "--idempotency-key", help="Idempotency key for de-dup on the server"
    )

    # `check` / `peek` are the same command. `peek` was added in v0.7.0
    # to match the slash command (/rc:peek) and the MCP tool
    # (peek_messages). `check` is kept as an alias for back-compat with
    # users who built muscle memory pre-v0.7.0.
    for peek_name in ("check", "peek"):
        p_check = sub.add_parser(
            peek_name, help="Show pending replies (peek_messages) — no ack"
        )
        p_check.add_argument("--thread", help="Override active thread")
        p_check.add_argument(
            "--all",
            action="store_true",
            help="Return pending across all known threads (cache only)",
        )

    # `wait` / `wait-blocked` are the same command. `wait-blocked` was
    # added in v0.7.0 to match the slash command naming (/rc:wait_blocked).
    for wait_name in ("wait", "wait-blocked"):
        p_wait = sub.add_parser(
            wait_name, help="Wait for a reply (one-shot, with timeout)"
        )
        p_wait.add_argument("--thread", help="Override active thread")
        p_wait.add_argument("--timeout", type=float, default=None, help="Minutes")

    p_ack = sub.add_parser("ack", help="Acknowledge processed messages")
    p_ack.add_argument("message_ids", nargs="+", help="Firestore doc ids")
    p_ack.add_argument("--thread", help="Override active thread")

    # v0.7.0: combined peek + ack. Returns the messages that were just
    # acked so Claude can act on them without separate fetch + ack
    # bookkeeping. Maps to the new MCP tool `get_messages`.
    p_get = sub.add_parser(
        "get-messages",
        help="Fetch all unacked replies AND ack them (one round-trip)",
    )
    p_get.add_argument("--thread", help="Override active thread")

    # v0.7.1: context recovery — fetch the last N messages including
    # already-acked ones. Different from `get-messages` which only
    # returns unacked entries.
    p_get_last = sub.add_parser(
        "get-last",
        help="Fetch the last N user messages including acked ones (context recovery)",
    )
    p_get_last.add_argument("--thread", help="Override active thread")
    p_get_last.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many messages back to fetch (default 20, server max 100)",
    )

    sub.add_parser("link", help="Start the OAuth device-code flow + show QR")
    sub.add_parser(
        "check-link",
        help="Force-poll: did the user authorize yet? Bypasses cooldowns.",
    )
    sub.add_parser("whoami", help="Show identity + SSE status + plugin version")
    sub.add_parser("logout", help="Clear all stored credentials")

    p_threads = sub.add_parser("threads", help="Manage thread allowlist")
    threads_sub = p_threads.add_subparsers(dest="threads_cmd", required=True)
    threads_sub.add_parser("list", help="List all backend threads (with known flag)")
    p_allow = threads_sub.add_parser("allow", help="Add to known_threads")
    p_allow.add_argument("names", nargs="+", help="Thread name(s) to allow")
    p_forget = threads_sub.add_parser("forget", help="Remove from known_threads")
    p_forget.add_argument("name", help="Thread name to forget")
    threads_sub.add_parser("known", help="Show the in-memory known_threads set")

    return parser.parse_args(argv)


def _build_request(ns: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Map argparse Namespace → (cmd, args) for the socket dispatcher.

    Returns command names that match MCP tool names exactly.
    """
    if ns.cmd in ("send", "send-message"):
        args: dict[str, Any] = {
            "body": ns.body,
            "require_response": bool(ns.require_response),
        }
        if ns.thread:
            args["thread"] = ns.thread
        if ns.idempotency_key:
            args["idempotency_key"] = ns.idempotency_key
        return "send_message", args
    if ns.cmd == "send-wait":
        # v0.7.0: send_message(require_response=True, wait=True) — the
        # MCP blocks inside the tool call until a reply arrives or the
        # timeout elapses. The result's `pending_messages` is the reply.
        args = {
            "body": ns.body,
            "require_response": True,
            "wait": True,
        }
        if ns.thread:
            args["thread"] = ns.thread
        if ns.timeout is not None:
            args["timeout_minutes"] = ns.timeout
        if ns.idempotency_key:
            args["idempotency_key"] = ns.idempotency_key
        return "send_message", args
    if ns.cmd in ("check", "peek"):
        args = {}
        if ns.all:
            args["thread"] = "*"
        elif ns.thread:
            args["thread"] = ns.thread
        return "peek_messages", args
    if ns.cmd in ("wait", "wait-blocked"):
        args = {}
        if ns.thread:
            args["thread"] = ns.thread
        if ns.timeout is not None:
            args["timeout_minutes"] = ns.timeout
        return "wait_for_response", args
    if ns.cmd == "ack":
        args = {"message_ids": list(ns.message_ids)}
        if ns.thread:
            args["thread"] = ns.thread
        return "ack_messages", args
    if ns.cmd == "get-messages":
        # v0.7.0: combined peek + ack via the new MCP tool.
        args = {}
        if ns.thread:
            args["thread"] = ns.thread
        return "get_messages", args
    if ns.cmd == "get-last":
        # v0.7.1: last-N including acked, for context recovery.
        args = {"limit": int(ns.limit)}
        if ns.thread:
            args["thread"] = ns.thread
        return "get_last_messages", args
    if ns.cmd == "link":
        return "link", {}
    if ns.cmd == "check-link":
        return "complete_link", {}
    if ns.cmd == "whoami":
        return "whoami", {}
    if ns.cmd == "logout":
        return "logout", {}
    if ns.cmd == "threads":
        if ns.threads_cmd == "list":
            return "list_threads", {}
        if ns.threads_cmd == "allow":
            # Multi-name allow: send sequentially via set_thread; we treat
            # the LAST name as also becoming the active thread (matches the
            # historical set_thread semantic). For the others, set_thread
            # auto-adds them to known_threads as a side effect.
            # The CLI handles this by issuing one set_thread per name.
            return "_multi_allow", {"names": list(ns.names)}
        if ns.threads_cmd == "forget":
            return "forget_thread", {"name": ns.name}
        if ns.threads_cmd == "known":
            return "list_known_threads", {}
    raise SystemExit(3)


async def _send_one(
    socket_path: Path, cmd: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Open the socket, send one request, read one response, close.

    Returns the parsed response dict. Raises ConnectionError on transport
    failure (so main() can exit 2 with a useful hint).
    """
    try:
        reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError) as e:
        raise ConnectionError(str(e)) from e
    try:
        body = json.dumps({"cmd": cmd, "args": args}) + "\n"
        writer.write(body.encode("utf-8"))
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise RuntimeError("server closed connection without responding")
        try:
            return json.loads(line)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"malformed server response: {e}") from e
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_multi_allow(
    socket_path: Path, names: list[str]
) -> dict[str, Any]:
    """`rcct threads allow foo bar` → multiple set_thread calls.

    Each set_thread also adds the name to known_threads (it does double-duty
    in v0.4.0+). The LAST name becomes the active thread.
    """
    last_result: dict[str, Any] = {}
    for name in names:
        resp = await _send_one(socket_path, "set_thread", {"name": name})
        if not resp.get("ok"):
            return resp  # surface the error immediately
        last_result = resp
    return last_result


def _format_human(cmd: str, result: dict[str, Any]) -> str:
    """Render a server result dict into a short human-readable string.

    Falls back to compact JSON if we don't know the shape of `cmd` (so
    new commands work even before the formatter learns about them).
    """
    payload = result.get("result") if isinstance(result, dict) else None
    if cmd == "send_message" and isinstance(payload, dict):
        mid = payload.get("message_id", "?")
        thread = payload.get("thread", "?")
        pending = payload.get("pending_count", 0)
        out = f"sent {mid} on thread {thread}"
        if pending:
            out += f" ({pending} pending replies in bundle)"
        return out
    if cmd in ("peek_messages",) and isinstance(payload, dict):
        msgs = payload.get("messages") or []
        if not msgs:
            return "(no pending messages)"
        lines = []
        for m in msgs:
            tid = m.get("thread_name") or m.get("thread_id") or "?"
            body = (m.get("body") or "").replace("\n", " ")
            mid = m.get("id", "?")
            lines.append(f"[{tid}] {mid}  {body[:120]}")
        return "\n".join(lines)
    if cmd == "wait_for_response" and isinstance(payload, dict):
        msgs = payload.get("messages") or []
        if not msgs:
            return "(timed out, no reply)"
        return _format_human("peek_messages", result)
    if cmd == "ack_messages" and isinstance(payload, dict):
        return f"acked {payload.get('acked', 0)}"
    if cmd == "get_messages" and isinstance(payload, dict):
        msgs = payload.get("messages") or []
        acked = payload.get("acked", 0)
        if not msgs:
            return "(no pending messages)"
        lines = [f"got + acked {acked} messages:"]
        for m in msgs:
            tid = m.get("thread_name") or m.get("thread_id") or "?"
            body = (m.get("body") or "").replace("\n", " ")
            mid = m.get("id", "?")
            lines.append(f"  [{tid}] {mid}  {body[:120]}")
        return "\n".join(lines)
    if cmd == "get_last_messages" and isinstance(payload, dict):
        # Same PeekResult shape as peek_messages — reuse its formatter
        # so the output is consistent across "look at history" surfaces.
        msgs = payload.get("messages") or []
        if not msgs:
            return "(no messages in thread yet)"
        lines = [f"last {len(msgs)} messages:"]
        for m in msgs:
            tid = m.get("thread_name") or m.get("thread_id") or "?"
            body = (m.get("body") or "").replace("\n", " ")
            mid = m.get("id", "?")
            acked = m.get("acked_at") or m.get("ackedAt")
            mark = "✓ " if acked else "• "
            lines.append(f"  {mark}[{tid}] {mid}  {body[:120]}")
        return "\n".join(lines)
    if cmd == "whoami" and isinstance(payload, dict):
        lines = [
            f"email:           {payload.get('email')}",
            f"active_thread:   {payload.get('active_thread')}",
            f"sse_status:      {payload.get('sse_status')}",
            f"plugin_version:  {payload.get('plugin_version')}",
        ]
        known = payload.get("known_threads") or []
        if known:
            lines.append(f"known_threads:   {', '.join(known)}")
        else:
            lines.append("known_threads:   (empty — call `rcct threads allow <name>`)")
        pcounts = payload.get("pending_count_by_thread") or {}
        if pcounts:
            lines.append("pending_by_thread:")
            for k, v in pcounts.items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)
    if cmd == "link" and isinstance(payload, dict):
        lines = []
        if payload.get("status") == "already_linked":
            return f"already linked as {payload.get('email')}"
        qr = payload.get("qr_ascii")
        code = payload.get("user_code")
        if qr:
            lines.append(qr)
        lines.append(f"\nuser code: {code}")
        lines.append(f"deep link: {payload.get('deep_link')}")
        lines.append("")
        lines.append(payload.get("instructions") or "")
        return "\n".join(lines)
    if cmd == "complete_link" and isinstance(payload, dict):
        return f"{payload.get('status')}: {payload.get('message')}"
    if cmd == "logout" and isinstance(payload, dict):
        return payload.get("message") or "logged out"
    if cmd == "list_threads" and isinstance(payload, list):
        if not payload:
            return "(no threads)"
        lines = []
        for t in payload:
            mark = " [known]" if t.get("known") else ""
            lines.append(f"  {t.get('name')}{mark}")
        return "\n".join(lines)
    if cmd == "forget_thread" and isinstance(payload, dict):
        return (
            f"forgot {payload.get('name')}"
            if payload.get("was_known")
            else f"{payload.get('name')} was not known"
        )
    if cmd == "list_known_threads" and isinstance(payload, dict):
        threads = payload.get("threads") or []
        return ", ".join(threads) if threads else "(empty)"
    if cmd == "set_thread" and isinstance(payload, dict):
        return f"active_thread → {payload.get('active_thread')}"
    return json.dumps(payload, indent=2)


def _format_error(result: dict[str, Any]) -> str:
    err = result.get("error", "error")
    msg = result.get("message", "(no message)")
    return f"error ({err}): {msg}"


async def _async_main(argv: list[str]) -> int:
    ns = _parse_args(argv)
    cmd, args = _build_request(ns)
    socket_path = ns.socket or default_socket_path()

    try:
        if cmd == "_multi_allow":
            result = await _handle_multi_allow(socket_path, args["names"])
            display_cmd = "set_thread"
        else:
            result = await _send_one(socket_path, cmd, args)
            display_cmd = cmd
    except ConnectionError as e:
        sys.stderr.write(
            f"error: MCP server not responding at {socket_path}\n\n"
            "This usually means:\n"
            "  - You're not running inside a Claude Code session with the "
            "RemoteCodetrol plugin loaded.\n"
            "  - The plugin isn't installed or hasn't been linked yet.\n"
            "\n"
            "Try: open a Claude Code session with the plugin enabled, "
            "or run /rc:link.\n"
            f"\n(transport detail: {e})\n"
        )
        return 2
    except RuntimeError as e:
        sys.stderr.write(f"error: protocol failure: {e}\n")
        return 4

    if ns.json:
        print(json.dumps(result, indent=2))
    elif result.get("ok"):
        print(_format_human(display_cmd, result))
    else:
        print(_format_error(result), file=sys.stderr)

    return 0 if result.get("ok") else 1


def main() -> None:
    argv = sys.argv[1:]
    exit_code = asyncio.run(_async_main(argv))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
