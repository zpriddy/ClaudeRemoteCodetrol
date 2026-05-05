"""@mcp.tool implementations for the RemoteCodetrol MCP server.

Each tool is a thin wrapper around APIClient. Thread resolution priority:
  1. explicit `thread=` parameter on the call
  2. value previously stored via `set_thread`
  3. REMOTECODETROL_THREAD env var (read into Config at startup)
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, Field

from .client import APIClient
from .config import Config
from .state import read_state, update_state


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


class SendResult(BaseModel):
    message_id: str = Field(alias="messageId")
    thread: str

    model_config = {"populate_by_name": True}


class Message(BaseModel):
    # Backend returns `messageId`; we expose it as `id` for Claude. The
    # alias lets us still parse the wire format. populate_by_name = True
    # means either input shape works (forwards/backwards compatible).
    id: str = Field(alias="messageId")
    body: str
    sender_type: str = Field(alias="senderType")
    created_at: str = Field(alias="createdAt")
    acked_at: str | None = Field(default=None, alias="ackedAt")

    model_config = {"populate_by_name": True}


class PeekResult(BaseModel):
    messages: list[Message]
    cursor: str | None = None


class AckResult(BaseModel):
    acked: int


class ThreadSummary(BaseModel):
    name: str
    last_message_at: str | None = Field(default=None, alias="lastMessageAt")

    model_config = {"populate_by_name": True}


class WhoamiResult(BaseModel):
    email: str
    default_thread: str | None


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


# ---------- tool registration ----------


def register_tools(mcp: Any, api: APIClient, state: ThreadState, config: Config) -> None:
    """Bind all @mcp.tool functions onto the given FastMCP instance.

    We register at runtime instead of at import time so tests can inject a
    fake APIClient.
    """

    @mcp.tool
    async def set_thread(name: str) -> SetThreadResult:
        """Set the active thread for subsequent send_message / peek / ack calls.

        The value is persisted to ~/.config/remotecodetrol/state.json so it
        survives MCP restarts.
        """
        state.set(name)
        return SetThreadResult(active_thread=name)

    @mcp.tool
    async def send_message(
        body: str,
        require_response: bool = False,
        thread: str | None = None,
        idempotency_key: str | None = None,
    ) -> SendResult:
        """Send a message to the user via the RemoteCodetrol iOS app.

        Set `require_response=True` when you need a reply before continuing
        (e.g. before destructive ops, decisions, etc.). After sending, call
        `wait_for_response` to poll until the user replies.
        """
        tid = _resolve_thread(state, thread)
        payload: dict[str, Any] = {"body": body, "requireResponse": require_response}
        if idempotency_key:
            payload["idempotencyKey"] = idempotency_key
        data = await api.post(f"/threads/{tid}/messages", json=payload)
        return SendResult.model_validate({**data, "thread": tid})

    @mcp.tool
    async def peek_messages(
        since_cursor: str | None = None,
        thread: str | None = None,
    ) -> PeekResult:
        """Peek unacked user messages on a thread.

        Crash-safe: messages stay returnable until you call `ack_messages`.
        """
        tid = _resolve_thread(state, thread)
        params: dict[str, Any] = {"unackedOnly": "true"}
        if since_cursor:
            params["since"] = since_cursor
        data = await api.get(f"/threads/{tid}/messages", params=params)
        return PeekResult.model_validate(data or {"messages": [], "cursor": None})

    @mcp.tool
    async def ack_messages(
        message_ids: list[str],
        thread: str | None = None,
    ) -> AckResult:
        """Ack (mark processed) one or more messages. Idempotent."""
        tid = _resolve_thread(state, thread)
        await api.post(f"/threads/{tid}/ack", json={"messageIds": message_ids})
        return AckResult(acked=len(message_ids))

    @mcp.tool
    async def list_threads() -> list[ThreadSummary]:
        """List the user's threads with last-activity timestamps."""
        data = await api.get("/threads")
        items = data.get("threads", data) if isinstance(data, dict) else data
        return [ThreadSummary.model_validate(it) for it in (items or [])]

    @mcp.tool
    async def wait_for_response(
        timeout_minutes: int | None = None,
        poll_interval_seconds: int | None = None,
        thread: str | None = None,
    ) -> PeekResult:
        """Block until the user replies or `timeout_minutes` elapses.

        Loops `peek_messages` -> `ack_messages` -> return. On timeout returns
        an empty PeekResult (no exception) so the caller can decide what to
        do next.
        """
        tid = _resolve_thread(state, thread)
        timeout = timeout_minutes if timeout_minutes is not None else config.default_timeout_minutes
        interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else config.default_poll_interval_seconds
        )
        deadline = time.monotonic() + timeout * 60
        while True:
            data = await api.get(
                f"/threads/{tid}/messages", params={"unackedOnly": "true"}
            )
            result = PeekResult.model_validate(
                data or {"messages": [], "cursor": None}
            )
            if result.messages:
                ids = [m.id for m in result.messages]
                await api.post(f"/threads/{tid}/ack", json={"messageIds": ids})
                return result
            if time.monotonic() >= deadline:
                return PeekResult(messages=[], cursor=None)
            await asyncio.sleep(min(interval, max(1, deadline - time.monotonic())))

    @mcp.tool
    async def whoami() -> WhoamiResult:
        """Return the logged-in email and active thread (debug helper).

        If not yet authorized, this raises an error instructing the caller
        to run `/remotecodetrol:link` first. It does NOT block waiting for
        authorization (that's `link`'s job).
        """
        email = await api.auth.whoami()
        return WhoamiResult(email=email, default_thread=state.get())

    @mcp.tool
    async def link() -> LinkResult:
        """Start (or restart) the OAuth device-code authorization flow.

        Returns a `user_code` and `verification_uri` to show the user.
        They authorize in the iOS app (Settings → 'Authorize new device'),
        and the next call to any tool will detect completed authorization
        and proceed normally.

        If already authorized, returns status='already_linked' with the
        current email — no action needed.
        """
        # Fast-path: are we already authorized?
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
            # Fall through to start a fresh flow.
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
        the macOS Keychain, and clears any pending device-code flow. The
        next tool call will require a fresh `link()`.
        """
        api.auth.logout()
        return LogoutResult(
            status="logged_out",
            message=(
                "All credentials cleared. Run /remotecodetrol:link to "
                "authorize again."
            ),
        )
