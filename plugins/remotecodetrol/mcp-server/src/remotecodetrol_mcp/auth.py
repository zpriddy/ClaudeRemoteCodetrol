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
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import keyring  # kept for one-time migration from keychain (v0.3.5)

from .config import Config
from .state import read_state, update_state


logger = logging.getLogger("remotecodetrol_mcp.auth")


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


def _default_token_file_path() -> Path:
    """Stable, plugin-version-independent path for the tokens file.

    macOS Keychain ACLs are bound to the calling binary's path. Plugin
    reinstalls put the MCP at a new path each time
    (`/cache/.../0.3.X/mcp-server/...`), so the new binary loses access
    to keychain entries written by previous versions. By storing tokens
    in a path-stable JSON file under the user's data dir, we avoid that
    re-link-after-every-update churn.

    Trade-off: file is plain JSON with `chmod 600` rather than encrypted
    at rest. For the threat model (single-user dev machine, FileVault),
    this is equivalent — anything running as the user can prompt+read
    keychain too. See SKILL.md / commit message for the rationale.
    """
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "RemoteCodetrol"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg else Path.home() / ".config"
        base = base / "remotecodetrol"
    return base / "tokens.json"


class TokenStore:
    """File-based refresh-token persistence at a path-stable location.

    v0.3.5+: switched from `keyring` to a JSON file because macOS Keychain
    ACLs are bound to the calling binary's path. Plugin reinstalls put
    the MCP at a new versioned path each time, and the new binary loses
    access to the existing keychain entry — forcing a re-link on every
    plugin update. File-based storage is path-stable.

    On first read, transparently migrates any existing keychain entries
    over to the file (one-time, best-effort). After migration, the
    keychain entries are deleted to avoid stale duplication.
    """

    def __init__(self, service: str, path: Path | None = None):
        self.service = service
        self.path = path or _default_token_file_path()
        # Migration is lazy — on first read — so construction stays
        # side-effect-free for tests.
        self._migrated = False

    # ---- public API (unchanged signature) ----

    def get_active_email(self) -> str | None:
        return self._read().get("active_email") or None

    def set_active_email(self, email: str) -> None:
        data = self._read()
        data["active_email"] = email
        self._write(data)

    def get_refresh_token(self, email: str) -> str | None:
        tokens = self._read().get("tokens")
        if not isinstance(tokens, dict):
            return None
        val = tokens.get(email)
        return val if isinstance(val, str) and val else None

    def set_refresh_token(self, email: str, refresh_token: str) -> None:
        data = self._read()
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            tokens = {}
            data["tokens"] = tokens
        tokens[email] = refresh_token
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

    # ---- file I/O ----

    def _read(self) -> dict[str, Any]:
        if not self._migrated:
            self._migrated = True
            self._maybe_migrate_from_keychain()
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("TokenStore: read of %s failed: %s", self.path, e)
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        """Atomic, 0600-permissioned write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass  # Best-effort; defense in depth.
        # Atomic: write to .tmp, fsync, rename. Open with 0600 from the
        # start so the temp file is never world-readable mid-write.
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    # ---- one-time migration from keychain ----

    def _maybe_migrate_from_keychain(self) -> None:
        """One-time, best-effort migration of existing keychain entries.

        If the tokens file already exists, we're past migration. Otherwise
        try to read keychain; if there's a usable entry, write it to the
        file and delete the keychain copy so we don't leave duplicates.
        """
        if self.path.exists():
            return
        try:
            active = keyring.get_password(self.service, ACTIVE_EMAIL_ACCOUNT)
            if not active:
                return
            refresh = keyring.get_password(self.service, active)
            if not refresh:
                return
            self._write(
                {"active_email": active, "tokens": {active: refresh}}
            )
            logger.info(
                "TokenStore: migrated tokens from keychain to %s (one-time)",
                self.path,
            )
            for account in (ACTIVE_EMAIL_ACCOUNT, active):
                try:
                    keyring.delete_password(self.service, account)
                except keyring.errors.PasswordDeleteError:
                    pass
                except Exception as e:
                    logger.debug(
                        "TokenStore: post-migration keychain cleanup failed (%s)", e
                    )
        except Exception as e:
            # Keychain might be inaccessible (path-ACL is the very issue
            # we're fixing). Don't crash; user just re-links once.
            logger.info("TokenStore: keychain migration skipped (%s)", e)


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
