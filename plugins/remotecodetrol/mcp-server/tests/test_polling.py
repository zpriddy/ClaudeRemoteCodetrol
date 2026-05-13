"""v0.6.0 polling consumer — next_interval algorithm + consumer state machine."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from remotecodetrol_mcp.auth import AuthClient, TokenStore
from remotecodetrol_mcp.client import APIClient
from remotecodetrol_mcp.polling import (
    PollingConsumer,
    PollingPolicy,
    next_interval,
)
from remotecodetrol_mcp.streaming import StreamingState


# ---------- next_interval: pure function tests ----------


def _ni(policy, *, has_pending=False, is_waiting=False, is_armed=True, current=60.0):
    """Test convenience wrapper — keeps test bodies readable when most
    calls share the same default kwargs."""
    return next_interval(
        policy,
        has_pending=has_pending,
        is_waiting=is_waiting,
        is_armed=is_armed,
        current_idle_interval=current,
    )


def test_next_interval_waiting_overrides_everything():
    """When a tool is actively blocked (wait=True), use waiting cadence
    even if there's pending or we'd otherwise be dormant. Caller has
    signaled max urgency."""
    p = PollingPolicy(interval_busy=5.0, interval_waiting=2.0)
    assert _ni(p, has_pending=True, is_waiting=True) == 2.0
    assert _ni(p, has_pending=False, is_waiting=True, current=300.0) == 2.0
    # Even dormant + waiting tightens to waiting cadence.
    assert _ni(p, is_armed=False, is_waiting=True) == 2.0


def test_next_interval_busy_when_pending():
    p = PollingPolicy(interval_busy=5.0, interval_idle_start=60.0)
    assert _ni(p, has_pending=True) == 5.0


def test_next_interval_dormant_uses_long_cadence():
    """v0.6.1: when not armed (and not pending/waiting), we use the
    interval_dormant ceiling — much longer than armed-idle. Without
    this branch we'd be stuck at the 60s armed-idle cadence forever
    on an MCP that has nothing to do, eating Firestore reads."""
    p = PollingPolicy(interval_dormant=300.0, interval_idle_start=60.0)
    # current_idle_interval is ignored when dormant — the policy uses
    # the flat dormant value regardless of where the backoff was.
    assert _ni(p, is_armed=False) == 300.0
    assert _ni(p, is_armed=False, current=120.0) == 300.0


def test_next_interval_idle_grows_by_backoff_factor():
    """Empty polls multiply the current interval until the ceiling.
    Only applies in the armed state — dormant ignores backoff."""
    p = PollingPolicy(
        interval_idle_start=60.0, interval_idle_max=300.0, backoff_factor=2.0
    )
    # 60 → 120 → 240 → 300 (capped)
    assert _ni(p, current=60.0) == 120.0
    assert _ni(p, current=120.0) == 240.0
    assert _ni(p, current=240.0) == 300.0
    # Already at ceiling — stays put.
    assert _ni(p, current=300.0) == 300.0


def test_next_interval_option_b_defaults():
    """Pin the v0.6.1 cost-optimized defaults so a future tuning
    accident gets caught."""
    p = PollingPolicy()
    assert p.interval_busy == 5.0
    assert p.interval_idle_start == 60.0
    assert p.interval_idle_max == 300.0
    assert p.backoff_factor == 2.0
    assert p.interval_waiting == 2.0
    assert p.interval_dormant == 300.0
    assert p.idle_disarm_after == 7200.0  # 2h


# ---------- PollingConsumer: integration with the mock backend ----------


def _make_api(handler, tmp_path) -> tuple[APIClient, httpx.AsyncClient]:
    import time as _time

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    store = TokenStore(path=tmp_path / "tokens.json")
    store.set_active_email("user@example.com")
    store.store_token(
        email="user@example.com",
        token="valid-tok",
        expires_at=_time.time() + 14 * 24 * 60 * 60,
        rotates_at=_time.time() + 7 * 24 * 60 * 60,
    )
    from remotecodetrol_mcp.config import Config

    config = Config(
        api_base="https://api.test.invalid",
        stream_url="https://stream.test.invalid",
        default_thread=None,
        device_label="test",
        default_poll_interval_seconds=1,
        default_timeout_minutes=1,
        mcp_token_ttl_sec=14 * 24 * 60 * 60,
        mcp_token_rotate_after_sec=7 * 24 * 60 * 60,
        known_threads_seed=(),
    )
    auth = AuthClient(config, http, store)
    return APIClient(config, auth, http), http


@pytest.mark.asyncio
async def test_polling_consumer_populates_cache_from_api(tmp_path, monkeypatch):
    """One full poll cycle fetches messages for every known thread and
    deposits them into the cache."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if "/threads/work/messages" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"id": "m1", "thread_id": "work", "body": "hi"},
                    ],
                    "cursor": None,
                },
            )
        return httpx.Response(200, json={"messages": [], "cursor": None})

    api, http = _make_api(handler, tmp_path)
    state = StreamingState()
    state.add_known_thread("work")
    state.add_known_thread("other")

    consumer = PollingConsumer(
        api, state, policy=PollingPolicy(interval_idle_start=0.05, interval_idle_max=0.1)
    )
    try:
        consumer.start()
        # Give it enough wall-clock for one full cycle. The first cycle
        # always runs immediately on start().
        await asyncio.sleep(0.2)
        # `work` should now be in pending; `other` returned empty.
        assert "work" in state.pending
        assert state.pending["work"][0]["id"] == "m1"
        # Both threads were polled.
        assert any("/threads/work/messages" in p for p in seen_paths)
        assert any("/threads/other/messages" in p for p in seen_paths)
    finally:
        await consumer.stop()
        await http.aclose()


@pytest.mark.asyncio
async def test_polling_consumer_set_waiting_tightens_cadence(tmp_path):
    """set_waiting(True) should drop the next sleep to interval_waiting.
    We verify by counting polls in a fixed window."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        poll_count += 1
        return httpx.Response(200, json={"messages": [], "cursor": None})

    api, http = _make_api(handler, tmp_path)
    state = StreamingState()
    state.add_known_thread("t")

    # Idle = 1s (slow), waiting = 50ms (fast). In 250ms we expect ≥3
    # polls in waiting mode vs ≤1 in idle.
    policy = PollingPolicy(
        interval_idle_start=1.0,
        interval_idle_max=1.0,
        interval_waiting=0.05,
        backoff_factor=1.0,  # don't grow during this test
    )
    consumer = PollingConsumer(api, state, policy=policy)
    try:
        consumer.set_waiting(True)
        consumer.start()
        await asyncio.sleep(0.25)
        assert poll_count >= 3, f"expected ≥3 polls in waiting mode, got {poll_count}"
    finally:
        await consumer.stop()
        await http.aclose()


@pytest.mark.asyncio
async def test_arm_promotes_from_dormant_and_wakes_loop(tmp_path):
    """v0.6.1: arm() while dormant should both flip state AND fire the
    wake event so any in-progress dormant sleep cuts short. Without the
    wake, an `arm()` during a 5min dormant tick would have to wait out
    the remainder of that tick before doing anything useful."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        poll_count += 1
        return httpx.Response(200, json={"messages": [], "cursor": None})

    api, http = _make_api(handler, tmp_path)
    state = StreamingState()
    state.add_known_thread("t")

    # Dormant = 10s (long enough to NOT fire naturally during test).
    # Armed-idle = 0.05s. If arm() correctly wakes, we should see
    # multiple polls in 200ms. Without wake, we'd only see the first.
    policy = PollingPolicy(
        interval_dormant=10.0,
        interval_idle_start=0.05,
        interval_idle_max=0.05,
        backoff_factor=1.0,
    )
    consumer = PollingConsumer(api, state, policy=policy)
    try:
        consumer.start()
        # Let the first poll happen then enter the long dormant sleep.
        await asyncio.sleep(0.05)
        count_before_arm = poll_count
        # Arm — should wake the dormant sleep.
        consumer.arm()
        assert consumer.is_armed
        await asyncio.sleep(0.2)
        # After arm we should have several more polls (the 50ms armed
        # cadence kicking in). Without wake, we'd be stuck at ~count_before_arm.
        assert poll_count > count_before_arm + 1, (
            f"arm() did not wake the loop: count went {count_before_arm} → {poll_count}"
        )
    finally:
        await consumer.stop()
        await http.aclose()


@pytest.mark.asyncio
async def test_set_waiting_implicitly_arms(tmp_path):
    """Entering wait mode without first arming should still trigger the
    armed cadence — there's no sense waiting if we won't poll fast
    enough to see the reply."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    api, http = _make_api(
        lambda r: httpx.Response(200, json={"messages": [], "cursor": None}),
        tmp_path,
    )
    state = StreamingState()
    state.add_known_thread("t")
    consumer = PollingConsumer(api, state, policy=PollingPolicy())
    try:
        assert not consumer.is_armed
        consumer.set_waiting(True)
        assert consumer.is_armed, "set_waiting(True) should imply arm()"
        consumer.set_waiting(False)
        # set_waiting(False) does NOT disarm — the user's interest in
        # this thread persists past the blocking call.
        assert consumer.is_armed
    finally:
        await consumer.stop()
        await http.aclose()


def test_disarm_resets_idle_interval():
    """Explicit disarm should snap us back to the start-of-idle so the
    next arm() doesn't inherit a stale stretched cadence."""
    api = None  # disarm doesn't touch the network
    state = StreamingState()
    consumer = PollingConsumer.__new__(PollingConsumer)
    # Construct just enough of the instance for disarm() — avoiding
    # __init__ keeps the test focused on the state machine.
    consumer.policy = PollingPolicy()
    consumer._armed = True
    consumer._current_idle_interval = 240.0  # mid-backoff

    consumer.disarm()

    assert not consumer.is_armed
    assert consumer._current_idle_interval == consumer.policy.interval_idle_start


def test_check_idle_disarm_slips_to_dormant():
    """After `idle_disarm_after` seconds without an arm() call, the
    next loop iteration slips back to dormant."""
    import time as _time

    consumer = PollingConsumer.__new__(PollingConsumer)
    consumer.policy = PollingPolicy(idle_disarm_after=0.0)  # immediate
    consumer._armed = True
    consumer._last_arm_at = _time.monotonic() - 1.0  # past timeout

    consumer._check_idle_disarm()

    assert not consumer.is_armed


# ---------- Leader election ----------


def test_leader_acquire_solo(tmp_path):
    """A single LeaderElector should always acquire successfully."""
    from remotecodetrol_mcp.leader import LeaderElector

    elector = LeaderElector(lockfile_path=tmp_path / "poll.lock")
    try:
        assert elector.try_acquire()
        assert elector.is_leader
        # Idempotent — calling again returns True without re-locking.
        assert elector.try_acquire()
    finally:
        elector.release()


def test_leader_second_acquire_blocked(tmp_path):
    """A second elector pointing at the same path should fail to
    acquire while the first holds the lock. This is the load-bearing
    invariant: it's what makes "only one MCP per host polls" work."""
    from remotecodetrol_mcp.leader import LeaderElector

    lockfile = tmp_path / "poll.lock"
    a = LeaderElector(lockfile_path=lockfile)
    b = LeaderElector(lockfile_path=lockfile)
    try:
        assert a.try_acquire()
        assert not b.try_acquire(), "second elector should fail while first holds"
        assert not b.is_leader
        # After A releases, B can take over.
        a.release()
        assert b.try_acquire()
        assert b.is_leader
    finally:
        a.release()
        b.release()


def test_leader_release_idempotent(tmp_path):
    """release() on a non-leader should be a safe no-op (cleanup paths
    call it unconditionally)."""
    from remotecodetrol_mcp.leader import LeaderElector

    elector = LeaderElector(lockfile_path=tmp_path / "poll.lock")
    # Never acquired — release should be silent no-op.
    elector.release()
    assert not elector.is_leader


@pytest.mark.asyncio
async def test_polling_follower_does_not_hit_backend(tmp_path):
    """When two PollingConsumers share a LeaderElector path, the second
    one should go into follower mode and make ZERO backend calls. This
    is the cost win the leader election is buying us."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    from remotecodetrol_mcp.leader import LeaderElector

    lockfile = tmp_path / "poll.lock"

    leader_count = 0
    follower_count = 0

    def leader_handler(request: httpx.Request) -> httpx.Response:
        nonlocal leader_count
        leader_count += 1
        return httpx.Response(200, json={"messages": [], "cursor": None})

    def follower_handler(request: httpx.Request) -> httpx.Response:
        nonlocal follower_count
        follower_count += 1
        return httpx.Response(200, json={"messages": [], "cursor": None})

    leader_api, leader_http = _make_api(leader_handler, tmp_path / "leader")
    follower_api, follower_http = _make_api(follower_handler, tmp_path / "follower")

    leader_state = StreamingState()
    leader_state.add_known_thread("t")
    follower_state = StreamingState()
    follower_state.add_known_thread("t")

    # Fast cadence so the leader actually polls during the test window.
    policy = PollingPolicy(
        interval_idle_start=0.05, interval_idle_max=0.05, backoff_factor=1.0
    )

    leader_consumer = PollingConsumer(
        leader_api,
        leader_state,
        policy=policy,
        leader=LeaderElector(lockfile_path=lockfile),
    )
    leader_consumer.arm()  # so it polls at armed cadence not dormant
    leader_consumer.start()
    await asyncio.sleep(0.05)  # let leader win the race

    follower_consumer = PollingConsumer(
        follower_api,
        follower_state,
        policy=policy,
        leader=LeaderElector(lockfile_path=lockfile),
    )
    follower_consumer.arm()
    follower_consumer.start()

    try:
        await asyncio.sleep(0.2)
        # The leader should have polled multiple times.
        assert leader_count >= 2, f"leader didn't poll: {leader_count}"
        # The follower should have made ZERO API calls — it's parked
        # in the FOLLOWER_RETRY loop, not polling.
        assert follower_count == 0, (
            f"follower made backend calls (should be 0): {follower_count}"
        )
    finally:
        await follower_consumer.stop()
        await leader_consumer.stop()
        await leader_http.aclose()
        await follower_http.aclose()


@pytest.mark.asyncio
async def test_polling_consumer_normalizes_camelcase(tmp_path):
    """Backend-emitted camelCase (responseOptions, repliedTo, etc.)
    must land in the cache as snake_case so the rest of the code
    doesn't have to handle both shapes."""
    import remotecodetrol_mcp.state as state_mod
    state_mod.STATE_PATH = tmp_path / "state.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "id": "m1",
                        "threadId": "t",
                        "senderType": "claude",
                        "body": "x",
                        "responseOptions": [{"id": "a", "label": "A"}],
                        "selectionMode": "single",
                    }
                ],
                "cursor": None,
            },
        )

    api, http = _make_api(handler, tmp_path)
    state = StreamingState()
    state.add_known_thread("t")
    consumer = PollingConsumer(
        api, state, policy=PollingPolicy(interval_idle_start=0.05)
    )
    try:
        consumer.start()
        await asyncio.sleep(0.15)
        cached = state.pending.get("t", [])
        assert cached, "expected the message in cache"
        msg = cached[0]
        # snake_case keys present after normalization
        assert msg["thread_id"] == "t"
        assert msg["sender_type"] == "claude"
        assert msg["response_options"] == [{"id": "a", "label": "A"}]
        assert msg["selection_mode"] == "single"
    finally:
        await consumer.stop()
        await http.aclose()
