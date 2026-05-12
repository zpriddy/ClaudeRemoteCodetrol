"""SSE streaming consumer for the RemoteCodetrol backend.

Maintains a long-lived `GET /v1/stream` connection (one per MCP process),
decodes Server-Sent Events into in-memory `StreamingState`, and notifies
waiting tools via an `asyncio.Event` whenever the cache mutates.

Design references: `docs/superpowers/specs/2026-05-07-mcp-streaming-relay-design.md`
sections §4 (wire protocol) and §5 (state machine + lifecycle).

The cache is a *strict mirror* of server state. We never invent entries
client-side; on every reconnect, the server's `state.snapshot` becomes the
new ground truth and we replace `pending` wholesale.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

import httpx

from .auth import AuthClient, AuthError, NotAuthorizedError
from .config import Config


logger = logging.getLogger("remotecodetrol_mcp.streaming")


# §5: cap per-thread cache so a degenerate user doesn't OOM the MCP.
MAX_PENDING_PER_THREAD = 200

# §5: cache_is_fresh window (2 × heartbeat_interval + slack).
FRESH_CACHE_WINDOW_S = 60.0

# Idle timeout: if no SSE bytes (event or heartbeat comment) arrive for this
# long, consider the connection dead and reconnect.
IDLE_TIMEOUT_S = 60.0

# Backoff schedule cap (§5).
BACKOFF_MAX_S = 30.0


SseStatus = Literal[
    "disconnected",
    "connecting",
    "connected",
    "reconnecting",
    "waiting_for_link",  # No token in keychain yet — fresh install or post-logout
    "auth_failed",  # Refresh token revoked by server — terminal until /relink
    "disabled",
]


# Backoff (in seconds) when the SSE loop is waiting for the user to link.
# Longer than connection-error backoff because we expect this to persist on
# user-time (seconds-to-minutes), not millis. Short enough that re-link is
# noticed quickly.
WAITING_FOR_LINK_RETRY_S = 10.0


@dataclass
class StreamingState:
    """Shared mutable state owned by SseConsumer, read by tools.

    Tools never mutate this directly — they read `pending` / `sse_status`
    and `await state_change.wait()`. Mutations go through SseConsumer's
    handlers (or `prune_acked` after a successful HTTP ack).
    """

    pending: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    sse_status: SseStatus = "disconnected"
    last_event_at: float = field(default_factory=time.monotonic)
    state_change: asyncio.Event = field(default_factory=asyncio.Event)
    active_thread: str | None = None
    last_event_id: str | None = None
    # v0.4.0: Per-process allowlist of threads this Claude session may see.
    # SSE events for threads NOT in this set are silently dropped at the
    # consumer (never cached, never surfaced via tools/hook). Initialised
    # from REMOTECODETROL_KNOWN_THREADS env var; mutated at runtime via
    # set_thread, send_message, forget_thread, and the equivalent CLI
    # subcommands. See spec §5.
    known_threads: set[str] = field(default_factory=set)
    # Set by SseConsumer at init time. tools.py calls `await persist_now()`
    # after a proactive cache prune (HTTP ack path) so the state file stays
    # in sync — without this, the file lags the cache until SSE delivers a
    # `message.acked` event back, which then no-ops because the cache is
    # already pruned. Net: hook re-injects already-acked messages.
    writer: "StateFileWriter | None" = None

    async def persist_now(self) -> None:
        """Trigger an immediate state-file write via the SSE consumer's
        writer. Used by tools.ack_messages after proactive pruning."""
        if self.writer is None:
            return
        try:
            result = self.writer(self)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:  # defensive — disk problems must not break ack
            logger.warning("state.persist_now failed: %s", e)

    # ---- mutators (call from SseConsumer or ack path) ----

    def replace_snapshot(self, messages: list[dict[str, Any]]) -> None:
        """Reset cache to the server's authoritative snapshot.

        Filters via `known_threads` (v0.4.0+): events for threads not in
        the allowlist are silently dropped. They still exist on the
        server; this Claude session just can't see them.
        """
        new_pending: dict[str, list[dict[str, Any]]] = {}
        for msg in messages:
            tid = msg.get("thread_id") or msg.get("threadId")
            if not tid:
                continue
            if self.known_threads and tid not in self.known_threads:
                continue
            new_pending.setdefault(tid, []).append(msg)
        # Apply the per-thread cap on snapshot too — backend should already
        # limit-to-last(200), but defense-in-depth.
        for tid, msgs in new_pending.items():
            if len(msgs) > MAX_PENDING_PER_THREAD:
                logger.warning(
                    "snapshot for thread %s had %d messages, truncating to %d",
                    tid,
                    len(msgs),
                    MAX_PENDING_PER_THREAD,
                )
                new_pending[tid] = msgs[-MAX_PENDING_PER_THREAD:]
        self.pending = new_pending
        self.bump()

    def add_message(self, msg: dict[str, Any]) -> bool:
        """Add a new message; return True if it was actually new.

        Filters via `known_threads` (v0.4.0+): events for unknown threads
        return False (silent drop). Pre-v0.4.0, all messages were accepted.
        """
        tid = msg.get("thread_id") or msg.get("threadId")
        if not tid:
            return False
        if self.known_threads and tid not in self.known_threads:
            return False
        msg_id = msg.get("id")
        bucket = self.pending.setdefault(tid, [])
        # Dedup by Firestore doc id (§5: snapshot may redeliver entries).
        if msg_id and any(existing.get("id") == msg_id for existing in bucket):
            return False
        bucket.append(msg)
        if len(bucket) > MAX_PENDING_PER_THREAD:
            dropped = len(bucket) - MAX_PENDING_PER_THREAD
            logger.warning(
                "thread %s exceeded cache cap; dropping %d oldest entries",
                tid,
                dropped,
            )
            del bucket[:dropped]
        self.bump()
        return True

    def remove_messages(self, thread_id: str, ids: list[str]) -> int:
        """Remove acked messages from the cache; return count removed."""
        bucket = self.pending.get(thread_id)
        if not bucket:
            return 0
        id_set = set(ids)
        before = len(bucket)
        bucket[:] = [m for m in bucket if m.get("id") not in id_set]
        removed = before - len(bucket)
        if not bucket:
            self.pending.pop(thread_id, None)
        if removed:
            self.bump()
        return removed

    def prune_acked(self, thread_id: str, ids: list[str]) -> int:
        """Public alias used by `ack_messages` after HTTP 2xx (§5)."""
        return self.remove_messages(thread_id, ids)

    def bump(self) -> None:
        """Notify waiters and refresh idle clock.

        Waiters MUST `clear()` the event themselves before re-awaiting
        (see tools.wait_for_response) — that way a single mutation wakes
        every coroutine currently blocked on `state_change.wait()`, and
        each waiter can re-check the cache for their thread.
        """
        self.last_event_at = time.monotonic()
        self.state_change.set()

    def cache_is_fresh(self) -> bool:
        if self.sse_status == "connected":
            return True
        return (time.monotonic() - self.last_event_at) < FRESH_CACHE_WINDOW_S

    def pending_count_by_thread(self) -> dict[str, int]:
        return {tid: len(msgs) for tid, msgs in self.pending.items()}

    def all_pending(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msgs in self.pending.values():
            out.extend(msgs)
        return out

    # ---- known_threads (v0.4.0+) ----

    def add_known_thread(self, name: str) -> bool:
        """Allow `name` to be visible. Returns True if newly added."""
        if name in self.known_threads:
            return False
        self.known_threads.add(name)
        # No state-file persistence: known_threads is in-memory by design
        # (per spec §5.1). Re-derived on each MCP launch.
        return True

    def forget_known_thread(self, name: str) -> bool:
        """Remove `name` from the allowlist; drop any cached pending for it.

        Returns True if it was previously known.
        """
        if name not in self.known_threads:
            return False
        self.known_threads.discard(name)
        if name in self.pending:
            self.pending.pop(name, None)
            self.bump()
        if self.active_thread == name:
            self.active_thread = None
        return True

    def is_thread_known(self, name: str) -> bool:
        return name in self.known_threads

    def list_known(self) -> list[str]:
        return sorted(self.known_threads)


# ---------- SSE wire parser ----------


@dataclass
class SseEvent:
    event: str
    id: str | None
    data: str
    retry_ms: int | None


class SseParser:
    """Stateful parser that reassembles SSE frames from arbitrary chunks.

    Per WHATWG SSE spec:
      - Lines are separated by `\n`, `\r`, or `\r\n`
      - Lines starting with `:` are comments (ignored, but count as bytes
        for our idle-timeout purposes)
      - `field: value` pairs accumulate into a frame
      - Multiple `data:` lines within one frame are joined with `\n`
      - A blank line dispatches the frame
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._event = ""
        self._data_lines: list[str] = []
        self._id: str | None = None
        self._retry_ms: int | None = None

    def feed(self, chunk: str) -> list[SseEvent]:
        self._buffer += chunk
        events: list[SseEvent] = []
        while True:
            # Find earliest line terminator.
            line, sep, rest = self._consume_line()
            if not sep:
                break
            self._buffer = rest
            if line == "":
                # Dispatch.
                if self._data_lines or self._event or self._id is not None:
                    events.append(SseEvent(
                        event=self._event or "message",
                        id=self._id,
                        data="\n".join(self._data_lines),
                        retry_ms=self._retry_ms,
                    ))
                self._event = ""
                self._data_lines = []
                self._retry_ms = None
                # `id` per spec persists across frames (Last-Event-ID), so we
                # do NOT reset it here. We DO record the most recent `id` on
                # each frame.
                continue
            if line.startswith(":"):
                # Comment line — ignored. (Our caller still resets the idle
                # timer on any byte read, so heartbeat comments do their job.)
                continue
            field, _, value = line.partition(":")
            # SSE: a single leading space after the colon is stripped.
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                self._event = value
            elif field == "data":
                self._data_lines.append(value)
            elif field == "id":
                self._id = value
            elif field == "retry":
                try:
                    self._retry_ms = int(value)
                except ValueError:
                    pass
            # Any other field is ignored per spec.
        return events

    def _consume_line(self) -> tuple[str, bool, str]:
        """Return (line, found_terminator, remainder)."""
        buf = self._buffer
        # Find the first of \r\n, \n, or \r.
        idx_n = buf.find("\n")
        idx_r = buf.find("\r")
        if idx_n == -1 and idx_r == -1:
            return ("", False, buf)
        if idx_n != -1 and (idx_r == -1 or idx_n < idx_r):
            return (buf[:idx_n], True, buf[idx_n + 1:])
        # \r found first; check if it's part of \r\n.
        if idx_r + 1 < len(buf) and buf[idx_r + 1] == "\n":
            return (buf[:idx_r], True, buf[idx_r + 2:])
        # The buffer ends with a bare \r — we can't be sure yet whether
        # the next byte will be \n. Treat as incomplete.
        if idx_r + 1 == len(buf):
            return ("", False, buf)
        return (buf[:idx_r], True, buf[idx_r + 1:])


# ---------- backoff ----------


def next_backoff(
    attempt: int,
    hint_ms: int | None,
    *,
    rng: random.Random | None = None,
) -> float:
    """Compute next reconnect delay (§5).

    If the server provided a `retry:` hint or `event: error` `retry` ms,
    honor it directly with no jitter. Otherwise: exponential 2^attempt
    capped at 30s, ±25% jitter.
    """
    if hint_ms is not None:
        return max(0.0, hint_ms / 1000.0)
    base = min(2 ** attempt, BACKOFF_MAX_S)
    r = rng or random
    return base * r.uniform(0.75, 1.25)


# ---------- consumer ----------


# Optional callback type: state_file_writer(state) -> None or coroutine.
StateFileWriter = Callable[[StreamingState], Awaitable[None] | None]


class SseConsumer:
    """Long-lived SSE consumer task. One per MCP process."""

    def __init__(
        self,
        config: Config,
        auth: AuthClient,
        http: httpx.AsyncClient,
        state: StreamingState,
        *,
        state_file_writer: StateFileWriter | None = None,
        rng: random.Random | None = None,
    ):
        self.config = config
        self.auth = auth
        self.http = http
        self.state = state
        self._state_file_writer = state_file_writer
        # Wire the writer onto the state so tools (e.g. ack_messages) can
        # trigger persistence directly after a proactive cache prune.
        # Without this, the state file lags the cache until SSE delivers
        # a message.acked round-trip — and then the SSE handler no-ops
        # because remove_messages returns 0 (already pruned).
        state.writer = state_file_writer
        self._rng = rng or random
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # ---- lifecycle ----

    def start(self) -> asyncio.Task[None]:
        """Spawn the consumer task. Idempotent."""
        if self._task is not None and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="rcct-sse-consumer")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def run(self) -> None:
        """Connect → consume → reconnect loop.

        Exits permanently only on `auth_failed` or explicit stop.
        """
        attempt = 0
        retry_hint_ms: int | None = None
        while not self._stop.is_set():
            self.state.sse_status = "connecting" if attempt == 0 else "reconnecting"
            try:
                await self._connect_and_consume()
                # Clean close (server hit its 60-min cap or remote end).
                # Reconnect immediately, no backoff.
                attempt = 0
                retry_hint_ms = None
                continue
            except _AuthRevokedSentinel:
                self.state.sse_status = "auth_failed"
                self.state.bump()
                logger.error("SSE auth revoked; consumer exiting permanently")
                return
            except _NoTokenSentinel:
                # No token in keychain yet — user hasn't linked, or token
                # was deleted. Don't exit; wait for /remotecodetrol:link to
                # write a fresh token and try again. This is the
                # double-restart-fix: in v0.3.2 and earlier, this case was
                # conflated with auth_revoked and the consumer exited
                # permanently, requiring a Claude Code restart after link.
                self.state.sse_status = "waiting_for_link"
                self.state.bump()
                # Reset attempt count — we're not in an exponential-backoff
                # connection-failure mode; we're idle waiting for an
                # external trigger.
                attempt = 0
                retry_hint_ms = None
                logger.info(
                    "SSE: no token; waiting %.0fs for link",
                    WAITING_FOR_LINK_RETRY_S,
                )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=WAITING_FOR_LINK_RETRY_S
                    )
                    return  # _stop fired during sleep
                except asyncio.TimeoutError:
                    pass
                continue
            except _RetryHintSentinel as e:
                retry_hint_ms = e.retry_ms
                attempt += 1
            except (httpx.HTTPError, OSError, asyncio.IncompleteReadError) as e:
                logger.info("SSE connection error: %s", e)
                attempt += 1
            except asyncio.CancelledError:
                self.state.sse_status = "disconnected"
                raise
            except Exception as e:  # defensive — never let consumer die silently
                logger.exception("Unexpected SSE consumer error: %s", e)
                attempt += 1

            if self._stop.is_set():
                return
            self.state.sse_status = "reconnecting"
            delay = next_backoff(attempt, retry_hint_ms, rng=self._rng)
            retry_hint_ms = None
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # _stop fired during sleep
            except asyncio.TimeoutError:
                pass

    async def _connect_and_consume(self) -> None:
        # Use config.stream_url, NOT api_v1 — the stream endpoint is a
        # separate Cloud Function (e.g. /stream), not a route under the api
        # function (which would be /api/v1/stream). See config._derive_stream_url
        # for the derivation rule and the REMOTECODETROL_STREAM_URL override.
        url = self.config.stream_url
        try:
            token = await self.auth.get_access_token()
        except NotAuthorizedError as e:
            # No token in keychain (fresh install / post-logout). NOT
            # terminal: the user might run /remotecodetrol:link in this
            # same session, after which the next retry will succeed.
            # Distinguished from AuthError below to avoid the
            # double-restart bug from v0.3.2 — see run()'s
            # _NoTokenSentinel handler.
            logger.info("SSE: no token in keychain (%s); waiting for link", e)
            raise _NoTokenSentinel()
        except AuthError as e:
            # Refresh token rejected by the backend. Terminal — the user
            # needs to explicitly relink (the existing refresh token is
            # invalid and we have no way to recover in-process).
            logger.warning("SSE: refresh token revoked (%s); pausing consumer", e)
            self.state.sse_status = "auth_failed"
            self.state.bump()
            raise _AuthRevokedSentinel()

        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        if self.state.last_event_id:
            headers["Last-Event-ID"] = self.state.last_event_id

        # Use a long-lived stream; httpx supports text/event-stream via
        # `stream("GET", ...)` returning a Response we can iterate.
        async with self.http.stream(
            "GET",
            url,
            headers=headers,
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
        ) as resp:
            if resp.status_code == 401:
                # Try one refresh; if still 401, treat as revoked.
                self.auth.invalidate()
                logger.info("SSE: 401 on connect; will retry after refresh")
                # Force a reconnect (handled by outer loop). Treating as a
                # transient connect failure rather than a hard auth_failed:
                # the next attempt will go through the refresh path in
                # AuthClient.get_access_token. If THAT fails too, we'll get
                # NotAuthorizedError and exit via the sentinel.
                raise httpx.HTTPError("auth refresh required")
            if resp.status_code == 404:
                # Old backend without /v1/stream — sleep & retry occasionally.
                logger.info("SSE: backend lacks /v1/stream (404); degraded mode")
                self.state.sse_status = "disconnected"
                self.state.bump()
                # Treat like a transient error: long-ish backoff hint so we
                # don't hammer.
                raise _RetryHintSentinel(60_000)
            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after")
                ms = None
                if retry_after:
                    try:
                        ms = int(float(retry_after) * 1000)
                    except ValueError:
                        pass
                raise _RetryHintSentinel(ms or 5_000)
            if resp.status_code >= 400:
                logger.info("SSE: HTTP %d on connect", resp.status_code)
                raise httpx.HTTPError(f"HTTP {resp.status_code}")

            self.state.sse_status = "connected"
            self.state.last_event_at = time.monotonic()
            self.state.bump()

            parser = SseParser()
            async for chunk in self._iter_text_with_idle_timeout(resp):
                self.state.last_event_at = time.monotonic()
                events = parser.feed(chunk)
                for evt in events:
                    if evt.id is not None:
                        self.state.last_event_id = evt.id
                    if evt.retry_ms is not None:
                        # Stash hint for our outer-loop reconnect timing.
                        # We don't reconnect on a `retry:` field alone — it's
                        # a hint for *if/when* we reconnect.
                        pass
                    await self._dispatch(evt)

    async def _iter_text_with_idle_timeout(
        self, resp: httpx.Response
    ):
        """Yield text chunks; raise OSError on idle timeout.

        We can't use httpx's built-in read timeout because we want a
        "no bytes for N seconds" semantic, not "any single read takes N".
        """
        aiter = resp.aiter_text().__aiter__()
        while True:
            try:
                chunk = await asyncio.wait_for(aiter.__anext__(), timeout=IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.info("SSE: idle timeout (%.0fs); reconnecting", IDLE_TIMEOUT_S)
                raise OSError("sse idle timeout")
            except StopAsyncIteration:
                return
            if chunk:
                yield chunk

    # ---- event handlers ----

    async def _dispatch(self, evt: SseEvent) -> None:
        try:
            payload = json.loads(evt.data) if evt.data else {}
        except json.JSONDecodeError as e:
            logger.warning("SSE: undecodable data (%s): %r", e, evt.data[:200])
            return

        if evt.event == "connected":
            # Per-connection welcome; just log for breadcrumbs.
            logger.debug("SSE connected: %s", payload)
            return
        if evt.event == "state.snapshot":
            messages = payload.get("pending", []) or []
            self.state.replace_snapshot(list(messages))
            await self._persist()
            return
        if evt.event == "message.created":
            # Body is the Message object directly.
            if isinstance(payload, dict):
                # Server may use camelCase; normalize the keys we care about.
                msg = _normalize_message(payload)
                if self.state.add_message(msg):
                    await self._persist()
            return
        if evt.event == "message.acked":
            tid = payload.get("thread_id") or payload.get("threadId")
            ids = payload.get("message_ids") or payload.get("messageIds") or []
            if tid and ids:
                if self.state.remove_messages(tid, list(ids)):
                    await self._persist()
            return
        if evt.event == "error":
            code = payload.get("code", "")
            retry_ms = payload.get("retry")
            logger.info("SSE: server error code=%s retry=%s", code, retry_ms)
            if code == "auth_revoked":
                raise _AuthRevokedSentinel()
            raise _RetryHintSentinel(retry_ms if isinstance(retry_ms, int) else None)
        # Unknown event types: log & ignore.
        logger.debug("SSE: ignoring unknown event %r", evt.event)

    async def _persist(self) -> None:
        if self._state_file_writer is None:
            return
        try:
            result = self._state_file_writer(self.state)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:  # defensive — disk problems must not kill SSE
            logger.warning("state-file write failed: %s", e)


def _normalize_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize wire-format keys to the snake_case our cache stores.

    The spec (§4.1) uses snake_case. Older backends may emit camelCase. We
    accept either and store snake_case canonically.
    """
    out = dict(payload)
    if "thread_id" not in out and "threadId" in out:
        out["thread_id"] = out["threadId"]
    if "thread_name" not in out and "threadName" in out:
        out["thread_name"] = out["threadName"]
    if "sender_type" not in out and "senderType" in out:
        out["sender_type"] = out["senderType"]
    if "sender_id" not in out and "senderId" in out:
        out["sender_id"] = out["senderId"]
    if "created_at" not in out and "createdAt" in out:
        out["created_at"] = out["createdAt"]
    if "require_response" not in out and "requireResponse" in out:
        out["require_response"] = out["requireResponse"]
    if "idempotency_key" not in out and "idempotencyKey" in out:
        out["idempotency_key"] = out["idempotencyKey"]
    return out


# ---------- internal sentinels ----------


class _AuthRevokedSentinel(Exception):
    """Tell the run loop to exit permanently — token can't be recovered."""


class _NoTokenSentinel(Exception):
    """Tell the run loop to wait for an out-of-process /link to write a token.

    Distinguished from `_AuthRevokedSentinel` because they need different
    handling: revoked refresh-token is terminal (the consumer has no way
    to recover without explicit user action), but "no token yet" is
    expected on fresh installs and clears as soon as `/link` completes.
    """


class _RetryHintSentinel(Exception):
    """Tell the run loop to honor a server-provided retry hint (ms)."""

    def __init__(self, retry_ms: int | None) -> None:
        super().__init__(f"retry={retry_ms}")
        self.retry_ms = retry_ms
