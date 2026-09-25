"""One OpenRouter key per phone, capped per day, so every phone pays for its own AI and can be
paused alone. The key goes to the phone once (agent/credentials) and is never stored here; the
dashboard keeps only its hash. On every join the app says which key it holds (X-Agent-Key-Hash)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from .agent_prompts import OPENROUTER
from .db import Db
from .devices import DeviceError
from .openrouter_keys import KeyApiError, KeyManager

NAME_PREFIX = "fastautomate-v2-"


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
            if phone["key_hash"] and presented_hash == phone["key_hash"]:
                return None
            try:
                keys = self._manager()
                if phone["key_hash"]:
                    await keys.delete(phone["key_hash"])  # the app lost it (reinstall): replace it
                key, key_hash = await keys.create(f"{NAME_PREFIX}{phone_id}-{phone['name']}", self._cap())
                if phone["paused"]:
                    await keys.set_disabled(key_hash, True)
            except KeyApiError as exc:
                return str(exc)
            self.db.update_phone(phone_id, key_hash=key_hash)
            try:
                await conn.call("agent/credentials", {"key": key, "hash": key_hash, "base_url": OPENROUTER}, timeout=20)
            except (DeviceError, TimeoutError) as exc:
                return f"Could not give the phone its AI key: {exc or 'no answer'}"
            return None

    async def forget(self, phone: dict) -> None:
        if phone.get("key_hash"):
            with contextlib.suppress(KeyApiError):
                await self._manager().delete(phone["key_hash"])

    async def pause(self, phone: dict, paused: bool) -> None:
        if phone.get("key_hash"):
            await self._manager().set_disabled(phone["key_hash"], paused)
        self.db.update_phone(phone["id"], paused=int(paused))

    async def apply_cap(self, daily_cap: float) -> None:
        keys = self._manager()
        for phone in self.db.phones():
            if phone["key_hash"]:
                with contextlib.suppress(KeyApiError):
                    await keys.set_limit(phone["key_hash"], daily_cap)

    async def spent_today(self, phone: dict) -> float | None:
        if not phone.get("key_hash"):
            return None
        try:
            return await self._manager().spent_today(phone["key_hash"])
        except KeyApiError:
            return None
