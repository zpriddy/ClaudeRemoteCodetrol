"""Atomic state-file writer behavior.

Spec §6: schema_version 1, ISO updated_at, list of pending messages with
preview. Atomic via tempfile + os.replace so the hook never observes a
partial JSON document.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from remotecodetrol_mcp.state_file import (
    SCHEMA_VERSION,
    state_file_path,
    write_state_file,
)
from remotecodetrol_mcp.streaming import StreamingState


def test_writes_expected_shape(tmp_path: Path):
    s = StreamingState()
    s.add_message({
        "id": "m1",
        "thread_id": "t",
        "thread_name": "test",
        "body": "hello there",
        "created_at": "2026-05-05T12:00:00Z",
    })
    target = tmp_path / "pending.json"
    write_state_file(s, path=target)

    raw = json.loads(target.read_text())
    assert raw["schema_version"] == SCHEMA_VERSION
    assert "updated_at" in raw
    assert raw["pending"][0]["id"] == "m1"
    assert raw["pending"][0]["thread_name"] == "test"
    assert raw["pending"][0]["preview"] == "hello there"


def test_long_body_truncated_in_preview(tmp_path: Path):
    s = StreamingState()
    s.add_message({"id": "m1", "thread_id": "t", "body": "x" * 250})
    target = tmp_path / "pending.json"
    write_state_file(s, path=target)
    payload = json.loads(target.read_text())
    preview = payload["pending"][0]["preview"]
    # Truncated and ellipsis-suffixed.
    assert len(preview) <= 101  # 100 + ellipsis
    assert preview.endswith("…")


def test_pending_sorted_by_created_at(tmp_path: Path):
    s = StreamingState()
    s.add_message({"id": "b", "thread_id": "t", "body": "B", "created_at": "2026-01-02T00:00:00Z"})
    s.add_message({"id": "a", "thread_id": "t", "body": "A", "created_at": "2026-01-01T00:00:00Z"})
    s.add_message({"id": "c", "thread_id": "t", "body": "C", "created_at": "2026-01-03T00:00:00Z"})
    target = tmp_path / "pending.json"
    write_state_file(s, path=target)
    payload = json.loads(target.read_text())
    assert [m["id"] for m in payload["pending"]] == ["a", "b", "c"]


def test_atomic_concurrent_reads_never_partial(tmp_path: Path):
    """Reader running in parallel never sees a malformed JSON file."""
    target = tmp_path / "pending.json"

    s = StreamingState()
    for i in range(50):
        s.add_message({"id": f"m{i}", "thread_id": "t", "body": "x" * 5_000})

    # Seed an initial valid file so the reader has something to find.
    write_state_file(s, path=target)

    stop = threading.Event()
    errors: list[Exception] = []

    def writer() -> None:
        for _ in range(50):
            if stop.is_set():
                return
            try:
                write_state_file(s, path=target)
            except Exception as e:
                errors.append(e)

    def reader() -> None:
        for _ in range(200):
            if stop.is_set():
                return
            try:
                content = target.read_text()
                if not content:
                    continue
                json.loads(content)
            except json.JSONDecodeError as e:  # critical failure
                errors.append(e)
            except FileNotFoundError:
                pass

    threads = [
        threading.Thread(target=writer),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    stop.set()
    assert not errors, f"observed {errors!r}"


def test_state_file_path_uses_project_dir_when_set(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    p = state_file_path()
    assert p == tmp_path / ".claude" / "cache" / "remotecodetrol-pending.json"


def test_state_file_path_falls_back_to_cache(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    p = state_file_path()
    # Either the macOS or Linux fallback — both end with the canonical filename.
    assert p.name == "pending.json"
    assert "remotecodetrol" in str(p)


def test_creates_parent_directory(tmp_path: Path):
    target = tmp_path / "deeply" / "nested" / "pending.json"
    s = StreamingState()
    write_state_file(s, path=target)
    assert target.exists()
    assert json.loads(target.read_text())["pending"] == []
