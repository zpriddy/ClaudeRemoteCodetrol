"""SSE wire-format parser tests.

Validates parsing of `event:`/`id:`/`data:`/`retry:` fields, multi-line
data reassembly, comment-line skipping (heartbeats), and chunk-boundary
robustness — chunks may split a frame anywhere.

References: WHATWG SSE spec, design doc §4.
"""

from __future__ import annotations

import pytest

from remotecodetrol_mcp.streaming import SseParser


def test_basic_event_parsed():
    p = SseParser()
    events = p.feed("event: connected\nid: abc\ndata: {\"k\":1}\n\n")
    assert len(events) == 1
    e = events[0]
    assert e.event == "connected"
    assert e.id == "abc"
    assert e.data == '{"k":1}'


def test_default_event_type_is_message():
    p = SseParser()
    events = p.feed("data: payload\n\n")
    assert events[0].event == "message"


def test_comment_line_ignored():
    p = SseParser()
    events = p.feed(":keepalive\n\n")
    # A comment + blank dispatches an *empty* frame in some readings; per spec
    # comments alone don't produce events. Our parser only dispatches when
    # something non-trivial accumulated.
    assert events == []


def test_heartbeat_between_events():
    p = SseParser()
    chunk = (
        "event: a\ndata: 1\n\n"
        ":keepalive\n\n"
        "event: b\ndata: 2\n\n"
    )
    events = p.feed(chunk)
    assert [e.event for e in events] == ["a", "b"]
    assert [e.data for e in events] == ["1", "2"]


def test_multiline_data_joined_with_newline():
    p = SseParser()
    events = p.feed("event: x\ndata: line1\ndata: line2\n\n")
    assert events[0].data == "line1\nline2"


def test_leading_space_after_colon_stripped():
    p = SseParser()
    events = p.feed("event:no_space\ndata: with_space\n\n")
    assert events[0].event == "no_space"
    assert events[0].data == "with_space"
    # Two leading spaces preserves the second.
    p2 = SseParser()
    events = p2.feed("data:  two\n\n")
    assert events[0].data == " two"


def test_chunked_arrival():
    """Frame arrives split across multiple feed() calls."""
    p = SseParser()
    parts = ["event: foo\n", "id: 42\n", "data: he", "llo\n", "\n"]
    out: list = []
    for chunk in parts:
        out.extend(p.feed(chunk))
    assert len(out) == 1
    assert out[0].event == "foo"
    assert out[0].id == "42"
    assert out[0].data == "hello"


def test_retry_field_captured():
    p = SseParser()
    events = p.feed("retry: 5000\nevent: error\ndata: oops\n\n")
    assert events[0].retry_ms == 5000


def test_invalid_retry_ignored():
    p = SseParser()
    events = p.feed("retry: not-a-number\nevent: x\ndata: y\n\n")
    assert events[0].retry_ms is None


def test_id_persists_across_frames():
    """SSE spec: `id` field is the Last-Event-ID and persists across frames
    until an `id:` line resets it. Our parser holds it across frames so the
    consumer can treat each event's `.id` as the current Last-Event-ID."""
    p = SseParser()
    events = p.feed("id: 1\ndata: a\n\nevent: x\ndata: b\n\n")
    assert events[0].id == "1"
    # 2nd frame omitted `id:`, so the persisted last-id is still 1.
    assert events[1].id == "1"


def test_crlf_line_endings():
    p = SseParser()
    events = p.feed("event: x\r\ndata: y\r\n\r\n")
    assert events[0].event == "x"
    assert events[0].data == "y"


def test_unknown_field_ignored():
    p = SseParser()
    events = p.feed("event: x\nweird: stuff\ndata: y\n\n")
    assert events[0].event == "x"
    assert events[0].data == "y"


def test_no_dispatch_until_blank_line():
    p = SseParser()
    events = p.feed("event: x\ndata: y\n")  # no terminating blank
    assert events == []
    events = p.feed("\n")
    assert len(events) == 1
