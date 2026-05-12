"""v4 (plugin v0.4.0+) auth: single 14-day opaque token, file-stored.

Replaces the v0.3.x access+refresh JWT model with a single opaque token
issued by the backend at /v1/oauth/token (device-code grant) or
/v1/oauth/check-link (force-poll). Renewed via /v1/oauth/rotate, which
returns a fresh token AND keeps the old one valid for a 60s grace
window — see backend spec §3.5.

Token storage: ~/Library/Application Support/RemoteCodetrol/tokens.json
(macOS) / ${XDG_CONFIG_HOME:-~/.config}/remotecodetrol/tokens.json
(linux). Permission 0600 on file; 0700 on parent dir. Schema:

  {
    "schema_version": 4,
    "active_email": "user@example.com",   # optional; populated post-link
    "tokens": {
      "user@example.com": {
        "token": "<plaintext>",
        "issued_at": <unix-seconds>,
        "expires_at": <unix-seconds>,     # issued_at + 14d
        "rotates_at": <unix-seconds>,     # issued_at + 7d
        "last_rotation_attempt_at": <unix-seconds> | null
      }
    }
  }

v0.3.x token files (no schema_version, or values are JWT/refresh-token
strings) are DETECTED ON LOAD and DISCARDED — we rewrite the file as an
empty v4 shape and the next tool call surfaces NotAuthorizedError so
Claude prompts a re-link. There is no migration code path.

Background rotation: when expires_at - now < 7d, kick off a non-blocking
refresh task. On failure, retry hourly for the next 7 days (still well
inside the token's remaining lifetime). User-facing tool calls never
block on rotation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import Config


logger = logging.getLogger("remotecodetrol_mcp.auth")


SCHEMA_VERSION = 4

# Sentinel: the in-memory cache of the current token. We re-read from disk
# on first use after MCP launch so a Claude session that re-uses the MCP
# process across `link()` invocations sees the freshly-written token.
_TOKEN_TTL_LEEWAY_SECONDS = 60

# Background rotation: tighter retry window for the day-7+ phase.
_ROTATION_RETRY_INTERVAL_SECONDS = 60 * 60  # 1 hour


class AuthError(RuntimeError):
    """Raised when authentication cannot be completed (terminal failure)."""


class NotAuthorizedError(RuntimeError):
    """No usable credentials. Caller should invoke link()."""

    def __init__(self, message: str, *, pending_user_code: str | None = None):
        super().__init__(message)
        self.pending_user_code = pending_user_code


@dataclass
class StoredToken:
    """Mirrors one entry of `tokens.json::tokens.<email>`."""

    token: str
    issued_at: float
    expires_at: float
    rotates_at: float
    last_rotation_attempt_at: float | None = None


@dataclass
class DeviceFlowInfo:
    """User-facing payload returned from `start_device_flow`."""

    user_code: str
    verification_uri: str
    deep_link: str
    expires_in_seconds: int
    interval_seconds: int
    device_code: str  # opaque; persisted via state for /check-link to consume


def _default_token_file_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "RemoteCodetrol"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        base = base / "remotecodetrol"
    return base / "tokens.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically with 0600 perms; chmod parent 0700 best-effort."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


class TokenStore:
    """File-backed v4 token store.

    On first read, detects pre-v4 shape and DISCARDS it (overwrites file
    with an empty v4 shape). This is the "no migration" decision from
    spec §1 / §8 — old links are abandoned, users re-link.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or _default_token_file_path()
        # In-memory mirror so we don't re-read the JSON file on every call.
        self._cache: dict[str, Any] | None = None
        # Lock to serialize writes (multiple in-flight rotations etc.).
        self._lock = asyncio.Lock()

    # ---- public API ----

    def get_active_email(self) -> str | None:
        return self._read().get("active_email") or None

    def set_active_email(self, email: str) -> None:
        data = self._read()
        data["active_email"] = email
        self._write(data)

    def get_token(self, email: str) -> StoredToken | None:
        tokens = self._read().get("tokens")
        if not isinstance(tokens, dict):
            return None
        entry = tokens.get(email)
        if not isinstance(entry, dict):
            return None
        try:
            return StoredToken(
                token=str(entry["token"]),
                issued_at=float(entry["issued_at"]),
                expires_at=float(entry["expires_at"]),
                rotates_at=float(entry["rotates_at"]),
                last_rotation_attempt_at=(
                    float(entry["last_rotation_attempt_at"])
                    if entry.get("last_rotation_attempt_at") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def store_token(
        self,
        email: str,
        token: str,
        expires_at: float,
        rotates_at: float,
        issued_at: float | None = None,
    ) -> None:
        data = self._read()
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            tokens = {}
            data["tokens"] = tokens
        tokens[email] = {
            "token": token,
            "issued_at": issued_at if issued_at is not None else time.time(),
            "expires_at": expires_at,
            "rotates_at": rotates_at,
            "last_rotation_attempt_at": None,
        }
        self._write(data)

    def mark_rotation_attempt(self, email: str) -> None:
        data = self._read()
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            return
        entry = tokens.get(email)
        if not isinstance(entry, dict):
            return
        entry["last_rotation_attempt_at"] = time.time()
        self._write(data)

    def clear(self, email: str) -> None:
        data = self._read()
        tokens = data.get("tokens")
        if isinstance(tokens, dict):
            tokens.pop(email, None)
        self._write(data)

    def clear_active_email(self) -> None:
        data = self._read()
        data.pop("active_email", None)
        self._write(data)

    def clear_all(self) -> None:
        self._write({"schema_version": SCHEMA_VERSION, "tokens": {}})

    # ---- internals ----

    def _read(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = {"schema_version": SCHEMA_VERSION, "tokens": {}}
            return self._cache
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("TokenStore: read of %s failed: %s — discarding", self.path, e)
            data = {}
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            # Pre-v4 (or corrupt). Overwrite with empty v4 shape so we never
            # mis-interpret old data as v4 tokens.
            logger.info(
                "TokenStore: pre-v4 token file at %s — discarding (per spec §8)",
                self.path,
            )
            self._cache = {"schema_version": SCHEMA_VERSION, "tokens": {}}
            self._write(self._cache)
            return self._cache
        # Ensure required top-level keys exist.
        if "tokens" not in data or not isinstance(data["tokens"], dict):
            data["tokens"] = {}
        self._cache = data
        return self._cache

    def _write(self, data: dict[str, Any]) -> None:
        self._cache = data
        _atomic_write_json(self.path, data)


class AuthClient:
    """v4 auth client: token validation, background rotation, device-flow link."""

    def __init__(
        self,
        config: Config,
        http: httpx.AsyncClient,
        store: TokenStore | None = None,
    ):
        self.config = config
        self.http = http
        self.store = store or TokenStore()
        # Single in-flight rotation guard (per email — but in practice we have
        # one active email at a time, so a single asyncio.Lock suffices).
        self._rotation_lock = asyncio.Lock()
        # Set after the first successful rotation/check-link/etc., so the
        # background-refresh loop knows to look at this email.
        self._active_email: str | None = self.store.get_active_email()

    # ---- public surface ----

    async def get_access_token(self) -> str:
        """Return a usable token. Triggers background rotation if past day 7."""
        email = self._active_email or self.store.get_active_email()
        if not email:
            raise NotAuthorizedError(
                "Not authorized. Run /remotecodetrol:link to start the OAuth "
                "device-code flow."
            )
        stored = self.store.get_token(email)
        if not stored:
            raise NotAuthorizedError(
                f"No token stored for {email}. Run /remotecodetrol:link."
            )
        now = time.time()
        if stored.expires_at - now <= _TOKEN_TTL_LEEWAY_SECONDS:
            # Token is essentially expired — try a synchronous rotation as a
            # last-ditch effort, but don't crash if it fails (caller can
            # always re-link).
            try:
                stored = await self._rotate_now(email, stored)
            except AuthError:
                self.store.clear(email)
                raise NotAuthorizedError(
                    "Token expired and rotation failed. Re-link via "
                    "/remotecodetrol:link."
                )
        elif now >= stored.rotates_at:
            # Past day-7 window. Spawn a background rotation if we haven't
            # tried in the last hour. Returns the still-valid current token
            # immediately; the next call after rotation succeeds will see
            # the new one.
            self._maybe_spawn_background_rotation(email, stored)
        return stored.token

    async def whoami(self) -> str:
        await self.get_access_token()
        email = self._active_email or self.store.get_active_email()
        if not email:
            raise AuthError("Inconsistent state: token loaded but no active email")
        return email

    def invalidate(self) -> None:
        """Hint that the current token may be revoked. Drops nothing on disk
        (the next call will re-read from disk). Provided for API parity with
        the v0.3.x AuthClient — the v4 token model has no in-memory cache to
        clear because we don't decode JWT claims."""
        # In v0.3.x this dropped the access-token cache; v4 doesn't have one.
        # Keep the method as a no-op so APIClient's retry logic still calls it.
        return None

    def logout(self) -> None:
        """Clear ALL credentials."""
        email = self._active_email or self.store.get_active_email()
        if email:
            self.store.clear(email)
        self.store.clear_active_email()
        self._active_email = None

    # ---- device-code link flow ----

    async def start_device_flow(self) -> DeviceFlowInfo:
        """POST /v1/oauth/device/code; return user_code + deep_link + device_code.

        The device_code is opaque and is the credential later passed to
        /v1/oauth/check-link or /v1/oauth/token to complete the flow. Caller
        is responsible for passing it back when the user confirms.
        """
        v1 = self.config.api_v1
        resp = await self.http.post(
            f"{v1}/oauth/device/code",
            data={"device_label": self.config.device_label},
        )
        if resp.status_code != 200:
            raise AuthError(
                f"device-code start failed: {resp.status_code} {resp.text[:200]}"
            )
        body = resp.json()
        user_code = body["user_code"]
        device_code = body["device_code"]
        interval = int(body.get("interval", 5))
        expires_in = int(body.get("expires_in", 600))
        verification_uri = body.get("verification_uri_complete") or (
            f"{body.get('verification_uri', 'https://remotecodetrol.web.app/authorize')}"
            f"?user_code={user_code}"
        )
        deep_link = f"remotecodetrol://authorize?code={user_code}"

        return DeviceFlowInfo(
            user_code=user_code,
            verification_uri=verification_uri,
            deep_link=deep_link,
            expires_in_seconds=expires_in,
            interval_seconds=interval,
            device_code=device_code,
        )

    async def complete_link_force(self, device_code: str) -> dict[str, Any]:
        """Force-poll /v1/oauth/check-link.

        Bypasses both client-side and server-side polling cooldowns. Use
        when the user explicitly confirms in-app authorization. Returns
        the raw JSON response so the caller can surface status + token to
        Claude. On `authorized`, persists the v4 token to disk.
        """
        v1 = self.config.api_v1
        resp = await self.http.post(
            f"{v1}/oauth/check-link",
            json={
                "device_code": device_code,
                "device_label": self.config.device_label,
            },
        )
        if resp.status_code in (200,):
            body = resp.json()
            if body.get("status") == "authorized":
                self._persist_fresh_token(body)
            return body
        if resp.status_code == 410:
            try:
                return resp.json()
            except Exception:
                return {"status": "expired"}
        raise AuthError(
            f"check-link failed: {resp.status_code} {resp.text[:200]}"
        )

    async def complete_link_via_token_grant(self, device_code: str) -> dict[str, Any]:
        """Backwards-compatible completion via /v1/oauth/token (device_code grant).

        Used when the caller wants to respect RFC 8628 polling semantics
        rather than force-polling via check-link. Most tool flows should
        use complete_link_force when the user has explicitly confirmed.
        """
        v1 = self.config.api_v1
        resp = await self.http.post(
            f"{v1}/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "device_label": self.config.device_label,
            },
        )
        if resp.status_code == 200:
            body = resp.json()
            self._persist_fresh_token(body)
            return {"status": "authorized", **body}
        try:
            err = resp.json()
        except Exception:
            err = {"error": f"http_{resp.status_code}"}
        code = err.get("error", "")
        if code == "authorization_pending":
            return {"status": "pending"}
        if code == "slow_down":
            return {"status": "pending"}
        if code in ("expired_token", "access_denied"):
            return {"status": "expired" if code == "expired_token" else "denied"}
        raise AuthError(f"token grant error: {code} ({resp.status_code})")

    # ---- rotation internals ----

    def _persist_fresh_token(self, body: dict[str, Any]) -> None:
        """Write a freshly issued v4 token to the store and set active_email.

        Called after a successful /token or /check-link response.

        v0.4.3+: backend now returns `email` in the response body (looked up
        from `users/{uid}.email` on the backend). We use it when present and
        fall back to the existing active_email or "default" for compatibility
        with backends that don't yet return it.
        """
        token = body["token"]
        expires_in = int(body.get("expires_in", self.config.mcp_token_ttl_sec))
        rotates_after = int(
            body.get("rotates_after", self.config.mcp_token_rotate_after_sec)
        )
        now = time.time()
        # Prefer the email the backend returned; fall back to the previously-
        # active email; final fallback "default" matches v0.4.0–0.4.2 behavior.
        email = body.get("email") or self._active_email or "default"

        # If the email changed (e.g. previously stored under "default" and the
        # backend now told us the real one), drop the old token entry so we
        # don't leak a stale "default" record alongside the real one.
        prior_email = self._active_email or self.store.get_active_email()
        if prior_email and prior_email != email:
            self.store.clear(prior_email)

        self.store.store_token(
            email=email,
            token=token,
            expires_at=now + expires_in,
            rotates_at=now + rotates_after,
            issued_at=now,
        )
        self.store.set_active_email(email)
        self._active_email = email

    async def _rotate_now(self, email: str, current: StoredToken) -> StoredToken:
        """Synchronously rotate the current token to a fresh one.

        Single in-flight guard — concurrent callers serialize and only the
        first actually performs the rotation.
        """
        async with self._rotation_lock:
            # Re-read after acquiring the lock; another coroutine may have
            # just rotated.
            stored = self.store.get_token(email) or current
            if stored.token != current.token:
                # Someone else rotated under us. Use the fresh token.
                return stored
            self.store.mark_rotation_attempt(email)
            v1 = self.config.api_v1
            resp = await self.http.post(
                f"{v1}/oauth/rotate",
                headers={"Authorization": f"Bearer {stored.token}"},
            )
            if resp.status_code != 200:
                raise AuthError(
                    f"rotate failed: {resp.status_code} {resp.text[:200]}"
                )
            body = resp.json()
            new_token = body["token"]
            expires_in = int(body.get("expires_in", self.config.mcp_token_ttl_sec))
            rotates_after = int(
                body.get("rotates_after", self.config.mcp_token_rotate_after_sec)
            )
            now = time.time()
            self.store.store_token(
                email=email,
                token=new_token,
                expires_at=now + expires_in,
                rotates_at=now + rotates_after,
                issued_at=now,
            )
            return self.store.get_token(email) or stored

    def _maybe_spawn_background_rotation(
        self, email: str, current: StoredToken
    ) -> None:
        """Fire-and-forget rotation. Throttled to once per hour per email."""
        last = current.last_rotation_attempt_at
        now = time.time()
        if last and (now - last) < _ROTATION_RETRY_INTERVAL_SECONDS:
            return
        # Don't await — non-blocking.
        asyncio.get_event_loop().create_task(
            self._background_rotate_silently(email, current)
        )

    async def _background_rotate_silently(
        self, email: str, current: StoredToken
    ) -> None:
        try:
            await self._rotate_now(email, current)
            logger.info("auth: background rotation succeeded for %s", email)
        except Exception as e:
            logger.warning("auth: background rotation failed for %s: %s", email, e)
            # Don't clear the token — current token still works for ~7 more
            # days. Background loop will retry hourly per the throttle.
