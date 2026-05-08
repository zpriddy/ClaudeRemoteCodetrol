"""Reconnect backoff: deterministic with seeded RNG, honors server hints.

Spec §5: 1, 2, 4, 8, 16, 30s cap with ±25% jitter, or honor server's
`retry:` hint (in ms) directly with no jitter.
"""

from __future__ import annotations

import random

import pytest

from remotecodetrol_mcp.streaming import BACKOFF_MAX_S, next_backoff


def test_hint_overrides_jitter():
    # 5000ms hint → exactly 5.0s, no jitter applied.
    rng = random.Random(0)
    assert next_backoff(attempt=10, hint_ms=5_000, rng=rng) == 5.0


def test_hint_zero_means_immediate():
    rng = random.Random(0)
    assert next_backoff(attempt=3, hint_ms=0, rng=rng) == 0.0


def test_negative_hint_clamped_to_zero():
    rng = random.Random(0)
    assert next_backoff(attempt=3, hint_ms=-1000, rng=rng) == 0.0


def test_exponential_progression():
    """Without a hint, base doubles each attempt up to the 30s cap."""
    # Use a fixed-seed RNG and inspect the multiplier (uniform 0.75-1.25).
    rng = random.Random(42)
    delays = [next_backoff(attempt=i, hint_ms=None, rng=rng) for i in range(8)]

    # Expected base values per attempt (before jitter): 1, 2, 4, 8, 16, 30, 30, 30.
    expected_bases = [1, 2, 4, 8, 16, 30, 30, 30]
    for d, base in zip(delays, expected_bases):
        # Within ±25% of the base.
        assert d >= base * 0.75 - 1e-9
        assert d <= base * 1.25 + 1e-9


def test_cap_prevents_runaway():
    rng = random.Random(7)
    for attempt in range(6, 50):
        d = next_backoff(attempt=attempt, hint_ms=None, rng=rng)
        # 30s base * 1.25 max jitter.
        assert d <= BACKOFF_MAX_S * 1.25 + 1e-9


def test_seeded_sequence_is_reproducible():
    """Same seed → identical sequence. Useful for regression debugging."""
    rng_a = random.Random(123)
    rng_b = random.Random(123)
    a = [next_backoff(attempt=i, hint_ms=None, rng=rng_a) for i in range(5)]
    b = [next_backoff(attempt=i, hint_ms=None, rng=rng_b) for i in range(5)]
    assert a == b
