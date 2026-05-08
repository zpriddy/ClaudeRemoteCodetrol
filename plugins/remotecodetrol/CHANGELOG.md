# Changelog

## v0.3.x — Streaming relay redesign

The v0.3.x lineage replaces v0.2.x's polling architecture (5-minute
peek interval, request/response MCP) with a push-driven streaming
design: a long-lived SSE connection between the MCP and the
backend, an in-memory cache mirrored from server-side state, and a
`UserPromptSubmit` hook that surfaces pending replies as Claude's
context on each subsequent prompt.

### v0.3.13 — checking-mode is sticky

`send_message(require_response=True)` puts the session into
**checking mode**. While in checking mode, Claude MUST keep a
recurring CronCreate poll running for replies. Stops only on
explicit user-stop or 2 hours of silence. SKILL hardened from
"may set up a loop" to "must keep one running once activated."

### v0.3.12 — SKILL clarifies loop vs ralph-loop, cron constraints

Built-in `loop` skill, NOT `ralph-loop:ralph-loop`. 1-minute cron
minimum (30s rounds up). Session-scoped by default. Always cancel
the loop after the wait completes.

### v0.3.11 — SKILL teaches `/loop` for self-polling on long waits

Pure SKILL update. Documents the third pattern (self-poll loop)
alongside `wait=True` (block) and end-the-turn (rely on external
prompts).

### v0.3.10 — `send_message` non-blocking by default (tmux-injection compat)

Reverted v0.3.9's blocking-by-default. The hook fires on ANY
`UserPromptSubmit` — terminal-typed OR external-automation-injected
(tmux pipes, cron, watch-mode, parallel sessions) — so blocking
the entire session by default was wrong. `wait=True` is preserved
as explicit opt-in.

### v0.3.9 — `send_message` blocks by default (superseded by 0.3.10)

Initial pass at making `send_message(require_response=True)` block
without needing a separate `wait_for_response`. Ergonomically nice
but broke tmux/cron/parallel workflows. Reverted in v0.3.10.

### v0.3.8 — hook output shape (`hookSpecificOutput` envelope)

Hook script was emitting flat `{"additionalContext": "..."}`. Claude
Code expected `{"hookSpecificOutput": {"hookEventName": "...",
"additionalContext": "..."}}`. Hook fired correctly, exited 0,
emitted valid JSON — and Claude Code silently ignored it. Diagnosed
via session-transcript JSONL (compared shape against superpowers'
SessionStart hook).

### v0.3.7 — SSE consumer always spawns at startup

Removed the `_have_credentials()` gate in server.py. Pre-0.3.7,
fresh installs needed TWO Claude Code restarts (one to spawn MCP,
one after `/link` to spawn the consumer with creds available). The
consumer's run loop is already resilient to no-creds-at-runtime
(v0.3.3); v0.3.7 just lets that resilience handle startup too.

### v0.3.6 — device-code poll cooldown (RFC 8628 `slow_down`)

Treated `slow_down` identically to `authorization_pending` — kept
hammering the OAuth poll endpoint, server kept saying "slow down,"
loop never cleared. Added `MIN_DEVICE_POLL_INTERVAL_SECONDS = 30`
and a `_next_device_poll_allowed_at` cooldown gate.

### v0.3.5 — file-based token storage

macOS Keychain ACLs are bound to the calling binary's path. Plugin
reinstalls put the MCP at a new path (`/cache/.../0.3.X/`), and
the new binary loses access to keychain entries written by the
previous version. Switched to a JSON file at
`~/Library/Application Support/RemoteCodetrol/tokens.json` (chmod
0600), with one-time migration from keychain.

### v0.3.4 — SKILL adds cross-session ack discipline

If multiple Claude Code sessions are running for the same user,
acking a message removes it from every session's cache. Old SKILL
said "ack everything you peeked"; new SKILL says "ack only your
active thread; other threads belong to other sessions." Caught
when this Claude session ack'd a message that belonged to the
user's other session running on the GasCity thread.

### v0.3.3 — SSE consumer waits for `/link` (no double-restart)

Pre-0.3.3, no-creds-at-runtime caused the SSE consumer to
permanently exit (`auth_failed`). After `/link` wrote a token,
the consumer was already gone and only a Claude Code restart
respawned it. v0.3.3 distinguishes `NotAuthorizedError` (no token,
non-terminal — wait for link) from `AuthError` (token revoked,
terminal).

### v0.3.2 — persist state file after proactive ack-prune

`tools.ack_messages` proactively pruned the local cache on HTTP
2xx, but didn't write the state file. The SSE-side `_persist`
only fires when `remove_messages` returns non-zero — which it
won't, because tools.py beat it to the prune. The hook ended up
re-injecting already-acked messages on every prompt. Added
`state.persist_now()` called from `ack_messages`.

### v0.3.1 — stream URL fix

`streaming.py` constructed the SSE URL as
`{api_v1}/stream` → `https://.../api/v1/stream`. The actual
stream Cloud Function is at `https://.../stream` — separate
function, separate URL path. `/api/v1/stream` 404'd at the api
function, came back as 401 due to auth-middleware-before-route
order, MCP saw 401, called `auth.invalidate()`, reconnect-loop'd.
Added `config.stream_url` derived from `api_base` (strip `/api`,
append `/stream`), with `REMOTECODETROL_STREAM_URL` override.

### v0.3.0 — initial streaming relay

New `stream` Cloud Function (separate from `api`, CPU-always-
allocated, 60-min timeout, concurrency 80). New `ownerUid` field
on `MessageDoc` so the listener's collection-group query can
filter per-user. New `messages_owner_pending` composite index.
SSE event protocol (`connected`, `state.snapshot`,
`message.created`, `message.acked`). MCP-side: stateful consumer,
in-memory cache, atomic `pending.json` writer, `UserPromptSubmit`
hook. `send_message` bundles `pending_messages` in its response.

## Architectural lessons recorded

The v0.3.x lineage took 13 patches. The recurring theme: every
"happy-path tested, ships clean" assumption hid a real-world
constraint. Listed as commit-message taglines in case the pattern
shows up again:

- Spec was right but implementation diverged in a way only e2e
  testing exposed (0.3.1, 0.3.6, 0.3.8)
- Two paths into the same state mutation, only one had persistence
  (0.3.2)
- Conflated error semantics in a single except clause (0.3.3)
- Skill written for single-session world breaks under concurrent
  sessions (0.3.4, 0.3.13)
- Path-bound OS primitive (Keychain) breaks under
  path-rotating package manager (0.3.5)
- Gate at one layer needs to mirror gate at the layer below
  (0.3.7 mirrors 0.3.3)
- Behavioral default that makes sense for one workflow doesn't
  for another; non-blocking + opt-in beats blocking + opt-out
  (0.3.10 reverts 0.3.9)
- Skill voice matters: "may" gets optimized away; "must" sticks
  (0.3.13)
