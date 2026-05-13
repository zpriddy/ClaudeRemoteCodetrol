"""In-memory message cache for the RemoteCodetrol MCP.

Historically (v0.3 – v0.5) this module also held the SSE consumer that
populated the cache via a long-lived `GET /v1/stream` connection. The
SSE consumer was removed in v0.6.0 in favor of a polling consumer
(`polling.py`) because each SSE connection pinned a Cloud Run instance
(containerConcurrency=1), driving cost to ~$144/mo per active user and
tripping max-instance 429s under modest fan-out.

What stayed here:
  * `StreamingState` — the cache + asyncio.Event coordination surface.
    Tools read `pending` / `state_change` / `known_threads` from this.
    Renaming was tempting but would touch every test + every importer
    for marginal clarity — the name is fine.
  * `_normalize_message` — wire-format key normalizer (camelCase →
    snake_case fallbacks).
  * `MAX_PENDING_PER_THREAD`, `FRESH_CACHE_WINDOW_S` — invariants the
    cache relies on.
  * `SseStatus` — status enum still used by tools to short-circuit
    when the consumer is `auth_failed` / `disabled` / `waiting_for_link`.
    The "Sse"-prefixed name is back-compat: the polling consumer sets
    the same values to drive the same UX decisions in tools.py.

What's gone:
  * `SseConsumer`, `SseParser`, `SseEvent`, `next_backoff`,
    `BACKOFF_MAX_S`, `IDLE_TIMEOUT_S`, `WAITING_FOR_LINK_RETRY_S`,
    sentinel exceptions. All SSE-only — see git history pre-v0.6.0
    if you ever need to resurrect them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal


logger = logging.getLogger("remotecodetrol_mcp.streaming")


# §5 (original streaming spec): cap per-thread cache so a degenerate
# user / fan-out spike doesn't OOM the MCP. Polling consumer respects
# the same cap because it reuses StreamingState.add_message.
MAX_PENDING_PER_THREAD = 200

# §5: `cache_is_fresh` window. With the polling consumer the cache is
# "fresh" as long as the consumer has polled within this window —
# tools fall through to direct API requests outside it.
FRESH_CACHE_WINDOW_S = 60.0


# Status enum the consumer publishes via `StreamingState.sse_status`.
# "Sse"-prefixed for back-compat; both consumer types (historical SSE
# and current polling) set the same values to drive identical UX
# decisions in tools.py — e.g. "skip waiting on state_change because
# the consumer can't authenticate".
SseStatus = Literal[
    "disconnected",
    "connecting",
    "connected",
    "reconnecting",
    "waiting_for_link",  # No token yet — fresh install or post-logout
    "auth_failed",  # Token revoked by server — terminal until /relink
    "disabled",
]


@dataclass
class StreamingState:
    """Shared mutable state owned by the message consumer, read by tools.

    Tools never mutate this directly — they read `pending` / `sse_status`
    and `await state_change.wait()`. Mutations go through the consumer's
    handlers (or `prune_acked` after a successful HTTP ack).
    """

    pending: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    sse_status: SseStatus = "disconnected"
    last_event_at: float = field(default_factory=time.monotonic)
    state_change: asyncio.Event = field(default_factory=asyncio.Event)
    active_thread: str | None = None
    last_event_id: str | None = None
    # v0.4.0: Per-process allowlist of threads this Claude session may see.
    # Events for threads NOT in this set are silently dropped at the
    # consumer (never cached, never surfaced via tools/hook). Initialised
    # from REMOTECODETROL_KNOWN_THREADS env var; mutated at runtime via
    # set_thread, send_message, forget_thread, and the equivalent CLI
    # subcommands. See spec §5.
    known_threads: set[str] = field(default_factory=set)
    # Set by the consumer at init time. tools.py calls `await persist_now()`
    # after a proactive cache prune (HTTP ack path) so the state file stays
    # in sync — without this, the file lags the cache until the next
    # consumer iteration delivers a `message.acked`-equivalent back.
    writer: "StateFileWriter | None" = None

    async def persist_now(self) -> None:
        """Trigger an immediate state-file write via the consumer's
        writer. Used by tools.ack_messages after proactive pruning."""
        if self.writer is None:
            return
        try:
            result = self.writer(self)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:  # defensive — disk problems must not break ack
            logger.warning("state.persist_now failed: %s", e)

    # ---- mutators (call from consumer or ack path) ----

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


def _normalize_message(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize wire-format keys to the snake_case our cache stores.

    The original SSE spec (§4.1) uses snake_case. Older backend versions
    may emit camelCase. We accept either and store snake_case canonically.
    The polling consumer's API responses go through this same function
    so the cache shape doesn't change between consumer types.
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
    # Spec 2 (v1.2.0+): reply-context + tri-state read-receipt fields.
    if "replied_to" not in out and "repliedTo" in out:
        out["replied_to"] = out["repliedTo"]
    if "mcp_acked_at" not in out and "mcpAckedAt" in out:
        out["mcp_acked_at"] = out["mcpAckedAt"]
    if "claude_acked_at" not in out and "claudeAckedAt" in out:
        out["claude_acked_at"] = out["claudeAckedAt"]
    # v0.5.0: selectable-response fields. Same camelCase→snake_case fallback
    # for back-compat with any backend version that emits camelCase.
    if "response_options" not in out and "responseOptions" in out:
        out["response_options"] = out["responseOptions"]
    if "selection_mode" not in out and "selectionMode" in out:
        out["selection_mode"] = out["selectionMode"]
    if "selected_option_ids" not in out and "selectedOptionIds" in out:
        out["selected_option_ids"] = out["selectedOptionIds"]
    return out
