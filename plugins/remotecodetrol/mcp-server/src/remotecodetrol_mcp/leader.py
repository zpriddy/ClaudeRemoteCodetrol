"""POSIX flock-based leader election for per-host polling (v0.6.1).

The problem: Claude Code spawns one MCP per session (stdio JSON-RPC), so
a user with N concurrent terminals has N MCPs — each running its own
polling consumer. At 20 users × 3 sessions = 60 polling consumers, which
either blows the Firestore free-tier read budget or forces a very slow
cadence.

The fix: every MCP runs `LeaderElector.try_acquire()` at startup. Exactly
one wins (POSIX `flock(LOCK_EX | LOCK_NB)`); the rest get `BlockingIOError`
and become followers that skip the poll loop entirely. The OS auto-
releases the flock when the leader process dies, so a follower can take
over after a `try_acquire()` retry — no heartbeat / liveness scheme
needed, no zombie-leader window.

What followers do for `peek_messages`:
  * Their own `streaming.pending` is empty (they're not polling).
  * The `peek_messages` tool falls back to reading the leader's
    `pending.json` from disk (same file the UserPromptSubmit hook
    already consumes). This is a sub-ms filesystem read.
  * `send_message` and `ack_messages` write through normally — they're
    cheap HTTP calls, not what we're optimizing.

Trade-off vs simpler "every MCP polls":
  + ~3× reduction in poll volume at typical multi-session usage.
  + Removes the "Firestore read budget scales with terminal count" cliff.
  − One extra file (`poll.lock`) in the cache dir.
  − Brief leader-handoff gap on process crash (≤ next try_acquire retry,
    default 60s) where no one polls. Acceptable: replies still surface
    via the next leader's first cycle.

Why this matters specifically with armed/dormant: dormant cadence is
already cheap (288 polls/day per MCP). The leader-election win is bigger
during ARMED periods — checking-mode flows would otherwise multiply by
session count. With leader election, one user with 3 terminals running
"deploy is finishing, ping me" gets one polling MCP, not three.
"""

from __future__ import annotations

import fcntl
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO


logger = logging.getLogger("remotecodetrol_mcp.leader")


@dataclass
class LeaderElector:
    """Per-host file-lock arbiter.

    Usage:
        elector = LeaderElector(lockfile_path=Path("~/.cache/.../poll.lock"))
        if elector.try_acquire():
            # We're the leader. Run the poll loop.
        else:
            # We're a follower. Skip polling.

    `try_acquire()` is non-blocking. The held `IO` lives on the instance
    until `release()` (or process exit). The OS releases automatically
    on process death — `fcntl.flock` semantics, not a separate liveness
    layer.
    """

    lockfile_path: Path
    _fd: IO[str] | None = None
    _is_leader: bool = False

    def try_acquire(self) -> bool:
        """Attempt to become leader. Returns True if successful.

        Safe to call repeatedly: if we already hold the lock, returns
        True without re-locking. If another process holds it, returns
        False without raising.
        """
        if self._is_leader:
            return True
        try:
            # Create-if-missing, don't truncate (preserves previous
            # leader's pid for diagnostics if we lose the race).
            self.lockfile_path.parent.mkdir(parents=True, exist_ok=True)
            fd = open(self.lockfile_path, "a+")
        except OSError as exc:
            logger.warning("leader: cannot open lockfile %s: %s", self.lockfile_path, exc)
            return False
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            fd.close()
            return False
        # Record our pid for `lsof` / debugging. Truncate first because
        # `a+` left us at EOF after a previous holder's pid.
        try:
            fd.seek(0)
            fd.truncate()
            fd.write(f"{os.getpid()}\n")
            fd.flush()
        except OSError:
            # Non-fatal: the lock still arbitrates correctly. The pid
            # line is just diagnostic.
            pass
        self._fd = fd
        self._is_leader = True
        logger.info("leader: acquired %s (pid=%d)", self.lockfile_path, os.getpid())
        return True

    def release(self) -> None:
        """Release the lock if held. Idempotent; safe in cleanup paths."""
        if self._fd is None:
            self._is_leader = False
            return
        try:
            fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            logger.debug("leader: flock(LOCK_UN) failed: %s", exc)
        try:
            self._fd.close()
        except OSError:
            pass
        self._fd = None
        self._is_leader = False
        logger.info("leader: released %s", self.lockfile_path)

    @property
    def is_leader(self) -> bool:
        return self._is_leader


def default_lockfile_path() -> Path:
    """Canonical path for the per-host poll lock.

    Matches the cache dir layout used by `pending.json` so all the
    cross-MCP coordination state sits together. On macOS that's
    `~/Library/Caches/remotecodetrol/`; on Linux, XDG cache dir.
    """
    import platform

    if platform.system() == "Darwin":
        base = Path.home() / "Library" / "Caches" / "remotecodetrol"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg) / "remotecodetrol" if xdg else Path.home() / ".cache" / "remotecodetrol"
    return base / "poll.lock"
