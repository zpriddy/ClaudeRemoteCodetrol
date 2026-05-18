"""@mcp.tool implementations for the RemoteCodetrol MCP server.

In v0.3.0 the MCP is a stateful streaming client (see streaming.py +
docs/superpowers/specs/2026-05-07-mcp-streaming-relay-design.md). Tools
prefer the in-memory cache when SSE is healthy and fall back to a direct
backend call when the cache is stale.

Thread resolution priority (unchanged from v0.2.4):
  1. explicit `thread=` parameter on the call
  2. value previously stored via `set_thread`
  3. REMOTECODETROL_THREAD env var (read into Config at startup)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Literal

logger = logging.getLogger("remotecodetrol_mcp.tools")

from pydantic import BaseModel, Field

from . import __version__ as PLUGIN_VERSION
from .client import APIClient, APIError
from .config import Config
from .qr import render_qr_ascii
from .state import read_state, update_state
from .streaming import StreamingState


# ---------- thread state ----------


class ThreadState:
    """Active-thread accessor. Always reads from state.json so updates
    written by another MCP process (e.g. a parallel Claude Code session)
    become visible immediately. The state file is small enough that a
    fresh read per tool call is negligible; the alternative — caching
    `_active` once at __init__ — caused cross-session staleness."""

    def __init__(self, config: Config):
        self.config = config

    def get(self, override: str | None = None) -> str | None:
        if override:
            return override
        return read_state().get("active_thread") or self.config.default_thread

    def set(self, name: str) -> None:
        update_state({"active_thread": name})


# ---------- pydantic result models ----------


class SetThreadResult(BaseModel):
    active_thread: str


# ---------- selectable response options (v0.5.0) ----------

ResponseOptionColor = Literal["neutral", "accent", "success", "warning", "danger"]
SelectionMode = Literal["single", "multi"]
MAX_RESPONSE_OPTIONS = 5
# Same regex the backend enforces; we validate locally to surface errors
# at tool-call time rather than after a network round-trip.
_OPTION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class ResponseOption(BaseModel):
    """A selectable-response button shown under a Claude message in the iOS app.

    `id` is opaque, Claude-chosen, stable across renders. It comes back to
    Claude as part of `selected_option_ids` so use values that are meaningful
    for branching ("yes", "opt_a", "deploy_now"), not random uuids.
    """

    id: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=80)
    color: ResponseOptionColor | None = None

    model_config = {"populate_by_name": True}


def _validate_response_options(
    options: list[ResponseOption] | None,
    mode: SelectionMode | None,
) -> None:
    """Local validation that mirrors the backend Zod schema. Raises ValueError
    so the MCP returns a structured tool error rather than a 400 from the
    backend after a wasted round-trip."""
    if options is None or len(options) == 0:
        if mode is not None:
            raise ValueError(
                "selection_mode requires response_options"
            )
        return
    if mode is None:
        raise ValueError(
            "selection_mode is required when response_options is provided"
        )
    if len(options) > MAX_RESPONSE_OPTIONS:
        raise ValueError(
            f"response_options accepts at most {MAX_RESPONSE_OPTIONS} entries"
        )
    ids = [o.id for o in options]
    if len(set(ids)) != len(ids):
        raise ValueError("response_options ids must be unique")
    for o in options:
        if not _OPTION_ID_RE.match(o.id):
            raise ValueError(
                f"response_options id {o.id!r} contains invalid characters"
                " (allowed: [a-zA-Z0-9_-])"
            )
        if "\n" in o.label:
            raise ValueError("response_options label cannot contain newlines")


class Message(BaseModel):
    """Spec §4.1 wire shape, accepts camelCase or snake_case input.

    Output is always emitted by-alias (snake_case as the design doc
    specifies); the legacy `messageId` alias survives for input only.
    """

    id: str = Field(alias="id")
    thread_id: str | None = Field(default=None, alias="thread_id")
    thread_name: str | None = Field(default=None, alias="thread_name")
    body: str = ""
    sender_type: str | None = Field(default=None, alias="sender_type")
    sender_id: str | None = Field(default=None, alias="sender_id")
    created_at: str | None = Field(default=None, alias="created_at")
    require_response: bool | None = Field(default=None, alias="require_response")
    idempotency_key: str | None = Field(default=None, alias="idempotency_key")
    acked_at: str | None = Field(default=None, alias="acked_at")
    body_truncated: bool | None = Field(default=None, alias="body_truncated")
    # Spec 2 (iOS app v1.2.0+): reply-context + tri-state read receipts.
    # Without these declarations, `extra: "ignore"` silently dropped them
    # during validation — the wire data was correct, but Claude's view
    # never showed `replied_to` even when the iOS user tapped Reply.
    replied_to: str | None = Field(default=None, alias="replied_to")
    mcp_acked_at: str | None = Field(default=None, alias="mcp_acked_at")
    claude_acked_at: str | None = Field(default=None, alias="claude_acked_at")
    # v0.5.0: selectable-response buttons on Claude messages (set on Claude
    # sends) and the user's tap-selection on user replies. Pydantic would
    # silently drop these without explicit declarations — same trap as the
    # v0.4.5 Spec 2 incident; the test suite below pins the round-trip.
    response_options: list[ResponseOption] | None = Field(
        default=None, alias="response_options"
    )
    selection_mode: SelectionMode | None = Field(
        default=None, alias="selection_mode"
    )
    selected_option_ids: list[str] | None = Field(
        default=None, alias="selected_option_ids"
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}


class SendResult(BaseModel):
    """Spec Appendix A: send_message returns id + bundled pending."""

    message_id: str
    thread: str
    pending_messages: list[Message] = Field(default_factory=list)
    pending_count: int = 0


class PeekResult(BaseModel):
    messages: list[Message]
    cursor: str | None = None
    source: str = "cache"  # "cache" | "api" — diagnostic, not load-bearing


class AckResult(BaseModel):
    acked: int


class GetMessagesResult(BaseModel):
    """v0.7.0: combined peek + ack result. `messages` is the list of
    unacked user messages that just got acked; `acked` is the count.
    Use when you want to consume all pending replies in one shot —
    saves Claude the bookkeeping of peek → process → collect-ids → ack.
    """

    messages: list[Message]
    acked: int


class ThreadSummary(BaseModel):
    name: str
    last_message_at: str | None = Field(default=None, alias="lastMessageAt")
    # v0.4.0: indicates whether this thread is in the per-session known_threads
    # allowlist. False threads are visible (so the user can discover them) but
    # send/peek/ack against them will error with "not in known_threads".
    known: bool = False

    model_config = {"populate_by_name": True}


class WhoamiResult(BaseModel):
    email: str
    active_thread: str | None
    sse_status: str
    plugin_version: str
    pending_count_by_thread: dict[str, int] = Field(default_factory=dict)
    # v0.4.0: per-session allowlist (sorted) — useful for debugging "why
    # can't I see thread X" surprises.
    known_threads: list[str] = Field(default_factory=list)


class LinkResult(BaseModel):
    """Returned from link(). Show both `user_code` and `qr_ascii` to the user.

    Scan with the iOS app's "Authorize new device" → in-app scanner; the
    QR encodes `remotecodetrol://authorize?code=<user_code>` which the
    scanner extracts and submits. The plain `user_code` is the manual
    fallback if scanning isn't convenient.
    """

    status: str  # "pending_authorization" | "already_linked"
    user_code: str | None = None
    verification_uri: str | None = None
    deep_link: str | None = None
    qr_ascii: str | None = None
    expires_in_seconds: int | None = None
    instructions: str
    email: str | None = None  # set when status == "already_linked"


class CompleteLinkResult(BaseModel):
    """Returned from complete_link(). Force-polls /v1/oauth/check-link.

    Use only when the user has explicitly confirmed in-app authorization.
    Bypasses RFC 8628 polling cooldowns.
    """

    status: str  # "authorized" | "pending" | "expired" | "denied" | "invalid"
    email: str | None = None  # set when status == "authorized"
    message: str


class ForgetThreadResult(BaseModel):
    name: str
    was_known: bool


class ListKnownThreadsResult(BaseModel):
    threads: list[str]


class LogoutResult(BaseModel):
    status: str  # "logged_out"
    message: str


# ---------- helpers ----------


def _resolve_thread(state: ThreadState, override: str | None) -> str:
    tid = state.get(override)
    if not tid:
        raise ValueError(
            "No thread set. Pass `thread=...`, call set_thread, or set "
            "REMOTECODETROL_THREAD. Use list_threads to see what's available."
        )
    return tid


def _ensure_known_thread(
    streaming: StreamingState | None, tid: str, *, auto_add: bool
) -> None:
    """Enforce the v0.4.0 known_threads allowlist.

    `auto_add=True` is the "declaring intent" path (set_thread, send_message):
    if the thread isn't yet known, add it. `auto_add=False` is the read path
    (peek_messages, ack_messages): unknown threads raise.

    No-op if `streaming` is None (test/standalone mode without SSE).
    """
    if streaming is None:
        return
    if streaming.is_thread_known(tid):
        return
    if auto_add:
        streaming.add_known_thread(tid)
        return
    raise ValueError(
        f"Thread '{tid}' is not in this session's known_threads. Call "
        f"set_thread('{tid}') or `rcct threads allow {tid}` first to "
        "declare intent. (Reading or acking on an undeclared thread is "
        "blocked to prevent cross-session leakage; see SKILL §known_threads.)"
    )


def _normalize_send_response(data: dict[str, Any] | None) -> dict[str, Any]:
    """Backend may emit camelCase; Tool surface speaks snake_case.

    Also tolerates old backends that don't return pending_messages yet —
    the design doc explicitly calls this out as a back-compat requirement
    (§9: "Old MCP clients receive the new fields and ignore them").
    """
    if not data:
        return {"messageId": None}
    out = dict(data)
    if "pending_messages" not in out and "pendingMessages" in out:
        out["pending_messages"] = out["pendingMessages"]
    if "pending_count" not in out and "pendingCount" in out:
        out["pending_count"] = out["pendingCount"]
    return out


def _messages_from_api(data: Any) -> list[dict[str, Any]]:
    """Coerce the legacy /v1/threads/{id}/messages response into our shape."""
    if not data:
        return []
    items = data.get("messages") if isinstance(data, dict) else data
    if not items:
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        msg = dict(it)
        # Legacy: backend used `messageId` for the doc id.
        if "id" not in msg and "messageId" in msg:
            msg["id"] = msg["messageId"]
        if "thread_id" not in msg and "threadId" in msg:
            msg["thread_id"] = msg["threadId"]
        if "sender_type" not in msg and "senderType" in msg:
            msg["sender_type"] = msg["senderType"]
        if "created_at" not in msg and "createdAt" in msg:
            msg["created_at"] = msg["createdAt"]
        out.append(msg)
    return out


# ---------- tool registration ----------


def register_tools(
    mcp: Any,
    api: APIClient,
    state: ThreadState,
    config: Config,
    streaming: StreamingState | None = None,
    polling: Any = None,  # PollingConsumer; typed as Any to avoid circular import
) -> dict[str, Any]:
    """Bind all @mcp.tool functions onto the given FastMCP instance.

    Also returns a `dispatchers` dict mapping tool name → async callable
    that takes the args dict and returns the tool's result. This is what
    socket_server.py uses for the CLI surface (single implementation,
    two surfaces — see spec §4.4).

    `streaming` is optional — when None (or when the consumer is
    `disabled` / `auth_failed`), tools fall back to direct API calls only.

    v0.6.0: `polling` is the `PollingConsumer` instance (replaces the
    old SSE consumer). When non-None, blocking call paths
    (`wait_for_response`, `send_message(wait=True)`) toggle
    `polling.set_waiting(True/False)` for their duration so the loop
    cadence tightens to ~2s while a caller is actually blocked. The
    code is intentionally tolerant of `polling=None` so the tests
    (which don't spin up a real consumer) keep working.
    """
    dispatchers: dict[str, Any] = {}

    def _expose(name: str, fn: Any) -> None:
        """Wrap fn with **args expansion for the socket-style dispatcher."""
        async def _wrapper(args: dict[str, Any]) -> Any:
            return await fn(**args)
        dispatchers[name] = _wrapper

    # Internal sentinel — when no streaming state is provided, behave as
    # if the cache is permanently stale so every read goes to the API.
    def _cache_fresh() -> bool:
        return streaming is not None and streaming.cache_is_fresh()

    def _streaming_status() -> str:
        return streaming.sse_status if streaming else "disabled"

    async def _await_reply_for_thread(
        tid: str, timeout_minutes: float
    ) -> tuple[list[Message], str]:
        """Block until a reply lands on `tid`, or timeout. Returns
        `(messages, source)` where source is "api" (cache reads were
        removed in v0.7.2 — see below).

        Used by both wait_for_response and (v0.3.9+) send_message with
        wait=True.

        v0.7.2: removed all cache reads from this path. Previously this
        function would return immediately if `streaming.pending` had
        anything cached for `tid` — but the cache can hold stale entries
        (e.g., another session acked the message but this MCP's prune
        notification path didn't fire), so the returned data was
        sometimes lying about pending state. Symptom: `wait_for_response`
        and `peek_messages` returning nothing even though new messages
        existed on the backend; only `send_message`'s bundling path
        (which queries Firestore directly) saw the truth.

        Current behavior:
          - Direct API peek first. If anything pending, return it.
          - If nothing: wait on `streaming.state_change` (set by the
            polling consumer on every cache mutation) as a wake signal,
            then re-peek the API. Loop until timeout.
          - If no streaming consumer (RC_DISABLE_POLLING=1 or
            auth_failed): single direct API peek, no waiting.

        Cost: each state_change wake = 1 API call. In waiting mode the
        polling cadence is 2s, so up to ~30 API calls/min during a
        wait. Acceptable — well under free-tier limits and bounded by
        the timeout.
        """
        deadline = time.monotonic() + float(timeout_minutes) * 60

        async def _fresh_peek() -> list[Message]:
            data = await api.get(
                f"/threads/{tid}/messages", params={"unackedOnly": "true"}
            )
            return [Message.model_validate(m) for m in _messages_from_api(data)]

        # Initial direct check. Bypasses any cache state entirely.
        initial = await _fresh_peek()
        if initial:
            return initial, "api"

        # Nothing yet. If we have no consumer to wake us, just return
        # empty (caller decides whether to retry / send again).
        if streaming is None or streaming.sse_status in ("auth_failed", "disabled"):
            return [], "api"

        # Tighten polling cadence to 2s for the wait window; restore on
        # exit. The polling consumer's add_message → state_change set is
        # what wakes this loop on each new arrival.
        if polling is not None:
            polling.set_waiting(True)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], "api"
                streaming.state_change.clear()
                if streaming.sse_status == "auth_failed":
                    return [], "api"
                try:
                    # Cap the sleep at 30s so we periodically re-poll
                    # the API even if state_change never fires —
                    # defense in depth against a stuck polling consumer.
                    await asyncio.wait_for(
                        streaming.state_change.wait(),
                        timeout=min(remaining, 30.0),
                    )
                except asyncio.TimeoutError:
                    pass  # fall through to re-check
                # Always re-query the API after a wake. Cache may have
                # received the message but we don't read it — going to
                # source of truth removes the entire stale-cache class
                # of bugs from this code path.
                msgs = await _fresh_peek()
                if msgs:
                    return msgs, "api"
        finally:
            if polling is not None:
                polling.set_waiting(False)

    # v0.6.1: small helper centralizing the "tell the polling consumer
    # that activity warrants real-time cadence" call. No-ops if polling
    # is None (tests, or RC_DISABLE_POLLING=1). Inlined where called
    # from would duplicate the None-check seven times.
    def _arm_polling() -> None:
        if polling is not None:
            polling.arm()

    @mcp.tool
    async def set_thread(name: str) -> SetThreadResult:
        """Set the active thread; also adds it to known_threads (v0.4.0+).

        The active-thread value is persisted to state.json so it survives
        MCP restarts. The known_threads allowlist is in-memory only and
        re-derived on each MCP launch.

        v0.6.1: arms the polling consumer — the user just declared
        intent on a thread, so we should be poll-ready for replies.
        """
        state.set(name)
        if streaming is not None:
            streaming.active_thread = name
            streaming.add_known_thread(name)
        _arm_polling()
        return SetThreadResult(active_thread=name)

    @mcp.tool
    async def send_message(
        body: str,
        require_response: bool = False,
        wait: bool = False,
        timeout_minutes: float | None = None,
        thread: str | None = None,
        idempotency_key: str | None = None,
        response_options: list[ResponseOption] | None = None,
        selection_mode: SelectionMode | None = None,
    ) -> SendResult:
        """Send a message to the user via the RemoteCodetrol iOS app.

        Set `require_response=True` when you need a reply before continuing.
        With `wait=False` (default), returns immediately after sending —
        Claude continues its turn, and the reply will land via either:
          - the UserPromptSubmit hook on the user's next prompt (whether
            they type it or some external automation injects it via tmux,
            cron, watch-mode, etc.), OR
          - a subsequent peek_messages / wait_for_response call by Claude.

        With `wait=True`, this call BLOCKS inside the tool until the reply
        arrives or `timeout_minutes` elapses. Use only when Claude truly
        cannot do anything useful until the answer comes back — and when
        you're willing to freeze the session for the duration.

        Why non-blocking-by-default (v0.3.10+): blocking pauses the entire
        Claude session, which is fine for one-shot interactive use but
        breaks workflows where prompts flow in from multiple sources
        (tmux pipes, cron, parallel sessions). The hook delivers replies
        on ANY UserPromptSubmit, not just terminal-typed ones, so external
        automation keeps Claude responsive.

        The response always includes `pending_messages` — any unacked
        replies on the thread at send time, plus newly-arrived ones if
        `wait=True` blocked.
        """
        tid = _resolve_thread(state, thread)
        # v0.4.0: sending IS declaring intent — auto-add to known_threads.
        _ensure_known_thread(streaming, tid, auto_add=True)
        # v0.6.1: any send arms the consumer; require_response=True arms
        # PLUS resets the idle-disarm timer. Even a fire-and-forget send
        # (require_response=False) usually means Claude is about to do
        # something the user will care about, so it's worth a few
        # minutes of armed cadence.
        if require_response:
            _arm_polling()
        # v0.5.0: the socket-server CLI dispatcher passes args as raw dicts,
        # so response_options can arrive as list[dict] rather than the typed
        # list[ResponseOption] FastMCP would deliver. Coerce to ResponseOption
        # so the validator (and the rest of the function) see a uniform type.
        if response_options is not None:
            response_options = [
                o if isinstance(o, ResponseOption) else ResponseOption.model_validate(o)
                for o in response_options
            ]
        # v0.5.0: validate selectable-response args before the round-trip so
        # bad input fails fast at the tool boundary (with a clear message)
        # rather than as a generic 400 from the backend Zod schema.
        _validate_response_options(response_options, selection_mode)
        payload: dict[str, Any] = {"body": body, "requireResponse": require_response}
        if idempotency_key:
            payload["idempotencyKey"] = idempotency_key
        # camelCase on the wire to match `requireResponse`/`idempotencyKey`
        # and the existing Firestore field names. The backend route accepts
        # snake_case too, but camelCase is canonical here.
        if response_options is not None and len(response_options) > 0:
            payload["responseOptions"] = [
                o.model_dump(exclude_none=True) for o in response_options
            ]
            payload["selectionMode"] = selection_mode
        data = await api.post(f"/threads/{tid}/messages", json=payload)
        normalized = _normalize_send_response(data)
        msg_id = normalized.get("messageId") or normalized.get("message_id")
        raw_pending = normalized.get("pending_messages") or []
        pending = [Message.model_validate(m) for m in raw_pending]

        # v0.3.9: block until reply if asked. Skip the wait if the bundle
        # already has something — that's our reply, no need to wait for
        # another. Skip if !require_response (no reply expected) or
        # !wait (caller opted out).
        if require_response and wait and not pending:
            timeout = (
                timeout_minutes
                if timeout_minutes is not None
                else config.default_timeout_minutes
            )
            awaited, _source = await _await_reply_for_thread(tid, timeout)
            pending = awaited

        return SendResult(
            message_id=msg_id or "",
            thread=tid,
            pending_messages=pending,
            pending_count=len(pending),
        )

    @mcp.tool
    async def peek_messages(
        thread: str | None = None,
        since_cursor: str | None = None,
    ) -> PeekResult:
        """Return unacked user messages.

        v0.6.2: the in-memory + on-disk cache short-circuits were
        removed. Every peek now goes directly to the backend.

        Why: on long threads (or threads with rapid turnover), the
        cache misses messages. The polling consumer's `unackedOnly=true`
        fetch caps at `peekMaxLimit` (100) per cycle and never advances
        a cursor, so anything beyond the first 100 unacked never
        reaches the cache. Tools that peek expected to see "everything
        unacked" and instead saw a truncated subset. A direct API call
        per peek is cheap (one Firestore query, bounded by the same
        `peekMaxLimit`) and removes the truncation footgun. The cache
        still exists for the UserPromptSubmit hook's `pending.json`
        read — that's a different code path and its size cap is fine
        for "show recent replies between turns".

        For `thread="*"` (all-threads peek), we still use the in-memory
        cache: the backend has no all-threads endpoint, and iterating
        every known thread would multiply load. The wildcard is rarely
        used in practice and the cache is good enough there.
        """
        # v0.6.1: peeking is an explicit "I expect new traffic" signal —
        # arm the consumer so we start polling at the active cadence
        # (for the hook + pending.json path; not for our direct fetch).
        _arm_polling()

        # All-threads peek: iterate every known thread and direct-API
        # peek each. v0.7.2 dropped the cache-only short-circuit — the
        # in-memory cache was returning stale entries (added by polling
        # but not pruned when another session acked them), so peek
        # results lied about pending state. The trade-off is N API
        # calls instead of 1, where N is len(known_threads); typically
        # 1-3 in practice. Each call is bounded by `peekMaxLimit`.
        if thread == "*":
            if streaming is None or not streaming.known_threads:
                return PeekResult(messages=[], cursor=None, source="api")
            all_msgs: list[Message] = []
            for known_tid in sorted(streaming.known_threads):
                try:
                    data = await api.get(
                        f"/threads/{known_tid}/messages",
                        params={"unackedOnly": "true"},
                    )
                    for raw in _messages_from_api(data):
                        all_msgs.append(Message.model_validate(raw))
                except Exception as exc:
                    # One bad thread shouldn't blank the whole wildcard
                    # peek. Skip + continue.
                    logger.warning(
                        "wildcard peek failed for thread=%s: %s",
                        known_tid, exc,
                    )
                    continue
            return PeekResult(messages=all_msgs, cursor=None, source="api")

        tid = _resolve_thread(state, thread)
        # v0.4.0: peeking on an undeclared thread is the cross-session leak
        # we're preventing — reject rather than auto-add.
        _ensure_known_thread(streaming, tid, auto_add=False)

        # v0.6.2: always direct API. No cache short-circuit, no
        # on-disk pending.json fallback — those layers were the source
        # of the long-thread missing-message bug. The trade-off is one
        # Firestore-bounded query per peek; cheap at solo + small-team
        # scale, and ack-driven flows still naturally batch.
        params: dict[str, Any] = {"unackedOnly": "true"}
        if since_cursor:
            params["since"] = since_cursor
        data = await api.get(f"/threads/{tid}/messages", params=params)
        msgs = [Message.model_validate(m) for m in _messages_from_api(data)]
        cursor = data.get("cursor") if isinstance(data, dict) else None
        return PeekResult(messages=msgs, cursor=cursor, source="api")

    @mcp.tool
    async def ack_messages(
        message_ids: list[str],
        thread: str | None = None,
    ) -> AckResult:
        """Ack (mark processed) one or more messages. Idempotent.

        On HTTP 2xx, the local cache is proactively pruned (§5) so the
        next peek doesn't return stale entries before the SSE confirmation
        round-trips back.
        """
        tid = _resolve_thread(state, thread)
        # v0.4.0: ack on undeclared thread is also a leak vector (you'd be
        # silencing a message you weren't supposed to see). Reject.
        _ensure_known_thread(streaming, tid, auto_add=False)
        await api.post(f"/threads/{tid}/ack", json={"messageIds": message_ids})
        # APIClient raises APIError on non-2xx, so reaching here means HTTP
        # 2xx and we're safe to prune locally.
        if streaming is not None:
            streaming.prune_acked(tid, message_ids)
            # Persist the post-prune cache to the state file so the
            # UserPromptSubmit hook doesn't re-inject already-acked
            # messages on the next prompt. Without this, the state file
            # lags the cache until the SSE round-trip delivers a
            # message.acked event — and then that event no-ops because
            # remove_messages returns 0 (already pruned).
            await streaming.persist_now()
        return AckResult(acked=len(message_ids))

    @mcp.tool
    async def get_messages(
        thread: str | None = None,
    ) -> GetMessagesResult:
        """**Combined peek + ack** — fetch all unacked user replies on
        a thread AND ack them in one tool call. Returns the messages
        so Claude has them in context to respond to.

        Use this when you want to *consume* the current pending replies
        — i.e. you intend to act on them. Use `peek_messages` instead
        when you want a read-only look (e.g. "is there anything new?"
        before deciding whether to ack).

        Equivalent to:
            result = peek_messages(thread=thread)
            ack_messages(message_ids=[m.id for m in result.messages],
                         thread=thread)
            return result.messages

        Safety:
          - If peek returns 0 messages, ack is skipped (no empty POST).
          - If ack fails after peek succeeded, the messages stay
            unacked on the server; next peek will return them again.
            Better than the inverse (losing them to a successful ack
            on a failed read).
        """
        tid = _resolve_thread(state, thread)
        _ensure_known_thread(streaming, tid, auto_add=False)
        _arm_polling()

        # Peek directly via the API (v0.6.2: cache removed for peeks).
        data = await api.get(
            f"/threads/{tid}/messages", params={"unackedOnly": "true"}
        )
        raw_msgs = _messages_from_api(data)
        if not raw_msgs:
            return GetMessagesResult(messages=[], acked=0)

        msgs = [Message.model_validate(m) for m in raw_msgs]
        message_ids = [m.id for m in msgs if m.id]

        # Ack them. APIClient raises on non-2xx, so reaching the post-
        # ack code means HTTP 2xx. If the ack fails (network blip,
        # auth churn) we surface the error to the caller — they need
        # to know the messages are STILL unacked.
        await api.post(f"/threads/{tid}/ack", json={"messageIds": message_ids})

        # Prune the in-memory cache + persist so the hook doesn't
        # re-inject these on the next prompt.
        if streaming is not None:
            streaming.prune_acked(tid, message_ids)
            await streaming.persist_now()

        return GetMessagesResult(messages=msgs, acked=len(message_ids))

    @mcp.tool
    async def get_last_messages(
        thread: str | None = None,
        limit: int = 20,
    ) -> PeekResult:
        """**Context recovery** — fetch the last N user messages on a
        thread, INCLUDING already-acked ones.

        Different from `peek_messages` / `get_messages`, which return
        only *unacked* replies. This tool is for the "what was that
        question I answered earlier?" or "what was the user discussing
        before I lost context?" pattern. It does NOT ack anything; the
        messages stay in whatever state they were already in.

        `limit` defaults to 20 and is server-capped at `peekMaxLimit`
        (100). Returns chronological order (oldest → newest of the
        last-N window) so the caller can read top-to-bottom.

        Use cases:
          - Recovering context after a session restart
          - Pulling thread history for a "what's the gist?" summary
          - Audit / debugging
        """
        tid = _resolve_thread(state, thread)
        _ensure_known_thread(streaming, tid, auto_add=False)
        # No `arm_polling()` — this is a one-off context fetch, not a
        # "watching for new messages" signal. Keeps the consumer in its
        # current state.

        # Clamp the limit before hitting the wire so an obvious caller
        # mistake doesn't burn the server's tighter cap silently.
        safe_limit = max(1, min(int(limit), 100))
        params = {
            "unackedOnly": "false",
            "recent": "true",
            "limit": str(safe_limit),
            "format": "wire",
        }
        data = await api.get(f"/threads/{tid}/messages", params=params)
        msgs = [Message.model_validate(m) for m in _messages_from_api(data)]
        cursor = data.get("cursor") if isinstance(data, dict) else None
        return PeekResult(messages=msgs, cursor=cursor, source="api")

    @mcp.tool
    async def list_threads() -> list[ThreadSummary]:
        """List ALL the user's threads with last-activity timestamps.

        v0.4.0+: each entry includes a `known: bool` indicating whether this
        thread is in the per-session known_threads allowlist. Listing is
        UNFILTERED so Claude can discover thread names; sending or peeking
        on an unknown one then triggers the allowlist check.
        """
        data = await api.get("/threads")
        items = data.get("threads", data) if isinstance(data, dict) else data
        out: list[ThreadSummary] = []
        for it in items or []:
            summary = ThreadSummary.model_validate(it)
            if streaming is not None and streaming.is_thread_known(summary.name):
                summary = summary.model_copy(update={"known": True})
            out.append(summary)
        return out

    @mcp.tool
    async def forget_thread(name: str) -> ForgetThreadResult:
        """Drop `name` from this session's known_threads allowlist (v0.4.0+).

        Subsequent peek_messages/ack_messages on this thread will reject
        until set_thread/send_message re-declares intent. Cached pending for
        this thread is dropped from memory.

        If `name` was the active thread, active_thread is cleared.
        """
        if streaming is None:
            return ForgetThreadResult(name=name, was_known=False)
        was = streaming.forget_known_thread(name)
        # If active thread was this one, also clear from state.json so a
        # fresh MCP launch doesn't auto-set it again.
        if was and state.get() == name:
            update_state({"active_thread": None})
        return ForgetThreadResult(name=name, was_known=was)

    @mcp.tool
    async def list_known_threads() -> ListKnownThreadsResult:
        """Return the in-memory known_threads allowlist (v0.4.0+)."""
        if streaming is None:
            return ListKnownThreadsResult(threads=[])
        return ListKnownThreadsResult(threads=streaming.list_known())

    @mcp.tool
    async def wait_for_response(
        timeout_minutes: float | None = None,
        thread: str | None = None,
        poll_interval_seconds: float | None = None,  # accepted, ignored
    ) -> PeekResult:
        """Block until the user replies on `thread`, or `timeout_minutes`
        elapses.

        Push-driven: awaits `state.state_change` from the SSE consumer.
        `poll_interval_seconds` is accepted for v0.2.4 back-compat and
        ignored — there is no polling in v0.3.0.

        On timeout returns an empty PeekResult (no exception). When the
        SSE link is unavailable (auth_failed, disabled, or backend lacks
        /v1/stream), falls back to a single direct API peek and returns
        whatever's there.
        """
        tid = _resolve_thread(state, thread)
        del poll_interval_seconds  # explicit silence

        timeout = (
            timeout_minutes
            if timeout_minutes is not None
            else config.default_timeout_minutes
        )
        msgs, source = await _await_reply_for_thread(tid, float(timeout))
        return PeekResult(messages=msgs, cursor=None, source=source)

    @mcp.tool
    async def whoami() -> WhoamiResult:
        """Return identity + streaming status + known_threads (debug helper).

        If not yet authorized, this raises an error instructing the caller
        to run `/remotecodetrol:link` first.
        """
        email = await api.auth.whoami()
        return WhoamiResult(
            email=email,
            active_thread=state.get(),
            sse_status=_streaming_status(),
            plugin_version=PLUGIN_VERSION,
            pending_count_by_thread=(
                streaming.pending_count_by_thread() if streaming else {}
            ),
            known_threads=streaming.list_known() if streaming else [],
        )

    @mcp.tool
    async def link() -> LinkResult:
        """Start (or restart) the OAuth device-code authorization flow (v0.4.0+).

        Returns a `user_code`, a deep-link URL, AND an ASCII QR code
        encoding `remotecodetrol://authorize?code=<user_code>`. Show BOTH
        the QR (for scanning in the iOS app's "Authorize new device"
        scanner) AND the user_code (manual fallback).

        After showing them, **wait at least 30 seconds** before calling
        whoami() or any other tool to check completion — the flow is
        human-gated. If the user explicitly says "I confirmed", call
        complete_link() instead — it bypasses cooldowns for an immediate
        answer.
        """
        try:
            email = await api.auth.whoami()
            return LinkResult(
                status="already_linked",
                email=email,
                instructions=(
                    f"Already linked as {email}. Run `logout()` first if "
                    "you want to authorize a different account."
                ),
            )
        except Exception:
            pass

        info = await api.auth.start_device_flow()
        # Persist the device_code so a follow-up complete_link() call (or
        # next auto-completion attempt) can find it without restarting the
        # flow. State key is namespaced to v4 so it doesn't collide with
        # any pre-existing v0.3.x keys.
        update_state({"v4_pending_device_code": info.device_code})

        try:
            qr_ascii = render_qr_ascii(info.deep_link)
        except Exception:
            # qrcode dep missing or render fails — degrade gracefully to
            # text-only display.
            qr_ascii = None

        return LinkResult(
            status="pending_authorization",
            user_code=info.user_code,
            verification_uri=info.verification_uri,
            deep_link=info.deep_link,
            qr_ascii=qr_ascii,
            expires_in_seconds=info.expires_in_seconds,
            instructions=(
                f"Open the RemoteCodetrol iOS app → Settings → "
                f"'Authorize new device'. Either scan the QR with the "
                f"in-app scanner, or enter code `{info.user_code}` "
                f"manually. Wait ≥30s before checking completion; if "
                f"the user explicitly confirms, call `complete_link()` "
                f"for an immediate answer."
            ),
        )

    @mcp.tool
    async def complete_link() -> CompleteLinkResult:
        """Force-poll /v1/oauth/check-link to verify pending authorization.

        Use ONLY when the user explicitly confirms they tapped Confirm
        in the iOS app. Bypasses RFC 8628 polling cooldowns for an
        immediate answer. If you're just curious or impatient, end the
        turn instead — the user's next prompt will trigger natural
        completion.

        Reads the device_code from state.json (set by the prior link()
        call). If no pending flow exists, returns `status="invalid"`
        with a hint to call link() first.
        """
        device_code = read_state().get("v4_pending_device_code")
        if not device_code:
            return CompleteLinkResult(
                status="invalid",
                message=(
                    "No pending device-code flow. Run `link()` first to "
                    "start one."
                ),
            )
        result = await api.auth.complete_link_force(device_code)
        status = result.get("status", "invalid")
        if status == "authorized":
            update_state({"v4_pending_device_code": None})
            email = await api.auth.whoami()
            return CompleteLinkResult(
                status="authorized",
                email=email,
                message=f"Linked as {email}. Ready to use.",
            )
        if status == "pending":
            return CompleteLinkResult(
                status="pending",
                message=(
                    "Still pending — user has not confirmed in the iOS "
                    "app yet. Wait for the user to tap Confirm; do NOT "
                    "loop on this tool."
                ),
            )
        # expired / denied / invalid
        update_state({"v4_pending_device_code": None})
        return CompleteLinkResult(
            status=status,
            message=(
                f"Device-code flow ended: {status}. Run `link()` to "
                "start a fresh one."
            ),
        )

    @mcp.tool
    async def logout() -> LogoutResult:
        """Clear all stored credentials (v0.4.0+).

        Removes the v4 token from `~/Library/Application Support/
        RemoteCodetrol/tokens.json` and clears active_email / pending
        device-code state.
        """
        api.auth.logout()
        update_state({"v4_pending_device_code": None})
        return LogoutResult(
            status="logged_out",
            message=(
                "All credentials cleared. Run /remotecodetrol:link to "
                "authorize again."
            ),
        )

    # Expose every tool to the CLI socket dispatcher (v0.4.0+). Single
    # implementation, two surfaces: stdio MCP via @mcp.tool above, and
    # rcct CLI via this dispatch map.
    _expose("set_thread", set_thread)
    _expose("send_message", send_message)
    _expose("peek_messages", peek_messages)
    _expose("ack_messages", ack_messages)
    _expose("get_messages", get_messages)
    _expose("get_last_messages", get_last_messages)
    _expose("list_threads", list_threads)
    _expose("forget_thread", forget_thread)
    _expose("list_known_threads", list_known_threads)
    _expose("wait_for_response", wait_for_response)
    _expose("whoami", whoami)
    _expose("link", link)
    _expose("complete_link", complete_link)
    _expose("logout", logout)

    return dispatchers
