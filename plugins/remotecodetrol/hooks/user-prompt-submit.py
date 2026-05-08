#!/usr/bin/env python3
"""UserPromptSubmit hook — inject pending RemoteCodetrol replies as context.

Runs on every Claude prompt. Reads the atomic state file the MCP server
writes (pending.json), diffs against this session's "already-shown" cursor,
and emits any new replies as `additionalContext` so the model sees them
without needing a tool call.

Design constraints (per spec §6):
  * Pure stdlib — Claude Code plugins shouldn't require pip installs.
  * Robust to missing files, missing env vars, malformed JSON. Never raise;
    just exit 0 silently if anything goes wrong (worst case: user fails to
    see one inject, hook fires again next prompt).
  * Atomic cursor writes (.tmp + os.rename) — no partial reads from a
    concurrent session.
  * Prune cursor files older than 14 days for tidiness.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Per spec §6:
#   if CLAUDE_PROJECT_DIR is set: ${CLAUDE_PROJECT_DIR}/.claude/cache/remotecodetrol-pending.json
#   else (macOS): ~/Library/Caches/remotecodetrol/pending.json
#   else (linux): ~/.cache/remotecodetrol/pending.json
PRUNE_AGE_SECONDS = 14 * 24 * 60 * 60  # 14 days


def _macos_cache_root() -> Path:
    return Path.home() / "Library" / "Caches" / "remotecodetrol"


def _linux_cache_root() -> Path:
    # Honor XDG_CACHE_HOME if set; otherwise ~/.cache
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else (Path.home() / ".cache")
    return base / "remotecodetrol"


def _default_cache_root() -> Path:
    if sys.platform == "darwin":
        return _macos_cache_root()
    return _linux_cache_root()


def _pending_path() -> Path:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return Path(project_dir) / ".claude" / "cache" / "remotecodetrol-pending.json"
    return _default_cache_root() / "pending.json"


def _sessions_dir() -> Path:
    # Cursor files always live in the user-cache, never the project dir.
    # Sessions are independent of the project; using project dir would
    # break "same session, different cwd" — Claude Code sessions can hop.
    return _default_cache_root() / "sessions"


def _read_json(path: Path) -> Any | None:
    """Return parsed JSON, or None on any failure (missing, malformed, race)."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, NotADirectoryError, PermissionError, IsADirectoryError):
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    except OSError:
        return None


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically. Best-effort: silently swallow errors."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        # If we can't persist the cursor, the worst case is re-injecting
        # the same message next turn. Annoying, not broken.
        pass


def _prune_old_cursor_files(sessions_dir: Path, now: float) -> None:
    """Delete cursor files older than 14 days. Tidiness only."""
    try:
        if not sessions_dir.is_dir():
            return
        cutoff = now - PRUNE_AGE_SECONDS
        for entry in sessions_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago" if seconds != 1 else "1s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago" if minutes != 1 else "1 min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago" if hours != 1 else "1h ago"
    days = hours // 24
    return f"{days}d ago" if days != 1 else "1d ago"


def _parse_iso(ts: str) -> float | None:
    """Parse ISO 8601 timestamp into epoch seconds. Returns None on failure."""
    if not isinstance(ts, str):
        return None
    # Python's fromisoformat tolerates "+00:00" but not "Z"; normalize.
    try:
        from datetime import datetime
        normalized = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None


def _truncate_preview(text: str, limit: int = 100) -> str:
    if not isinstance(text, str):
        return ""
    text = text.strip().replace("\n", " ")
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _format_message_lines(messages: list[dict[str, Any]], now: float) -> list[str]:
    """Render each message as one line: [thread:NAME] (Xm ago) "preview"."""
    lines: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        thread_name = msg.get("thread_name") or msg.get("thread_id") or "unknown"
        body = msg.get("body", "")
        preview = msg.get("preview") or _truncate_preview(body)
        created_at = msg.get("created_at")
        ts = _parse_iso(created_at) if isinstance(created_at, str) else None
        age_str = _format_age(now - ts) if ts is not None else "just now"
        lines.append(f'[thread:{thread_name}] ({age_str}) "{preview}"')
    return lines


def _build_additional_context(messages: list[dict[str, Any]], now: float) -> str:
    lines = _format_message_lines(messages, now)
    body = "\n".join(lines)
    return (
        "Messages received from user via RemoteCodetrol while you were idle:\n\n"
        f"{body}\n\n"
        "Follow the remotecodetrol skill: read these, decide if relevant, "
        "acknowledge via ack_messages, then continue."
    )


def main() -> int:
    now = time.time()

    # Housekeeping first — runs regardless of whether we have anything to
    # inject this turn. Cheap (one stat per file).
    sessions_dir = _sessions_dir()
    _prune_old_cursor_files(sessions_dir, now)

    # Step 1: Read pending state. Missing/malformed → exit silently.
    pending_path = _pending_path()
    pending_doc = _read_json(pending_path)
    if not isinstance(pending_doc, dict):
        return 0
    pending = pending_doc.get("pending")
    if not isinstance(pending, list):
        return 0

    # Drop entries we can't safely diff on.
    pending_with_ids = [m for m in pending if isinstance(m, dict) and isinstance(m.get("id"), str)]

    # Step 2: Per-session cursor.
    session_id = os.environ.get("CLAUDE_SESSION_ID")
    if not session_id:
        # Without a session ID we can't diff stably across prompts. Bail
        # silently — re-injecting on every turn would spam the model.
        return 0

    cursor_path = sessions_dir / f"{session_id}.json"
    cursor_doc = _read_json(cursor_path) or {}
    shown = cursor_doc.get("last_shown_ids") if isinstance(cursor_doc, dict) else None
    shown_ids = set(shown) if isinstance(shown, list) else set()

    # Step 3: Diff.
    new_messages = [m for m in pending_with_ids if m["id"] not in shown_ids]
    if not new_messages:
        return 0

    # Step 4: Emit additionalContext.
    context = _build_additional_context(new_messages, now)
    output = {"additionalContext": context}
    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()

    # Step 5: Update cursor with the union of previously-shown + new.
    # Keep only IDs that are still in pending so the cursor doesn't grow
    # unbounded — once a message is acked it falls out of pending, so
    # there's no need to remember we showed it.
    next_shown = {m["id"] for m in pending_with_ids}
    _atomic_write_json(cursor_path, {"last_shown_ids": sorted(next_shown)})

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # The hook is best-effort; never break the user's prompt submission.
        sys.exit(0)
