"""One OpenRouter key per phone, capped per day, so every phone pays for its own AI and can be
paused alone. The key goes to the phone once (agent/credentials) and is never stored here; the
dashboard keeps only its hash. On every join the app says which key it holds (X-Agent-Key-Hash).

Other OpenAI-compatible providers (for example Gemini's OpenAI endpoint) cannot issue keys: there
every phone gets the dashboard's own key ("shared-" hash), with no per-phone cap."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Callable

from .settings import OPENROUTER, is_openrouter, llm
from .db import Db
from .devices import DeviceError
from .openrouter_keys import KeyApiError, KeyManager

NAME_PREFIX = "fastautomate-v2-"
SHARED = "shared-"


def shared_hash(base_url: str, key: str) -> str:
    return SHARED + hashlib.sha256(f"{base_url}\n{key}".encode()).hexdigest()[:16]


def key_mode(settings: dict[str, str]) -> tuple:
    """What the phones' keys depend on; when it changes, connected phones get new credentials."""
    if is_openrouter(settings):
        return ("openrouter", settings.get("management_key", ""))
    conn = llm(settings)
    return ("shared", conn["base_url"], conn["key"])


def _own(key_hash: str) -> bool:
    """A per-phone OpenRouter key (not the shared one)."""
    return bool(key_hash) and not key_hash.startswith(SHARED)


class PhoneKeys:
    def __init__(self, db: Db, managers: Callable[[str], KeyManager] = KeyManager) -> None:
        self.db = db
        self._managers = managers
        self._locks: dict[int, asyncio.Lock] = {}

    def _manager(self) -> KeyManager:
        return self._managers(self.db.settings().get("management_key", ""))

    def _cap(self) -> float:
        try:
            return float(self.db.settings().get("daily_cap_usd") or 2)
        except ValueError:
            return 2.0

    async def ensure(self, phone_id: int, conn, presented_hash: str) -> str | None:
        """Make sure the phone holds a working key. Returns a problem to show on its card, or None."""
        async with self._locks.setdefault(phone_id, asyncio.Lock()):
            phone = self.db.phone(phone_id)
            if not phone:
                return None
            settings = self.db.settings()
            if not is_openrouter(settings):
                return await self._share(phone, conn, presented_hash, settings)
            if _own(phone["key_hash"]) and presented_hash == phone["key_hash"]:
                return None
            try:
                keys = self._manager()
                if _own(phone["key_hash"]):  # the app lost it (reinstall): replace it
                    with contextlib.suppress(KeyApiError):  # a key that cannot be deleted must not block a new one
                        await keys.delete(phone["key_hash"])
                key, key_hash = await keys.create(f"{NAME_PREFIX}{phone_id}-{phone['name']}", self._cap())
            except KeyApiError as exc:
                return str(exc)
            if phone["paused"]:
                try:
                    await keys.set_disabled(key_hash, True)
                except KeyApiError as exc:  # a paused phone must not get a working key
                    with contextlib.suppress(KeyApiError):
                        await keys.delete(key_hash)
                    self.db.update_phone(phone_id, key_hash="")
                    return str(exc)
            self.db.update_phone(phone_id, key_hash=key_hash)
            try:
                await conn.call("agent/credentials", {"key": key, "hash": key_hash, "base_url": OPENROUTER}, timeout=20)
            except (DeviceError, TimeoutError) as exc:
                return f"Could not give the phone its AI key: {exc or 'no answer'}"
            return None

    async def _share(self, phone: dict, conn, presented_hash: str, settings: dict[str, str]) -> str | None:
        ai = llm(settings)
        if not ai["key"]:
            return "Add the API key in Settings."
        key_hash = shared_hash(ai["base_url"], ai["key"])
        if phone["key_hash"] == key_hash and presented_hash == key_hash:
            return None
        if _own(phone["key_hash"]):
            await self.forget(phone)  # its OpenRouter key is no longer used
        self.db.update_phone(phone["id"], key_hash=key_hash)
        try:
            await conn.call("agent/credentials", {"key": ai["key"], "hash": key_hash, "base_url": ai["base_url"]}, timeout=20)
        except (DeviceError, TimeoutError) as exc:
            return f"Could not give the phone its AI key: {exc or 'no answer'}"
        return None

    async def forget(self, phone: dict) -> None:
        if _own(phone.get("key_hash", "")):
            with contextlib.suppress(KeyApiError):
                await self._manager().delete(phone["key_hash"])

    async def pause(self, phone: dict, paused: bool) -> None:
        if _own(phone.get("key_hash", "")):  # a shared key cannot be paused; the dashboard refuses tasks instead
            await self._manager().set_disabled(phone["key_hash"], paused)
        self.db.update_phone(phone["id"], paused=int(paused))

    async def apply_cap(self, daily_cap: float) -> None:
        own = [p["key_hash"] for p in self.db.phones() if _own(p["key_hash"])]
        if not own:
            return  # shared keys have no per-phone cap
        keys = self._manager()
        for key_hash in own:
            with contextlib.suppress(KeyApiError):
                await keys.set_limit(key_hash, daily_cap)

    async def spent_today(self, phone: dict) -> float | None:
        if not _own(phone.get("key_hash", "")):
            return None
        try:
            return await self._manager().spent_today(phone["key_hash"])
        except KeyApiError:
            return None
