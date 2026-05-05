"""HTTP client for the RemoteCodetrol REST API.

Wraps httpx with bearer-auth injection and one automatic retry on 401
(which forces a token refresh / device-code re-auth).
"""

from __future__ import annotations

from typing import Any

import httpx

from .auth import AuthClient
from .config import Config


class APIError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"API {status}: {body[:300]}")
        self.status = status
        self.body = body


class APIClient:
    def __init__(self, config: Config, auth: AuthClient, http: httpx.AsyncClient):
        self.config = config
        self.auth = auth
        self.http = http

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.config.api_v1}{path}"
        for attempt in (0, 1):
            token = await self.auth.get_access_token()
            resp = await self.http.request(
                method,
                url,
                json=json,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code == 401 and attempt == 0:
                # Token might be revoked or rotated server-side — drop cache and retry.
                self.auth.invalidate()
                continue
            if resp.status_code >= 400:
                raise APIError(resp.status_code, resp.text)
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise APIError(401, "exhausted retries on 401")

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)
