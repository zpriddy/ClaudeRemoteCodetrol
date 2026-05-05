"""OAuth 2.0 device-code client + macOS Keychain refresh-token storage.

Implements RFC 8628 against the RemoteCodetrol backend (see design doc §6.2).
All token storage is via the `keyring` library so we get OS-appropriate
secure storage (macOS Keychain on this user's machine).

The device-code flow is split across two MCP tool calls so it can be driven
from a chat conversation:

  1. `link()` calls `start_device_flow()` — returns user_code immediately
     and persists the device_code to state.json.
  2. The user authorizes in the iOS app.
  3. The next tool call (`whoami`, `send_message`, …) hits
     `get_access_token()`. With no cached token and no refresh token, it
     finds the pending device_code on disk and calls
     `complete_device_flow_once()` which polls /oauth/token a single time.
     If the user has authorized, tokens are stored and the call proceeds.
     If still pending, the tool raises NotAuthorizedError so Claude can
     prompt the user to wait or re-link.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
import keyring

from .config import Config
from .state import read_state, update_state


ACTIVE_EMAIL_ACCOUNT = "__active_email__"
ACCESS_TOKEN_LEEWAY_SECONDS = 60
PENDING_FLOW_KEY = "pending_device_flow"


class AuthError(RuntimeError):
    """Raised when authentication cannot be completed (terminal failure)."""


class NotAuthorizedError(RuntimeError):
    """Raised when no usable credentials exist and the caller should
    invoke `link()` (or wait for an in-flight authorization to complete)."""

    def __init__(self, message: str, *, pending_user_code: str | None = None):
        super().__init__(message)
        self.pending_user_code = pending_user_code


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: float
    email: str


@dataclass
class DeviceFlowInfo:
    """User-facing payload returned from `start_device_flow`."""

    user_code: str
    verification_uri: str
    expires_in_seconds: int
    interval_seconds: int


def _decode_jwt_unverified(token: str) -> dict[str, Any]:
    """Decode a JWT without verifying the signature.

    We trust TLS for token transport; signature verification is the server's
    job. We only inspect `sub`/`exp` for client-side bookkeeping.
    """
    return jwt.decode(token, options={"verify_signature": False})


def _email_from_access_token(access_token: str) -> str:
    claims = _decode_jwt_unverified(access_token)
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise AuthError("Access token is missing a `sub` claim")
    return sub


def _expiry_from_access_token(access_token: str) -> float:
    claims = _decode_jwt_unverified(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        raise AuthError("Access token is missing an `exp` claim")
    return float(exp)


class TokenStore:
    """Thin wrapper over `keyring` for refresh-token persistence."""

    def __init__(self, service: str):
        self.service = service

    def get_active_email(self) -> str | None:
        return keyring.get_password(self.service, ACTIVE_EMAIL_ACCOUNT)

    def set_active_email(self, email: str) -> None:
        keyring.set_password(self.service, ACTIVE_EMAIL_ACCOUNT, email)

    def get_refresh_token(self, email: str) -> str | None:
        return keyring.get_password(self.service, email)

    def set_refresh_token(self, email: str, refresh_token: str) -> None:
        keyring.set_password(self.service, email, refresh_token)

    def clear(self, email: str) -> None:
        try:
            keyring.delete_password(self.service, email)
        except keyring.errors.PasswordDeleteError:
            pass

    def clear_active_email(self) -> None:
        try:
            keyring.delete_password(self.service, ACTIVE_EMAIL_ACCOUNT)
        except keyring.errors.PasswordDeleteError:
            pass


class AuthClient:
    """OAuth client: device-code flow, refresh, in-memory access-token cache."""

    def __init__(
        self,
        config: Config,
        http: httpx.AsyncClient,
        store: TokenStore | None = None,
    ):
        self.config = config
        self.http = http
        self.store = store or TokenStore(config.keychain_service)
        self._cached: TokenBundle | None = None

    # ---- public API ----

    async def get_access_token(self) -> str:
        """Return a valid access token.

        Order of attempts:
          1. In-memory cache (if not expired)
          2. Refresh-token grant (if a refresh token is in the keychain)
          3. Single poll of any pending device-code flow on disk
          4. Raise NotAuthorizedError — caller should invoke link()
        """
        bundle = self._cached
        now = time.time()
        if bundle and bundle.expires_at - now > ACCESS_TOKEN_LEEWAY_SECONDS:
            return bundle.access_token

        # 2) Try refresh
        email = self.store.get_active_email()
        if email:
            refresh = self.store.get_refresh_token(email)
            if refresh:
                try:
                    bundle = await self._refresh(refresh, email)
                    self._cached = bundle
                    return bundle.access_token
                except AuthError:
                    self.store.clear(email)

        # 3) Try to complete a pending device flow (one shot)
        pending = self._read_pending()
        if pending:
            completed = await self.complete_device_flow_once()
            if completed is not None:
                self._cached = completed
                return completed.access_token
            # still pending — fall through to NotAuthorizedError below
            raise NotAuthorizedError(
                "Authorization is still pending. Open the iOS app and enter "
                f"code {pending['user_code']} in Settings → 'Authorize new "
                "device', then retry. If you've already authorized, wait a "
                "moment and try again — there's a 5s polling interval.",
                pending_user_code=pending["user_code"],
            )

        # 4) No path forward — caller must invoke link()
        raise NotAuthorizedError(
            "Not authorized. Run /remotecodetrol:link (or call the link() "
            "tool) to start the OAuth device-code flow.",
        )

    async def whoami(self) -> str:
        """Email of the active identity (forces a token validation pass)."""
        await self.get_access_token()
        assert self._cached is not None
        return self._cached.email

    def invalidate(self) -> None:
        """Drop the cached access token (e.g. after a 401)."""
        self._cached = None

    def logout(self) -> None:
        """Clear ALL credentials: cache, keychain refresh token, pending flow."""
        self._cached = None
        email = self.store.get_active_email()
        if email:
            self.store.clear(email)
        self.store.clear_active_email()
        self._clear_pending()

    # ---- device-code flow primitives ----

    async def start_device_flow(self) -> DeviceFlowInfo:
        """Initiate a new device-code flow.

        POSTs /oauth/device/code, persists the device_code to state.json
        for the next tool call to consume, and returns the user-facing
        bits (user_code + verification URL).
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
        device_code: str = body["device_code"]
        user_code: str = body["user_code"]
        interval = int(body.get("interval", 5))
        expires_in = int(body.get("expires_in", 600))
        verify = body.get("verification_uri_complete") or (
            f"{body.get('verification_uri', 'https://remotecodetrol.web.app/authorize')}"
            f"?user_code={user_code}"
        )

        update_state({
            PENDING_FLOW_KEY: {
                "device_code": device_code,
                "user_code": user_code,
                "verification_uri": verify,
                "interval": interval,
                "expires_at": time.time() + expires_in,
            }
        })

        # Best-effort stderr breadcrumb for log readers; not user-facing in
        # Claude Code (which only sees the tool return value).
        print(
            f"[remotecodetrol-mcp] device flow started, code={user_code}",
            file=sys.stderr,
            flush=True,
        )

        return DeviceFlowInfo(
            user_code=user_code,
            verification_uri=verify,
            expires_in_seconds=expires_in,
            interval_seconds=interval,
        )

    async def complete_device_flow_once(self) -> TokenBundle | None:
        """Poll /oauth/token ONCE for a pending device flow.

        Returns:
            TokenBundle on success (and stores tokens to keychain).
            None if the user hasn't authorized yet (`authorization_pending`)
              or the server says `slow_down` (treated like pending here —
              the caller can simply re-invoke later).

        Raises:
            AuthError if the flow is terminally failed (`expired_token`,
            `access_denied`) or a network/protocol error occurs. The
            pending state is cleared from disk in that case.
            NotAuthorizedError if there's no pending flow at all.
        """
        pending = self._read_pending()
        if not pending:
            raise NotAuthorizedError("No pending device flow to complete.")

        if time.time() > pending["expires_at"]:
            self._clear_pending()
            raise AuthError(
                "Pending device-code flow expired. Run /remotecodetrol:link "
                "again to start a new one."
            )

        v1 = self.config.api_v1
        resp = await self.http.post(
            f"{v1}/oauth/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": pending["device_code"],
            },
        )
        if resp.status_code == 200:
            payload = resp.json()
            access = payload["access_token"]
            refresh = payload["refresh_token"]
            email = _email_from_access_token(access)
            bundle = TokenBundle(
                access_token=access,
                refresh_token=refresh,
                expires_at=_expiry_from_access_token(access),
                email=email,
            )
            self.store.set_active_email(email)
            self.store.set_refresh_token(email, refresh)
            self._clear_pending()
            return bundle

        try:
            err = resp.json()
        except Exception:
            err = {"error": f"http_{resp.status_code}"}
        code = err.get("error", "")
        if code in ("authorization_pending", "slow_down"):
            return None
        if code in ("expired_token", "access_denied"):
            self._clear_pending()
            raise AuthError(f"device-code flow ended: {code}")
        raise AuthError(
            f"device-code poll error: {code} ({resp.status_code})"
        )

    # ---- internals ----

    async def _refresh(self, refresh_token: str, email: str) -> TokenBundle:
        url = f"{self.config.api_v1}/oauth/token"
        resp = await self.http.post(
            url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        if resp.status_code != 200:
            raise AuthError(
                f"refresh failed: {resp.status_code} {resp.text[:200]}"
            )
        payload = resp.json()
        new_refresh = payload.get("refresh_token", refresh_token)
        access = payload["access_token"]
        bundle = TokenBundle(
            access_token=access,
            refresh_token=new_refresh,
            expires_at=_expiry_from_access_token(access),
            email=email,
        )
        # Refresh tokens rotate on every use; persist the new one.
        self.store.set_refresh_token(email, new_refresh)
        return bundle

    @staticmethod
    def _read_pending() -> dict[str, Any] | None:
        return read_state().get(PENDING_FLOW_KEY)

    @staticmethod
    def _clear_pending() -> None:
        update_state({PENDING_FLOW_KEY: None})
