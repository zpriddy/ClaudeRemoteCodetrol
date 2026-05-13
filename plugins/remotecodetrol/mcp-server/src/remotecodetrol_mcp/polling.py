"""Polling-based message consumer (v0.6.0+, replaces SSE).

Populates the same `StreamingState` cache (`pending`, `state_change`,
`known_threads`) via short HTTP polls against
`GET /v1/threads/{tid}/messages?unackedOnly=true&format=wire` instead
of a long-lived SSE connection. v0.6.0 removed the `stream` Cloud
Function entirely — polling is the only consumer.

Why we ripped out SSE:
  * Cost: each SSE connection pinned a Cloud Run instance (the deployed
    service had `containerConcurrency: 1` despite the comment in TF
    claiming 80). At 8 avg instances × $0.025/h × 24h × 30d ≈
    $144/mo with a single active user. Polling has no per-instance
    cost — Cloud Run scales to zero between polls.
  * Reliability: under modest fan-out we tripped the
    `max_instance_count` ceiling and got 429s on every new session
    until something disconnected.
  * Architectural simplicity: one consumer instead of two means
    half the failure modes around auth/refresh/reconnect.

Design notes:
  * One poll cycle iterates every thread in `known_threads`. The set
    is small in practice (1–5 threads per active Claude session).
  * Per-thread polls are sequential, not parallel — we want to avoid
    burst-then-idle traffic patterns.
  * Backoff is interval-only — no jitter because each Claude session's
    MCP polls independently and the population is naturally
    desynchronized by session start time.
  * `wait_for_response` and `send_message(wait=True)` call
    `set_waiting(True)` for the duration so a watching Claude session
    doesn't lag 60s behind a tap.

Multi-MCP coordination (deferred):
  Today every Claude session spawns its own MCP and its own poll loop.
  At polling cost (~negligible) this is fine for solo use, but a future
  `leader.py` could use POSIX flock on the cache dir to ensure exactly
  one MCP per host runs the loop. Followers would read pending.json on
  demand (same path the hook already uses). Skipped in v0.6.0 because
  the cost case for it is weak under solo usage.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from typing import Awaitable, Callable

from .client import APIClient
from .streaming import StreamingState, _normalize_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollingPolicy:
    """How aggressively to poll. All values in seconds.

    See the docstring of `next_interval()` for the contract: this object
    is the **only** place poll cadence is decided. If you need a knob,
    add it here, not in the consumer body.

    Defaults are "Option B — cost-optimized" (per the v0.6.0 design
    discussion): minimize backend invocations at the expense of average
    reply latency. Suits solo / personal usage where you'll see Claude's
    question when you see it. Bump `interval_idle_start` down to ~15s
    if you want more responsive feeling at the cost of ~4× the polls.
    """

    # Cadence when something is pending — Claude is in checking mode and
    # the user expects fast turnaround. Floor is 1s; values much lower
    # blow past the rate-limit budget on the backend.
    interval_busy: float = 5.0
    # Initial cadence when nothing is pending. Cost-optimized: 60s is
    # the "email-cadence" floor that keeps cost well under free tier
    # even with several concurrent sessions per host.
    interval_idle_start: float = 60.0
    # Ceiling on the backoff. Once nothing has happened for a while,
    # we stretch out to here and stay until something changes.
    interval_idle_max: float = 300.0
    # Multiplier applied to the current idle interval on each empty
    # poll. 2.0 reaches max in 3 polls from start (60 → 120 → 240 → 300).
    backoff_factor: float = 2.0
    # Cadence when a tool is actively waiting on a reply (wait=True or
    # wait_for_response). The user has explicitly signaled they need
    # it now, so we tighten — but only to 2s, not 1s, to stay clear of
    # the backend rate limit on long blocking calls.
    interval_waiting: float = 2.0


def next_interval(
    policy: PollingPolicy,
    *,
    has_pending: bool,
    is_waiting: bool,
    current_idle_interval: float,
) -> float:
    """Compute the next poll delay.

    TODO(zach): tune this to taste. The defaults above target:
      - sub-2s end-to-end latency when Claude is checking + the user
        is actively replying (`has_pending=True`)
      - 30s steady-state when idle, growing to 2min if nothing happens
      - 1s when a tool call is actively blocked waiting (`is_waiting=True`)

    The math is dead-simple on purpose — three modes plus a single
    multiplicative backoff. If you want a smarter curve (e.g. step
    down to busy faster the more recent a pending event was), this is
    the function to grow.

    Returns:
        Seconds to sleep before the next poll cycle.
    """
    if is_waiting:
        return policy.interval_waiting
    if has_pending:
        return policy.interval_busy
    return min(current_idle_interval * policy.backoff_factor, policy.interval_idle_max)


class PollingConsumer:
    """SSE-equivalent that populates StreamingState via periodic HTTP polls.

    Public surface mirrors `SseConsumer` so `server.py` can swap one for
    the other without the rest of the codebase noticing:
      * `start()` — kick off the background task
      * `stop()` — cancel the task and let in-flight polls finish
      * `set_waiting(active: bool)` — toggle the tight-poll mode used
        by `wait_for_response` / `send_message(wait=True)`
    """

    def __init__(
        self,
        api: APIClient,
        streaming: StreamingState,
        policy: PollingPolicy | None = None,
        state_file_writer: Callable[[StreamingState], Awaitable[None]] | None = None,
    ) -> None:
        self.api = api
        self.streaming = streaming
        self.policy = policy or PollingPolicy()
        # When set, the writer is invoked after every poll cycle that
        # mutates pending. Mirrors SseConsumer's contract — the hook
        # reads the resulting `pending.json` to surface replies between
        # turns, so any cache mutation needs to be persisted.
        self.state_file_writer = state_file_writer
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._waiting = False
        self._current_idle_interval = self.policy.interval_idle_start

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="rcct-polling-consumer")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    def set_waiting(self, active: bool) -> None:
        """Toggle waiting mode. Called by wait_for_response /
        send_message(wait=True). Tightens cadence to ~1s for the
        duration; restores normal cadence once cleared."""
        self._waiting = active

    async def _run(self) -> None:
        """Main poll loop. Resets backoff to start-of-idle every time
        we see new pending; reaches max idle after a few empty cycles."""
        self.streaming.sse_status = "connected"  # cache surface naming kept for compat
        while not self._stop.is_set():
            try:
                had_new = await self._poll_once()
            except Exception as exc:
                # Defensive: a single bad poll must not kill the loop.
                # Wait the busy interval (short) so transient errors
                # don't compound into a 2min stall.
                logger.warning("polling cycle failed: %s", exc)
                had_new = False

            has_pending = bool(self.streaming.all_pending())
            if had_new and self.state_file_writer is not None:
                # Persist cache to pending.json so the UserPromptSubmit
                # hook (which reads cache-first) sees the new replies
                # without having to wait for the next MCP startup.
                try:
                    await self.state_file_writer(self.streaming)
                except Exception as exc:
                    logger.warning("state_file_writer failed: %s", exc)
            if had_new or has_pending:
                # Reset the backoff: as soon as something happens, drop
                # back to the start-of-idle cadence so the NEXT idle
                # window decays from 60s, not 300s.
                self._current_idle_interval = self.policy.interval_idle_start

            delay = next_interval(
                self.policy,
                has_pending=has_pending,
                is_waiting=self._waiting,
                current_idle_interval=self._current_idle_interval,
            )

            # If we just chose an idle delay, advance the backoff state
            # for next iteration (so each successive empty cycle stretches).
            if not has_pending and not self._waiting:
                self._current_idle_interval = delay

            try:
                # Sleep-or-stop, whichever comes first. Lets `stop()`
                # return inside `interval_idle_max` seconds in the worst case.
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                continue

    async def _poll_once(self) -> bool:
        """Poll every known thread for new pending. Returns True if
        any thread yielded a previously-unseen message id."""
        any_new = False
        for tid in list(self.streaming.known_threads):
            try:
                data = await self.api.get(
                    f"/threads/{tid}/messages",
                    params={"unackedOnly": "true", "format": "wire"},
                )
            except Exception as exc:
                logger.debug("poll failed for thread=%s: %s", tid, exc)
                continue
            for msg in data.get("messages", []):
                # Normalize camelCase → snake_case so the cache shape is
                # identical to what the old SSE consumer produced. Tests
                # and the hook reader both depend on snake_case keys.
                if self.streaming.add_message(_normalize_message(msg)):
                    any_new = True
        return any_new
