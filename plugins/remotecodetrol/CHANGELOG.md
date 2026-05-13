# Changelog

## v0.6.1 — Free-tier-sustainable polling: armed/dormant + leader election

Two compounding changes that take the v0.6.0 polling consumer from
"works for solo use" to "works for 10–20 users on Firestore's free
tier" (50K reads/day).

**Armed / dormant state machine.** The consumer now has a third state
beyond busy/idle: DORMANT (no recent activity → poll every 5 min).
The default state at boot is dormant — an MCP that's never been
asked to do anything contributes only ~288 reads/day, not 1440.
Tools that expect new traffic (`set_thread`,
`send_message(require_response=True)`, `peek_messages`,
`wait_for_response`, `set_waiting`) call `polling.arm()`, which
flips state to armed AND wakes any in-flight dormant sleep within
~10 ms via a new `_wake: asyncio.Event`. After 2 hours without an
`arm()` call, the consumer slips back to dormant on its own (matches
the cron `/loop` skill's stop condition).

**Leader-elected polling.** A new `leader.py` uses POSIX `fcntl.flock`
on `~/Library/Caches/remotecodetrol/poll.lock` to ensure exactly one
MCP per host runs the poll loop, regardless of how many Claude
sessions are open. Losers go into FOLLOWER mode: no polling, retry
acquisition every 60 s (in case the leader dies). The OS releases
flock on process death — no liveness daemon needed. Followers' tool
calls fall through to reading `pending.json` from disk (the leader's
output), so `peek_messages` still returns fresh data without anyone
hitting the backend. `RC_DISABLE_LEADER=1` opts out (every MCP polls,
v0.6.0 behavior).

**State file v2.** `pending.json` schema bumped to 2 — entries now
carry the full message dict as `raw` alongside the existing
preview/id/thread fields. v1 readers (the UserPromptSubmit hook)
ignore the extra key; followers depend on it to reconstruct the
cache without hitting the backend. `read_state_file()` tolerates v1
files and returns `[]` rather than failing.

**Cost math at 20 users, with both changes:**
- ~1 MCP per host actually polling (vs. one per session)
- ~80% of day in dormant (300 s) + ~20% armed (60 s avg)
- Expected reads: ~7K/day, well under the 50 K/day Firestore free tier
- Expected function invocations: ~7K/day, well under the 2 M/month tier

**iOS scroll bug** (companion fix in main repo):
- `isAtBottom` defaulted to `true` on first mount — a lie before the
  LazyVStack rendered. That caused two visible bugs together: opening
  a thread sometimes landed mid-thread instead of at the latest
  message, and the jump-to-latest pill (gated on `!isAtBottom`) never
  showed in that state.
- Fix: default `isAtBottom = false`, add `didInitialScroll: Bool`,
  gate `topSentinel.onAppear → loadOlder()` on it. Initial render
  no longer paginates-up before the snap-to-bottom completes. Pill
  now shows in any "we're not at the bottom" state, giving the user
  a one-tap escape hatch if the snap ever races again.

**Plugin tests:** 16/16 polling, 97/97 total.
**Backend tests:** 51/51 (no backend changes in this release).

## v0.6.0 — Polling consumer replaces SSE; `stream` Cloud Function removed

**Why:** every SSE connection pinned a Cloud Run instance
(`containerConcurrency: 1` despite the TF comment claiming 80), driving
`stream` cost to ~$144/mo per active user and tripping
`max_instance_count=10` 429s on every new session beyond the cap. For
solo usage, polling is essentially free.

**MCP plugin:**
- New `polling.py` with cost-optimized "Option B" defaults: 5s busy,
  60s idle (growing 2× per empty cycle to 300s ceiling), 2s while a
  tool is actively waiting on a reply.
- `next_interval()` is a pure function — no state, no I/O — so it's
  testable in isolation. All cadence decisions live in one place.
- `set_waiting(True/False)` toggle invoked by `wait_for_response` and
  `send_message(wait=True)` for the duration of a blocking call, so
  the loop tightens to ~2s for the wait then restores normal cadence.
- `streaming.py` stripped of `SseConsumer`, `SseParser`, `SseEvent`,
  `next_backoff`, sentinel exceptions. `StreamingState`,
  `_normalize_message`, `MAX_PENDING_PER_THREAD` stay — the polling
  consumer reuses the same cache surface so nothing else changes.
  The `SseStatus` Literal name kept for back-compat (tools.py reads
  it; renaming everywhere wasn't worth the diff).
- `server.py` swaps `SseConsumer` → `PollingConsumer` at boot. Env
  flag renamed `RC_DISABLE_STREAMING` → `RC_DISABLE_POLLING`.
- Deleted tests: `test_sse_parser.py`, `test_backoff.py`. New tests:
  `test_polling.py` (7 cases covering `next_interval`, cache
  population, waiting-mode cadence, camelCase normalization).

**Backend:**
- Removed `triggers/stream.ts` and the `stream` export from `index.ts`.
- Removed `formatSseEvent` and `HEARTBEAT_FRAME` from `shared/wire.ts`
  (only `triggers/stream.ts` used them).
- Tests for the SSE wire helpers deleted alongside.

**Terraform:**
- `module.functions.google_cloudfunctions2_function.stream` deleted.
- `module.functions.google_cloud_run_v2_service_iam_member.stream_invoker_public` deleted.
- `output.stream_url`, `output.stream_function_name` removed (no
  external references in this repo).

**Cost impact:** stream function eliminated. Expected savings ≈
$144/mo at single-user volume. The polling consumer adds a handful
of `GET /v1/threads/{tid}/messages` calls per minute per active
session, well under the Cloud Functions 2M-invocation free tier.

**Behavioral trade-off:** average reply latency increases from ~1s
(SSE) to ~30s at idle cadence (60s poll, average half-window). When
a tool is actively blocked (`wait=True`) the cadence tightens to 2s
so the perceived latency is ~1s during interactive question-and-wait
flows. The cron `loop` skill at 1m granularity is unaffected — it
was already polling on its own schedule.

**Future:** a `leader.py` could add POSIX-flock leader election so
only one MCP per host runs the loop (followers read the resulting
`pending.json` on demand, same path the hook already uses). Deferred
because the cost case for it is weak at solo-user volume — polling
is already cheap per-process. Easy to add later if multi-user
volume picks up.

## v0.5.0 — Selectable response buttons

`send_message` accepts `response_options` (1–5 buttons) +
`selection_mode` (`single`/`multi`). The iOS app renders tappable
buttons under the Claude message. The user's reply carries both
`body` (joined labels, human-readable) and `selected_option_ids`
(structured ids for branching).

**MCP plugin:**
- New `ResponseOption` Pydantic model with `id` / `label` / `color`
  validation. New `MAX_RESPONSE_OPTIONS = 5`, `SelectionMode` literal.
- `tools.py::send_message` gains `response_options` and
  `selection_mode` kwargs. Validates locally (cardinality, unique ids,
  regex on id, no newlines in label) before the network round-trip so
  bad calls fail fast at the tool boundary, not as a generic 400.
- `tools.py::Message` adds `response_options`, `selection_mode`,
  `selected_option_ids` fields — explicit, so the v0.4.5 trap (Pydantic
  silently dropping wire-format keys via `extra: "ignore"`) doesn't
  recur.
- `streaming.py::_normalize_message` maps camelCase
  `responseOptions`/`selectionMode`/`selectedOptionIds` →
  snake_case at the cache layer, defense-in-depth for old backends.
- `skills/remotecodetrol/SKILL.md` documents the new params and when
  to use buttons vs. plain markdown.

**Backend (companion v0.5.0):**
- `shared/types.ts::MessageDoc` gains optional `responseOptions`,
  `selectionMode`, `selectedOptionIds` fields.
- `api/routes/threads.ts::SendMessageSchema` accepts both camelCase
  and snake_case for the new fields, validates cross-coupling via
  `superRefine`, persists in canonical camelCase.
- `shared/wire.ts::toWireMessage` emits snake_case
  `response_options`/`selection_mode`/`selected_option_ids` only when
  non-empty.
- `infra/modules/firestore/rules/firestore.rules` allows iOS clients
  to write `selectedOptionIds` (list, ≤5) on user-create messages and
  forbids the Claude-only `responseOptions`/`selectionMode` keys on
  user creates.

**iOS (companion v1.3.0):**
- New `ResponseSelectorView` rendered inside the message bubble; single
  tap commits in `single` mode, "Send N" pill commits in `multi` mode.
- `Message` gains `responseOptions`, `selectionMode`,
  `selectedOptionIds`; `sendUserReply` plumbs through.
- Theme tokens for selected / unselected / disabled button states with
  full light + dark variants.

**Compatibility:** all new fields optional, both ends. Old MCP + new
backend → Claude can't ask with buttons but receives the user's
`body` text normally. New MCP + old backend → the Zod schema rejects
the new fields with a 400 (the backend MUST be deployed first). Old
iOS + new backend → buttons silently absent in render; the message
body still shows correctly.

## v0.4.5 — Spec 2 wire fields restored (replied_to, mcp_acked_at, claude_acked_at)

Found by exercising iOS v1.2.0's reply feature: the user tapped Reply,
the iOS code wrote `repliedTo` to Firestore correctly, the backend
relayed it on the wire as `replied_to` — but Claude's `peek_messages`
output never included it.

Root cause: the MCP's Pydantic `Message` model was authored before
Spec 2 and didn't declare any of the new fields. Combined with
`extra: "ignore"`, validation silently dropped them. Same pattern
would also have eaten `mcp_acked_at` and `claude_acked_at` (the
tri-state read-receipt fields) — so the fix adds all three.

Files:
- `tools.py::Message` — added `replied_to`, `mcp_acked_at`,
  `claude_acked_at` fields.
- `streaming.py::_normalize_message` — added camelCase fallbacks
  for the same three (`repliedTo`/`mcpAckedAt`/`claudeAckedAt` →
  snake), defense-in-depth in case the SSE wire ever emits the
  iOS-side casing directly.

The bug class is "data was correct on the wire, schema dropped it
during validation." Worth a code-review pattern: any time you add a
field to the Firestore schema or wire format, grep `extra: "ignore"`
and update every consumer model.

## v0.4.4 — Boot-time grandfather of `active_thread` into `known_threads`

Two fixes from a post-restart `whoami` test:

- **`__version__` was stale** (`"0.3.13"`). Bumped to `0.4.4` to match
  `pyproject.toml` / `plugin.json`. Cosmetic-only — actual code was
  v0.4.x; just the version constant was unbumped across the v0.4.x
  cuts.

- **`active_thread` persisted but `known_threads` didn't.** After a
  Claude Code / MCP restart, `state.json::active_thread` reloads
  (e.g. `"HackNet"`) but `known_threads` is in-memory only and starts
  empty. Result: `peek_messages()` and `ack_messages()` on the active
  thread fail with "thread not in known_threads" until the user
  explicitly re-declares intent. Sending still worked because
  `send_message`'s `auto_add=True` path quietly fixed it.

  Fix: at MCP boot in `server.py`, read `_STATE.get()` and seed
  `known_threads` with it. Treats the persisted active_thread as
  prior-session intent — same idea as the in-flight auto-add, just
  applied at startup.

  This deliberately keeps `known_threads` itself in-memory (per spec
  §5.1); the seed comes from data already on disk.

## v0.4.3 — Real email in `whoami` (no more `default`)

Backend now returns `email` in the `/v1/oauth/token` and
`/v1/oauth/check-link` responses. The MCP stores it as
`active_email`, so `whoami` and `tokens.json` show your actual
address (e.g., `me@zpriddy.com`) instead of the placeholder
`default` that v0.4.0–0.4.2 used because opaque tokens carry no
JWT claims.

Backwards-compatible: backends that don't yet return `email` cause
the MCP to fall back to the existing active_email or `default` —
same behavior as v0.4.2.

The MCP also cleans up the stale `default` entry in `tokens.json`
on the first link/rotate against a v0.4.3+ backend, so you don't
end up with both `default` and `me@zpriddy.com` records.

## v0.4.2 — `rcct` wrapper symlink-resolution fix

The `bin/rcct` Bash wrapper used `BASH_SOURCE[0]` to derive
`${CLAUDE_PLUGIN_ROOT}` when run outside Claude Code. `BASH_SOURCE`
doesn't follow symlinks, so when the SessionStart hook installed
`~/.local/bin/rcct → ${CLAUDE_PLUGIN_ROOT}/bin/rcct` and the user
invoked `rcct` from PATH, the wrapper's fallback resolved to
`~/.local/` — which has no `mcp-server/` subdir, so `uvx --from`
failed with `Distribution not found at: file:///Users/.../.local/mcp-server`.

Fix: iterate the symlink chain manually before computing the parent
directory. macOS bash 3.2 has no `readlink -f`, so the loop is
portable across Linux + macOS. When `${CLAUDE_PLUGIN_ROOT}` is set by
Claude Code (the common case inside the MCP context), the resolution
short-circuits — no behavior change there.

Found by exercising `rcct whoami` from a Bash shell. The transport
itself was fine; the wrapper just couldn't find the package to launch.

## v0.4.1 — Spec 2/3 polish + tests + delivered-notify

Adds the v0.4.0 test suite, hardens `socket_server` against non-socket
files at the path, and lights up the MCP-side `delivered` notification
that powers the iOS tri-state read receipts shipping in app v1.2.0.

### Tests
- 40 new tests across 5 files (`test_v4_auth`, `test_known_threads`,
  `test_complete_link`, `test_socket_server`, `test_qr`) — covers the
  v0.4.0 surface end-to-end.
- 4 stale v0.3.x test files removed (`test_auth`, `test_client`,
  `test_sse_consumer`, `test_tools`) — they imported v0.3.x-only
  symbols (`PENDING_FLOW_KEY`, `TokenBundle`) and were broken at
  baseline. Coverage on `client.py` and the SSE consumer's run-loop
  is reduced; worth a follow-up sweep.

### Socket server hardening
- `_handle_stale_socket` now unlinks anything that isn't a live AF_UNIX
  socket (was: only `ECONNREFUSED` / `ENOENT`). Closes a gap where a
  regular file at the socket path stranded the MCP with `EADDRINUSE`
  on bind. Found by the test suite.

### Delivered notification (Spec 2 backend integration)
- The SSE consumer's `message.created` handler now spawns a
  fire-and-forget `POST /v1/threads/{tid}/messages/{mid}/delivered` so
  the backend can stamp `MessageDoc.mcpAckedAt`. iOS reads this to
  render the second tri-state check (the "blue lines" between sent
  and Claude-acked).
- Errors on the call are swallowed at debug level — never blocks the
  SSE loop.

## v0.4.0 — Transport rewrite (auth + CLI + thread scoping + QR)

Spec 1 of the v4 trilogy. Replaces the OAuth access+refresh JWT pair
with a single 14-day opaque token, adds a `rcct` Bash CLI talking to
the MCP over a Unix domain socket, enforces per-session thread
isolation via a `known_threads` allowlist, and renders an ASCII QR in
the link flow. Spec doc:
`docs/superpowers/specs/2026-05-11-v4-transport-design.md`.

**Breaking:** no migration from v0.3.x. Existing links are abandoned;
v4 detects the old `tokens.json` schema (no `schema_version` key, or
`< 4`), discards it, and the next tool call surfaces a clean re-link
prompt. Users re-link via `/remotecodetrol:link`. Documented in the
spec as a deliberate scope cut — the migration complexity isn't worth
it for the user base size.

### Auth — single 14-day opaque token

- 32-byte URL-safe random, hashed (`SHA-256`) for the Firestore doc id;
  plaintext returned exactly once at issuance. Same hash-as-id pattern
  as the old `oauth_refresh_tokens` collection.
- 14-day TTL, day-7 background rotation trigger, hourly retry over the
  remaining 7 days if rotation fails. Non-blocking — the old token
  stays valid while rotation retries.
- **60s rotation grace window** solves the "client never received the
  rotation response" race: when a token is replaced, the old doc gets
  `supersededBy = newHash` and `graceUntil = now + 60s`. Validation
  accepts it during the grace; client retries with the old token,
  receives the new plaintext a second time, learns the new value.
- **New endpoints:** `POST /v1/oauth/rotate` (overlap rotation),
  `POST /v1/oauth/check-link` (force-poll, bypasses RFC 8628
  cooldowns).
- **Removed endpoints:** `/v1/oauth/jwks`. The `refresh_token` grant
  type on `/v1/oauth/token` is dropped entirely — v4 uses `/rotate`.
- **Auth middleware** rewritten: JWT verification removed; v4 validates
  opaque tokens against `mcp_tokens/<hash>` with a 60s LRU cache (~500
  entries per instance). Steady-state expected ≥ 95% hit rate; cache
  hit is pure RAM, no Firestore read.
- **`tokens.json` schema:** new `schema_version: 4` shape with a single
  `token` field per email; old shape is detected and discarded.
- **Daily sweeper:** new `sweepMcpTokensCron` Cloud Function (Cloud
  Scheduler → Pub/Sub) deletes `mcp_tokens` docs where
  `expiresAt < now - 1d` OR (`supersededBy != null AND graceUntil < now - 1d`).

### CLI — `rcct` over Unix domain socket

- New `rcct` Bash entry point (`bin/rcct` shell wrapper around `uvx
  --from ... rcct`). Subcommands: `send`, `check`, `wait`, `ack`,
  `whoami`, `link`, `check-link`, `logout`, `threads {list, allow,
  forget, known}`. All accept `--json` for machine-readable output.
- **IPC:** line-delimited JSON over Unix domain socket at
  `~/Library/Caches/remotecodetrol/mcp.sock` (macOS) /
  `${XDG_RUNTIME_DIR}/remotecodetrol/mcp.sock` (linux), permission
  `0600`. Single-request-per-connection. Stale-socket recovery via
  connect-probe at startup.
- **Single implementation, two surfaces:** the same Python functions
  registered as `@mcp.tool` are dispatched from the socket handler.
  No duplication; `tokens.json`, the SSE cache, and `known_threads`
  are all shared because there's only one MCP process.
- **No auto-spawn.** If the socket isn't responding, `rcct` exits 2
  with a hint to start a Claude session with the plugin. Single-writer-
  on-`tokens.json` invariant > convenience of standalone CLI.
- **PATH install:** a `SessionStart` hook symlinks `~/.local/bin/rcct`
  → `${CLAUDE_PLUGIN_ROOT}/bin/rcct` on every session start
  (idempotent; auto-points at the latest plugin version after upgrade).
  Stderr warning if `~/.local/bin` isn't on PATH.

### `known_threads` — per-session thread allowlist

- New `set[str]` on `StreamingState`, in-memory only, per MCP process.
  Re-derived from env + runtime declarations on every launch (no
  persistence; aligns with session-scoped philosophy).
- **Auto-populated** by `set_thread(name)` and `send_message(thread=...)`
  — sending IS declaring intent. Pre-seedable via the
  `REMOTECODETROL_KNOWN_THREADS=foo,bar` env var.
- **`forget_thread(name)`** / `rcct threads forget <name>` removes a
  thread, clears active if it matched, and prunes the cache.
- **SSE consumer drops events** for threads not in the set (snapshot
  replay filtered the same way). Cross-session leakage is now
  structural, not honor-system: Claude session A literally cannot see
  threads it hasn't declared, even when session B sends on them.
- **`peek_messages` / `ack_messages` reject** unknown threads.
  `peek_messages(thread="*")` returns pending across known threads
  only. `list_threads()` still returns ALL backend threads
  (discoverability) — each `ThreadSummary` gains a `known: bool`.

### Link flow — ASCII QR + `complete_link()`

- `link()` / `rcct link` now render an ASCII QR (`qrcode` lib,
  error-correction `M`, `invert=True` for dark terminals) encoding
  `remotecodetrol://authorize?code=<USER_CODE>`. The iOS app's in-app
  scanner extracts the code; iPhone Camera will deep-link once Spec 2
  ships URL-scheme registration.
- `LinkResult` gains `qr_ascii`, `deep_link`, and `expires_in_seconds`.
  The `user_code` remains as manual fallback. SKILL teaches Claude
  to lead with the QR and offer the code as backup.
- **New `complete_link()` tool** / `rcct check-link` for the "user
  just confirmed" case. Hits `/v1/oauth/check-link` directly,
  bypassing both client-side cooldown gates (added in v0.3.6) and
  server-side RFC 8628 `slow_down` enforcement. Returns
  `authorized | pending | expired | denied | invalid`. Use only when
  the user explicitly says "I tapped Confirm"; otherwise the SKILL
  rule is "wait at least 30 seconds before any status check."

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
