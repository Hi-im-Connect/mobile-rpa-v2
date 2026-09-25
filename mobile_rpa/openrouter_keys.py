"""OpenRouter key management: the dashboard gives every phone its own capped key.

Needs the account's management key (openrouter.ai/settings/management-keys). Shapes checked live
2026-09-25: POST /keys -> {"data": {"hash", "limit", "usage_daily", ...}, "key": "sk-or-v1-..."};
GET and PATCH /keys/{hash} -> {"data": {...}} (PATCH takes "disabled" and "limit");
DELETE /keys/{hash} -> {"deleted": true}.
"""

from __future__ import annotations

import httpx

API = "https://openrouter.ai/api/v1/keys"


class KeyApiError(RuntimeError):
    pass


class KeyManager:
    def __init__(self, management_key: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not management_key:
            raise KeyApiError("Add the OpenRouter management key in Settings.")
        self._headers = {"Authorization": f"Bearer {management_key}"}
        self._transport = transport

    async def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=20, transport=self._transport) as client:
                resp = await client.request(method, url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise KeyApiError(f"Could not reach OpenRouter: {type(exc).__name__}") from exc
        if resp.status_code == 404 and method == "DELETE":
            return {"deleted": True}
        if resp.status_code >= 400:
            raise KeyApiError(f"OpenRouter said {resp.status_code}: {_message(resp)}")
        return resp.json()

    async def create(self, name: str, daily_cap: float) -> tuple[str, str]:
        data = await self._call("POST", API, {"name": name[:60], "limit": daily_cap, "limit_reset": "daily"})
        return data["key"], data["data"]["hash"]

    async def delete(self, key_hash: str) -> None:
        await self._call("DELETE", f"{API}/{key_hash}")

    async def set_disabled(self, key_hash: str, disabled: bool) -> None:
        await self._call("PATCH", f"{API}/{key_hash}", {"disabled": disabled})

    async def set_limit(self, key_hash: str, daily_cap: float) -> None:
        await self._call("PATCH", f"{API}/{key_hash}", {"limit": daily_cap})

    async def spent_today(self, key_hash: str) -> float:
        data = await self._call("GET", f"{API}/{key_hash}")
        return round(float(data["data"].get("usage_daily") or 0), 4)


def _message(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:200]
    except ValueError:
        return resp.text[:200]
