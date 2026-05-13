"""Atomic state-file writer for the UserPromptSubmit hook.

The hook (`hooks/user-prompt-submit.py`) reads this file each turn and
emits an `additionalContext` block describing pending replies the user
sent while Claude was idle. Format and atomicity are specified in §6 of
the design doc (`docs/superpowers/specs/2026-05-07-mcp-streaming-relay-design.md`).

Path resolution priority:
  1. ``$CLAUDE_PROJECT_DIR/.claude/cache/remotecodetrol-pending.json``
     — when set, scopes pending replies to the current project so two
     concurrent Claude sessions in different projects don't see each
     other's mail.
  2. ``~/Library/Caches/remotecodetrol/pending.json`` (macOS default)
  3. ``~/.cache/remotecodetrol/pending.json`` (Linux fallback)

Atomicity: we always write to ``pending.json.tmp`` and ``os.replace`` to
the final name. POSIX guarantees rename-into-existing is atomic, so the
hook never sees a partial JSON file.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .streaming import StreamingState


logger = logging.getLogger("remotecodetrol_mcp.state_file")


# v2 (v0.6.1): entries gain a `raw` field holding the full message dict
# so non-leader MCPs (followers in leader-elected polling) can reconstruct
# the cache from pending.json on demand. v1 readers (the hook) ignore the
# extra key. Existing v1 pending.json files written before the upgrade
# remain readable — `read_state_file` tolerates missing `raw` and falls
# back to the v1 shape.
SCHEMA_VERSION = 2
PREVIEW_LEN = 100


def state_file_path() -> Path:
    """Resolve where pending.json lives. See module docstring for priority."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return Path(project_dir) / ".claude" / "cache" / "remotecodetrol-pending.json"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "remotecodetrol" / "pending.json"
    # Linux / others: respect XDG_CACHE_HOME if set, else ~/.cache.
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "remotecodetrol" / "pending.json"


def _serialize_state(state: StreamingState) -> dict[str, Any]:
    """Build the JSON document the hook expects.

    The hook only needs a flat list of pending messages with their previews,
    so we squash the per-thread cache into a single sorted list (oldest
    first by created_at).
    """
    flat: list[dict[str, Any]] = []
    for msgs in state.pending.values():
        flat.extend(msgs)
    # Sort oldest-first by created_at when present.
    flat.sort(key=lambda m: m.get("created_at") or "")

    pending: list[dict[str, Any]] = []
    for m in flat:
        body = m.get("body", "") or ""
        preview = body[:PREVIEW_LEN]
        if len(body) > PREVIEW_LEN:
            preview = preview.rstrip() + "…"
        pending.append({
            "id": m.get("id"),
            "thread_id": m.get("thread_id"),
            "thread_name": m.get("thread_name") or m.get("thread_id"),
            "body": body,
            "created_at": m.get("created_at"),
            "preview": preview,
            # v0.6.1: full message dict for follower reconstruction.
            # The hook only reads the flat preview/id/thread_id keys
            # above; this extra field is invisible to it.
            "raw": m,
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "updated_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pending": pending,
    }


def write_state_file(state: StreamingState, *, path: Path | None = None) -> Path:
    """Atomically write `pending.json`.

    Returns the path written. Never raises on benign filesystem hiccups
    (logs warnings instead) so a flaky cache directory can't bring down
    the SSE consumer.
    """
    target = path or state_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _serialize_state(state)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)

    # Write to a sibling .tmp then os.replace — POSIX-atomic.
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the tmp file.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return target


def make_writer(path: Path | None = None):
    """Bind the writer to a path so the polling consumer can call it
    directly. Returns a callable that writes the current state to disk
    and logs (rather than raises) on transient filesystem errors."""
    def _writer(state: StreamingState) -> None:
        try:
            write_state_file(state, path=path)
        except Exception as e:
            logger.warning("write_state_file failed: %s", e)
    return _writer


def read_state_file(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read pending.json and return the list of full message dicts the
    leader's poll loop produced.

    Used by follower MCPs in leader-elected polling: their own
    `streaming.pending` is empty, but `peek_messages` tool calls still
    need to return whatever the leader has cached. A sub-ms file read is
    much cheaper than the alternative — making the follower hit the
    backend API directly, which defeats the leader-election savings.

    Returns [] if the file doesn't exist, is malformed, or was written
    by a pre-v0.6.1 schema (no `raw` field). The caller decides how to
    handle empty results (typically: fall through to a direct API peek
    for v0.6.0-compat).
    """
    target = path or state_file_path()
    try:
        with open(target, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    out: list[dict[str, Any]] = []
    for entry in doc.get("pending", []):
        raw = entry.get("raw")
        if isinstance(raw, dict):
            out.append(raw)
    return out
