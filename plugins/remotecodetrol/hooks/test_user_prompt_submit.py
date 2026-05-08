"""Tests for the UserPromptSubmit hook.

Driven via subprocess so we exercise the real entry point + env handling.
We override the cache root by setting CLAUDE_PROJECT_DIR (for pending.json)
and HOME (for the sessions/ dir). HOME-based override is the cleanest way
to keep the cursor file under our tmp_path without monkeypatching internal
helpers — the hook reads sessions/ via Path.home() unconditionally.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).parent / "user-prompt-submit.py"


def _run(env_extra: dict[str, str], extra_clear: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    """Invoke the hook with a clean env."""
    env = {
        # Keep PATH so python3 resolves; everything else gets stripped to
        # avoid the host's CLAUDE_* leaking in.
        "PATH": os.environ.get("PATH", ""),
    }
    for key in extra_clear or []:
        env.pop(key, None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _write_pending(pending_path: Path, messages: list[dict]) -> None:
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "updated_at": "2026-05-07T17:42:00Z",
                "pending": messages,
            }
        ),
        encoding="utf-8",
    )


def _make_msg(mid: str, body: str = "hello", thread: str = "test") -> dict:
    return {
        "id": mid,
        "thread_id": thread,
        "thread_name": thread,
        "body": body,
        "created_at": "2026-05-07T17:41:00Z",
    }


@pytest.fixture
def env(tmp_path: Path) -> dict:
    """Standard sandbox: project dir + fake HOME."""
    project_dir = tmp_path / "project"
    home = tmp_path / "home"
    project_dir.mkdir()
    home.mkdir()
    pending_path = project_dir / ".claude" / "cache" / "remotecodetrol-pending.json"
    sessions_dir = home / "Library" / "Caches" / "remotecodetrol" / "sessions"
    return {
        "CLAUDE_PROJECT_DIR": str(project_dir),
        "CLAUDE_SESSION_ID": "session-abc",
        "HOME": str(home),
        "_pending_path": pending_path,
        "_sessions_dir": sessions_dir,
        "_session_id": "session-abc",
    }


def test_no_pending_file_silent(env: dict) -> None:
    """Missing pending.json → no output, exit 0."""
    res = _run(
        {
            "CLAUDE_PROJECT_DIR": env["CLAUDE_PROJECT_DIR"],
            "CLAUDE_SESSION_ID": env["CLAUDE_SESSION_ID"],
            "HOME": env["HOME"],
        }
    )
    assert res.returncode == 0
    assert res.stdout == ""


def test_empty_pending_no_output(env: dict) -> None:
    """pending.json exists but pending list is empty → no output."""
    _write_pending(env["_pending_path"], [])
    res = _run(
        {
            "CLAUDE_PROJECT_DIR": env["CLAUDE_PROJECT_DIR"],
            "CLAUDE_SESSION_ID": env["CLAUDE_SESSION_ID"],
            "HOME": env["HOME"],
        }
    )
    assert res.returncode == 0
    assert res.stdout == ""


def test_already_shown_no_output(env: dict) -> None:
    """If all pending IDs are in cursor → no output."""
    _write_pending(env["_pending_path"], [_make_msg("BM1")])
    cursor = env["_sessions_dir"] / f"{env['_session_id']}.json"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps({"last_shown_ids": ["BM1"]}), encoding="utf-8")

    res = _run(
        {
            "CLAUDE_PROJECT_DIR": env["CLAUDE_PROJECT_DIR"],
            "CLAUDE_SESSION_ID": env["CLAUDE_SESSION_ID"],
            "HOME": env["HOME"],
        }
    )
    assert res.returncode == 0
    assert res.stdout == ""


def test_new_messages_emits_context(env: dict) -> None:
    """Pending with new IDs → additionalContext output + cursor written."""
    _write_pending(
        env["_pending_path"],
        [
            _make_msg("BM1", "Sweet, it's working!"),
            _make_msg("BM2", "second one"),
        ],
    )
    res = _run(
        {
            "CLAUDE_PROJECT_DIR": env["CLAUDE_PROJECT_DIR"],
            "CLAUDE_SESSION_ID": env["CLAUDE_SESSION_ID"],
            "HOME": env["HOME"],
        }
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout, "expected JSON output"
    payload = json.loads(res.stdout)
    assert "additionalContext" in payload
    ctx = payload["additionalContext"]
    assert "Sweet, it's working!" in ctx
    assert "second one" in ctx
    assert "[thread:test]" in ctx
    assert "ack_messages" in ctx

    # Cursor file should contain both IDs.
    cursor = env["_sessions_dir"] / f"{env['_session_id']}.json"
    assert cursor.exists()
    cursor_doc = json.loads(cursor.read_text())
    assert set(cursor_doc["last_shown_ids"]) == {"BM1", "BM2"}


def test_partially_seen_emits_only_new(env: dict) -> None:
    """One pending shown, one new → output only the new one."""
    _write_pending(
        env["_pending_path"],
        [_make_msg("BM1", "old"), _make_msg("BM2", "FRESH")],
    )
    cursor = env["_sessions_dir"] / f"{env['_session_id']}.json"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(json.dumps({"last_shown_ids": ["BM1"]}), encoding="utf-8")

    res = _run(
        {
            "CLAUDE_PROJECT_DIR": env["CLAUDE_PROJECT_DIR"],
            "CLAUDE_SESSION_ID": env["CLAUDE_SESSION_ID"],
            "HOME": env["HOME"],
        }
    )
    assert res.returncode == 0
    payload = json.loads(res.stdout)
    assert "FRESH" in payload["additionalContext"]
    assert "old" not in payload["additionalContext"]


def test_malformed_pending_no_op(env: dict) -> None:
    """Malformed JSON in pending.json → graceful no-op."""
    env["_pending_path"].parent.mkdir(parents=True, exist_ok=True)
    env["_pending_path"].write_text("{not json", encoding="utf-8")

    res = _run(
        {
            "CLAUDE_PROJECT_DIR": env["CLAUDE_PROJECT_DIR"],
            "CLAUDE_SESSION_ID": env["CLAUDE_SESSION_ID"],
            "HOME": env["HOME"],
        }
    )
    assert res.returncode == 0
    assert res.stdout == ""


def test_malformed_cursor_treated_as_empty(env: dict) -> None:
    """Cursor unreadable → treated as 'nothing shown', new IDs are emitted."""
    _write_pending(env["_pending_path"], [_make_msg("BM1")])
    cursor = env["_sessions_dir"] / f"{env['_session_id']}.json"
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text("garbage", encoding="utf-8")

    res = _run(
        {
            "CLAUDE_PROJECT_DIR": env["CLAUDE_PROJECT_DIR"],
            "CLAUDE_SESSION_ID": env["CLAUDE_SESSION_ID"],
            "HOME": env["HOME"],
        }
    )
    assert res.returncode == 0
    payload = json.loads(res.stdout)
    assert "BM1" not in payload["additionalContext"] or "hello" in payload["additionalContext"]
    # Cursor was overwritten cleanly.
    cursor_doc = json.loads(cursor.read_text())
    assert cursor_doc == {"last_shown_ids": ["BM1"]}


def test_no_session_id_silent(env: dict) -> None:
    """Without CLAUDE_SESSION_ID, hook bails silently to avoid re-injecting."""
    _write_pending(env["_pending_path"], [_make_msg("BM1")])
    res = _run(
        {
            "CLAUDE_PROJECT_DIR": env["CLAUDE_PROJECT_DIR"],
            "HOME": env["HOME"],
        },
        extra_clear=["CLAUDE_SESSION_ID"],
    )
    assert res.returncode == 0
    assert res.stdout == ""


def test_prune_old_cursor_files(env: dict, tmp_path: Path) -> None:
    """Cursor files older than 14 days are deleted on hook invocation."""
    sessions = env["_sessions_dir"]
    sessions.mkdir(parents=True, exist_ok=True)

    old_file = sessions / "ancient.json"
    fresh_file = sessions / "recent.json"
    old_file.write_text(json.dumps({"last_shown_ids": []}), encoding="utf-8")
    fresh_file.write_text(json.dumps({"last_shown_ids": []}), encoding="utf-8")

    # Backdate the old file 30 days.
    thirty_days_ago = time.time() - 30 * 24 * 60 * 60
    os.utime(old_file, (thirty_days_ago, thirty_days_ago))

    # Run with no pending — the prune still runs as housekeeping.
    res = _run(
        {
            "CLAUDE_PROJECT_DIR": env["CLAUDE_PROJECT_DIR"],
            "CLAUDE_SESSION_ID": env["CLAUDE_SESSION_ID"],
            "HOME": env["HOME"],
        }
    )
    assert res.returncode == 0
    assert not old_file.exists(), "old cursor file should have been pruned"
    assert fresh_file.exists(), "recent cursor file should remain"


def test_default_cache_path_when_no_project_dir(tmp_path: Path) -> None:
    """No CLAUDE_PROJECT_DIR → falls back to ~/Library/Caches/remotecodetrol/pending.json."""
    home = tmp_path / "home"
    home.mkdir()
    pending = home / "Library" / "Caches" / "remotecodetrol" / "pending.json"
    _write_pending(pending, [_make_msg("BM-default", "from-default-path")])

    res = _run(
        {
            "CLAUDE_SESSION_ID": "sess-1",
            "HOME": str(home),
        },
        extra_clear=["CLAUDE_PROJECT_DIR"],
    )
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert "from-default-path" in payload["additionalContext"]
