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
import time
from typing import Any

from pydantic import BaseModel, Field

from . import __version__ as PLUGIN_VERSION
from .client import APIClient, APIError
from .config import Config
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


class ThreadSummary(BaseModel):
    name: str
    last_message_at: str | None = Field(default=None, alias="lastMessageAt")

    model_config = {"populate_by_name": True}


class WhoamiResult(BaseModel):
    email: str
    active_thread: str | None
    sse_status: str
    plugin_version: str
    pending_count_by_thread: dict[str, int] = Field(default_factory=dict)


class LinkResult(BaseModel):
    """Returned from link(). The user_code + verification_uri are what
    Claude should display to the user. After they authorize, calling any
    other tool (whoami, send_message, …) will pick up the new tokens."""

    status: str  # "pending_authorization" | "already_linked"
    user_code: str | None = None
    verification_uri: str | None = None
    expires_in_seconds: int | None = None
    instructions: str
    email: str | None = None  # set when status == "already_linked"


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
) -> None:
    """Bind all @mcp.tool functions onto the given FastMCP instance.

    `streaming` is optional — when None (or when the SSE consumer is
    `disabled` / `auth_failed`), tools fall back to direct API calls only.
    """

    # Internal sentinel — when no streaming state is provided, behave as
    # if the cache is permanently stale so every read goes to the API.
    def _cache_fresh() -> bool:
        return streaming is not None and streaming.cache_is_fresh()

    def _streaming_status() -> str:
        return streaming.sse_status if streaming else "disabled"

    @mcp.tool
    async def set_thread(name: str) -> SetThreadResult:
        """Set the active thread for subsequent send_message / peek / ack calls.

        The value is persisted to ~/.config/remotecodetrol/state.json so it
        survives MCP restarts.
        """
        state.set(name)
        if streaming is not None:
            streaming.active_thread = name
        return SetThreadResult(active_thread=name)

    @mcp.tool
    async def send_message(
        body: str,
        require_response: bool = False,
        thread: str | None = None,
        idempotency_key: str | None = None,
    ) -> SendResult:
        """Send a message to the user via the RemoteCodetrol iOS app.

        Set `require_response=True` when you need a reply before continuing.
        The response now bundles `pending_messages` (any unacked replies on
        the thread at send time), so Claude usually doesn't need to call
        peek separately.
        """
        tid = _resolve_thread(state, thread)
        payload: dict[str, Any] = {"body": body, "requireResponse": require_response}
        if idempotency_key:
            payload["idempotencyKey"] = idempotency_key
        data = await api.post(f"/threads/{tid}/messages", json=payload)
        normalized = _normalize_send_response(data)
        msg_id = normalized.get("messageId") or normalized.get("message_id")
        raw_pending = normalized.get("pending_messages") or []
        pending = [Message.model_validate(m) for m in raw_pending]
        count = normalized.get("pending_count")
        if count is None:
            count = len(pending)
        return SendResult(
            message_id=msg_id or "",
            thread=tid,
            pending_messages=pending,
            pending_count=int(count),
        )

    @mcp.tool
    async def peek_messages(
        thread: str | None = None,
        since_cursor: str | None = None,
    ) -> PeekResult:
        """Return unacked user messages.

        If `thread="*"`, returns pending across all threads (cache only —
        the backend has no all-threads peek endpoint). Otherwise resolves
        a single thread and reads cache-first, falling back to a direct
        API call when the cache is stale.

        `since_cursor` is accepted for v0.2.4 back-compat; with the
        cache-first model it's only honored on the API fallback path.
        """
        # All-threads peek: cache-only. Server has no endpoint for this,
        # and the SSE snapshot already covers every thread the user owns.
        if thread == "*":
            if streaming is None:
                return PeekResult(messages=[], cursor=None, source="cache")
            msgs = [Message.model_validate(m) for m in streaming.all_pending()]
            return PeekResult(messages=msgs, cursor=None, source="cache")

        tid = _resolve_thread(state, thread)

        if _cache_fresh() and streaming is not None:
            cached = streaming.pending.get(tid, [])
            msgs = [Message.model_validate(m) for m in cached]
            return PeekResult(messages=msgs, cursor=None, source="cache")

        # Stale (or no streaming) → direct API call.
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
        await api.post(f"/threads/{tid}/ack", json={"messageIds": message_ids})
        # APIClient raises APIError on non-2xx, so reaching here means HTTP
        # 2xx and we're safe to prune locally.
        if streaming is not None:
            streaming.prune_acked(tid, message_ids)
        return AckResult(acked=len(message_ids))

    @mcp.tool
    async def list_threads() -> list[ThreadSummary]:
        """List the user's threads with last-activity timestamps."""
        data = await api.get("/threads")
        items = data.get("threads", data) if isinstance(data, dict) else data
        return [ThreadSummary.model_validate(it) for it in (items or [])]

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
        deadline = time.monotonic() + float(timeout) * 60

        # If we already have something cached, return immediately.
        if streaming is not None and streaming.pending.get(tid):
            cached = streaming.pending.get(tid, [])
            msgs = [Message.model_validate(m) for m in cached]
            return PeekResult(messages=msgs, cursor=None, source="cache")

        # Streaming unavailable → single direct-API check, no polling.
        if streaming is None or streaming.sse_status in ("auth_failed", "disabled"):
            data = await api.get(
                f"/threads/{tid}/messages", params={"unackedOnly": "true"}
            )
            msgs = [Message.model_validate(m) for m in _messages_from_api(data)]
            cursor = data.get("cursor") if isinstance(data, dict) else None
            return PeekResult(messages=msgs, cursor=cursor, source="api")

        # Otherwise wait on the state-change event. We clear() before each
        # wait so a stale "set" left over from a prior mutation doesn't
        # cause spurious wake loops.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return PeekResult(messages=[], cursor=None, source="cache")
            streaming.state_change.clear()
            # Re-check after clearing — if a mutation happened between the
            # last cache read and now, we'd otherwise miss it.
            if streaming.sse_status == "auth_failed":
                return PeekResult(messages=[], cursor=None, source="cache")
            cached = streaming.pending.get(tid, [])
            if cached:
                msgs = [Message.model_validate(m) for m in cached]
                return PeekResult(messages=msgs, cursor=None, source="cache")
            try:
                await asyncio.wait_for(streaming.state_change.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return PeekResult(messages=[], cursor=None, source="cache")
            # Loop and re-check after wakeup. Spurious wakes (mutation on a
            # different thread) just retry the cache lookup.

    @mcp.tool
    async def whoami() -> WhoamiResult:
        """Return identity + streaming status (debug helper).

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
        )

    @mcp.tool
    async def link() -> LinkResult:
        """Start (or restart) the OAuth device-code authorization flow.

        Returns a `user_code` and `verification_uri` to show the user.
        They authorize in the iOS app (Settings → 'Authorize new device'),
        and the next call to any tool will detect completed authorization
        and proceed normally.
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
        return LinkResult(
            status="pending_authorization",
            user_code=info.user_code,
            verification_uri=info.verification_uri,
            expires_in_seconds=info.expires_in_seconds,
            instructions=(
                f"Open the RemoteCodetrol iOS app → Settings → "
                f"'Authorize new device' → enter code {info.user_code} → "
                "tap Confirm. Then run `whoami()` (or any other tool) to "
                "verify the link succeeded."
            ),
        )

    @mcp.tool
    async def logout() -> LogoutResult:
        """Clear all stored credentials.

        Removes the cached access token, deletes the refresh token from
        the macOS Keychain, and clears any pending device-code flow.
        """
        api.auth.logout()
        return LogoutResult(
            status="logged_out",
            message=(
                "All credentials cleared. Run /remotecodetrol:link to "
                "authorize again."
            ),
        )
