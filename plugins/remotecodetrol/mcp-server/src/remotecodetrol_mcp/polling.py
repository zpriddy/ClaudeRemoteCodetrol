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
import time
from dataclasses import dataclass

from typing import Awaitable, Callable

from .client import APIClient
from .leader import LeaderElector
from .streaming import StreamingState, _normalize_message


# How often a follower re-tries to acquire the leader lock. Long because
# leader handoff only happens on process death, which is rare. The cost
# of a missed poll cycle during handoff is bounded by this interval.
FOLLOWER_RETRY_S = 60.0

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
    # v0.6.1: cadence while DORMANT. The consumer enters dormant when
    # nothing has armed polling (no recent send_message(require_response),
    # peek_messages, set_thread, etc.) AND idle_disarm_after has elapsed.
    # In dormant we still poll occasionally so unsolicited replies don't
    # remain invisible forever — but ~5min spacing means a 24/7 idle MCP
    # contributes <300 reads/day. Combined with leader election this lets
    # 20 users coexist comfortably under Firestore's 50K-reads/day tier.
    interval_dormant: float = 300.0
    # v0.6.1: time without an arm()-call before the consumer slips back
    # to dormant. 2 hours matches the `loop` skill's "checking mode" stop
    # condition — when the skill stops, the MCP should also wind down.
    idle_disarm_after: float = 7200.0


def next_interval(
    policy: PollingPolicy,
    *,
    has_pending: bool,
    is_waiting: bool,
    is_armed: bool,
    current_idle_interval: float,
) -> float:
    """Compute the next poll delay.

    Decision order (highest urgency first):
      1. `is_waiting` → `interval_waiting` (a tool is actively blocked
         on a reply; tightest cadence regardless of arm state)
      2. `has_pending` → `interval_busy` (Claude is in checking mode and
         the user is currently replying; we want sub-busy latency)
      3. `not is_armed` → `interval_dormant` (nothing recently asked us
         to look; long, flat cadence purely to surface unsolicited
         replies eventually)
      4. armed + idle → `current_idle_interval × backoff_factor` capped
         at `interval_idle_max`

    The dormant branch is what gets us free-tier-sustainable at 20
    users: an MCP doing nothing actively contributes ~288 polls/day
    (one every 5 min) instead of ~1440 (one every 60 s).

    Returns:
        Seconds to sleep before the next poll cycle.
    """
    if is_waiting:
        return policy.interval_waiting
    if has_pending:
        return policy.interval_busy
    if not is_armed:
        return policy.interval_dormant
    return min(current_idle_interval * policy.backoff_factor, policy.interval_idle_max)


class PollingConsumer:
    """SSE-equivalent that populates StreamingState via periodic HTTP polls.

    State machine (v0.6.1+):

        DORMANT  ──arm()──▶  ARMED ──no-arm for idle_disarm_after──▶  DORMANT
                              │ │
                              │ └─has_pending──▶ ARMED+BUSY (5s cadence)
                              └───set_waiting(True)──▶  ARMED+WAITING (2s)

    Dormant polls every `interval_dormant` (5 min default) — enough to
    eventually surface unsolicited replies without blowing budget. Armed
    polls on the standard cadence (60 s growing to 300 s) and is the
    state during checking-mode workflows. Waiting tightens to 2 s while
    a tool is actively blocked on a reply.

    Public surface:
      * `start()` / `stop()` — lifecycle
      * `set_waiting(bool)` — for blocking tool paths
      * `arm()` — call from tools.py when activity warrants real-time
        cadence (send_message, peek, wait_for_response, set_thread)
      * `disarm()` — explicit wind-down (rarely needed; timeout handles
        it automatically)
    """

    def __init__(
        self,
        api: APIClient,
        streaming: StreamingState,
        policy: PollingPolicy | None = None,
        state_file_writer: Callable[[StreamingState], Awaitable[None]] | None = None,
        leader: LeaderElector | None = None,
    ) -> None:
        self.api = api
        self.streaming = streaming
        self.policy = policy or PollingPolicy()
        # v0.6.1: optional leader-election arbiter. When set, only the
        # process that wins the host-wide lock runs the poll loop;
        # losers loop in follower mode retrying acquisition every
        # `FOLLOWER_RETRY_S`. Without leader injected the consumer
        # polls unconditionally (back-compat with v0.6.0 and useful
        # for tests).
        self.leader = leader
        # When set, the writer is invoked after every poll cycle that
        # mutates pending. Mirrors SseConsumer's contract — the hook
        # reads the resulting `pending.json` to surface replies between
        # turns, so any cache mutation needs to be persisted.
        self.state_file_writer = state_file_writer
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._waiting = False
        self._current_idle_interval = self.policy.interval_idle_start
        # v0.6.1: armed state + timestamp of last arm call. Default is
        # dormant — we don't poll often until something asks us to. The
        # rationale: a fresh MCP that boots and never does anything
        # shouldn't be draining Firestore reads. `arm()` is cheap and
        # called from every tool that actually expects new traffic.
        self._armed = False
        self._last_arm_at: float | None = None
        # v0.6.1: wake event for arm(). The loop sleeps on _stop, _wake,
        # whichever fires first. `arm()` sets _wake so a dormant sleep
        # (up to 5 min) doesn't trap the user — first armed cycle fires
        # within ~10 ms of the arm() call.
        self._wake = asyncio.Event()

    def start(self) -> None:
        """Kick off the background task. With a leader arbiter, this
        spawns either the leader poll loop (if we win the lock) or a
        follower retry loop (if we lose). Without an arbiter, it always
        spawns the leader loop — same behavior as v0.6.0."""
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        if self.leader is None or self.leader.try_acquire():
            # No arbiter, or we won leader: run the real loop.
            self._task = asyncio.create_task(self._run(), name="rcct-polling-leader")
        else:
            # Lost the leader race. Sit in follower mode; the leader's
            # pending.json updates are what we serve from on tool calls.
            self.streaming.sse_status = "disabled"
            self._task = asyncio.create_task(
                self._follow(), name="rcct-polling-follower"
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
        if self.leader is not None:
            self.leader.release()

    async def _follow(self) -> None:
        """Follower loop: periodically retry leader acquisition. If we
        ever win, switch to the leader loop. The cost of this loop is a
        single non-blocking syscall per cycle — effectively free."""
        while not self._stop.is_set():
            await self._sleep_with_wake(FOLLOWER_RETRY_S)
            if self._stop.is_set():
                return
            if self.leader is not None and self.leader.try_acquire():
                logger.info("polling: promoted from follower to leader")
                self.streaming.sse_status = "connected"
                await self._run()
                return

    def set_waiting(self, active: bool) -> None:
        """Toggle waiting mode. Called by wait_for_response /
        send_message(wait=True). Tightens cadence to ~2s for the
        duration; restores normal cadence once cleared.

        Entering wait also implicitly arms the consumer — there's no
        sense waiting if we won't poll fast enough to see the reply.
        """
        self._waiting = active
        if active:
            self.arm()

    def arm(self) -> None:
        """Bring the consumer into ARMED state and reset the idle
        timeout. Cheap; safe to call on every tool invocation that
        expects pending traffic. Called from tools.py for:
          * `send_message(require_response=True)`
          * `peek_messages` (the user is actively checking)
          * `wait_for_response`
          * `set_thread` (the user just declared intent on a thread)

        Side effect: resets the backoff so the NEXT idle window starts
        at `interval_idle_start` (60s), not wherever the previous
        cycle had ramped to. That way the user's first arm-after-idle
        gets snappy behavior rather than 300s-later behavior.
        """
        was_dormant = not self._armed
        self._armed = True
        self._last_arm_at = time.monotonic()
        self._current_idle_interval = self.policy.interval_idle_start
        if was_dormant:
            # Interrupt any in-progress dormant sleep. The loop awaits
            # _stop OR _wake; whichever fires first ends the sleep.
            # Without this an arm() during a 300s dormant tick would
            # have to wait out the remainder of that tick before the
            # 60s-cadence cycle kicked in — bad UX for the user who
            # just said "ping me when the build's done".
            self._wake.set()

    def disarm(self) -> None:
        """Explicit wind-down to DORMANT. Almost never needed in
        practice — the idle-timeout path handles 99% of disarm cases.
        Exposed for tests and for the (rare) flow where Claude knows
        no more replies are coming (e.g. `forget_thread` on the only
        active thread)."""
        self._armed = False
        self._current_idle_interval = self.policy.interval_idle_start

    def _check_idle_disarm(self) -> None:
        """Slip back to DORMANT if no arm() in `idle_disarm_after`."""
        if not self._armed or self._last_arm_at is None:
            return
        if time.monotonic() - self._last_arm_at >= self.policy.idle_disarm_after:
            logger.info("polling: idle %ds, slipping to dormant", int(self.policy.idle_disarm_after))
            self._armed = False

    @property
    def is_armed(self) -> bool:
        """Expose for tests + diagnostics. Tools should use `arm()` /
        `disarm()` rather than mutating this directly."""
        return self._armed

    async def _run(self) -> None:
        """Main poll loop. Resets backoff to start-of-idle every time
        we see new pending; reaches max idle after a few empty cycles.
        Honors armed/dormant state — dormant polls every 5 min, armed
        polls on the standard idle cadence."""
        self.streaming.sse_status = "connected"  # cache surface naming kept for compat
        while not self._stop.is_set():
            self._check_idle_disarm()
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
                is_armed=self._armed,
                current_idle_interval=self._current_idle_interval,
            )

            # If we just chose an armed-idle delay, advance the backoff
            # state for next iteration (so each successive empty cycle
            # stretches). Skip when busy/waiting/dormant — those don't
            # use the per-cycle ramp.
            if self._armed and not has_pending and not self._waiting:
                self._current_idle_interval = delay

            # Sleep until: stop fires, wake fires (arm() while dormant),
            # or the chosen delay elapses. `_wake` is cleared at the top
            # of the next iteration so a single arm() doesn't poison
            # subsequent dormant sleeps.
            self._wake.clear()
            await self._sleep_with_wake(delay)

    async def _sleep_with_wake(self, delay: float) -> None:
        """Sleep for `delay` seconds, returning early if stop or wake
        fires. Split out for readability — the inline version had
        nested asyncio.wait_for calls and was hard to reason about."""
        stop_task = asyncio.create_task(self._stop.wait())
        wake_task = asyncio.create_task(self._wake.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_task, wake_task},
                timeout=delay,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (stop_task, wake_task):
                if not t.done():
                    t.cancel()

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
