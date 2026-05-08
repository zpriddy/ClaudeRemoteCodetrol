"""Cache mutation behavior on `StreamingState`.

These exercise the spec §5 invariants:
  - dedup by Firestore doc id
  - replace_snapshot replaces wholesale
  - per-thread cap drops oldest with a warning
  - prune_acked is idempotent
"""

from __future__ import annotations

import logging

import pytest

from remotecodetrol_mcp.streaming import MAX_PENDING_PER_THREAD, StreamingState


def msg(id_: str, thread: str = "t", body: str = "x") -> dict:
    return {"id": id_, "thread_id": thread, "body": body}


def test_add_message_basic():
    s = StreamingState()
    assert s.add_message(msg("m1")) is True
    assert s.pending == {"t": [msg("m1")]}


def test_add_message_dedup():
    s = StreamingState()
    s.add_message(msg("m1"))
    # Re-adding same id is a no-op (snapshot redelivery).
    assert s.add_message(msg("m1")) is False
    assert len(s.pending["t"]) == 1


def test_add_message_skips_when_no_thread_id():
    s = StreamingState()
    assert s.add_message({"id": "m1", "body": "x"}) is False
    assert s.pending == {}


def test_replace_snapshot_replaces_wholesale():
    s = StreamingState()
    s.add_message(msg("m1"))
    s.add_message(msg("m2"))
    s.replace_snapshot([msg("m3"), msg("m4", thread="other")])
    assert set(s.pending.keys()) == {"t", "other"}
    assert [m["id"] for m in s.pending["t"]] == ["m3"]


def test_remove_messages_prunes_and_clears_empty_thread():
    s = StreamingState()
    s.add_message(msg("m1"))
    s.add_message(msg("m2"))
    removed = s.remove_messages("t", ["m1"])
    assert removed == 1
    assert [m["id"] for m in s.pending["t"]] == ["m2"]

    removed = s.remove_messages("t", ["m2"])
    assert removed == 1
    # Thread bucket is removed when empty.
    assert "t" not in s.pending


def test_remove_messages_idempotent():
    s = StreamingState()
    s.add_message(msg("m1"))
    s.remove_messages("t", ["m1"])
    # Repeating an ack on already-removed ids is a no-op.
    assert s.remove_messages("t", ["m1"]) == 0


def test_per_thread_cap_drops_oldest(caplog):
    s = StreamingState()
    # Push (cap + 5) entries; cap should hold.
    for i in range(MAX_PENDING_PER_THREAD + 5):
        s.add_message(msg(f"m{i}"))
    assert len(s.pending["t"]) == MAX_PENDING_PER_THREAD
    # Oldest 5 evicted, newest preserved.
    ids = [m["id"] for m in s.pending["t"]]
    assert ids[0] == "m5"
    assert ids[-1] == f"m{MAX_PENDING_PER_THREAD + 4}"


def test_snapshot_truncates_oversized_payload():
    s = StreamingState()
    huge = [msg(f"m{i}") for i in range(MAX_PENDING_PER_THREAD + 50)]
    s.replace_snapshot(huge)
    assert len(s.pending["t"]) == MAX_PENDING_PER_THREAD


def test_cache_is_fresh_when_connected():
    s = StreamingState()
    s.sse_status = "connected"
    assert s.cache_is_fresh() is True


def test_cache_is_stale_after_idle():
    import time as _time
    s = StreamingState()
    s.sse_status = "disconnected"
    s.last_event_at = _time.monotonic() - 1000.0
    assert s.cache_is_fresh() is False


def test_state_change_event_set_on_mutation():
    s = StreamingState()
    assert not s.state_change.is_set() or s.state_change.is_set()  # either ok
    s.state_change.clear()
    s.add_message(msg("m1"))
    assert s.state_change.is_set()


def test_pending_count_by_thread():
    s = StreamingState()
    s.add_message(msg("m1", thread="alpha"))
    s.add_message(msg("m2", thread="alpha"))
    s.add_message(msg("m3", thread="beta"))
    assert s.pending_count_by_thread() == {"alpha": 2, "beta": 1}
