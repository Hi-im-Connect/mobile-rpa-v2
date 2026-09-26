"""Saved phones (all connected through the FastAutomate v2 app), their state and thumbnails."""

from __future__ import annotations

import asyncio
import contextlib
import time

from .db import Db
from .devices import DeviceHub, app_serial, device_id_of
from .events import Broker

THUMB_MAX_AGE = 5.0  # cards ask every 10 s; several open dashboards share one capture


class PhoneRegistry:
    def __init__(self, db: Db, broker: Broker, devices: DeviceHub) -> None:
        self.db, self.broker, self.devices = db, broker, devices
        self.busy: dict[str, int] = {}  # serial -> run id
        self.notes: dict[str, str] = {}  # serial -> problem shown on the card
        self._thumbs: dict[str, tuple[float, bytes]] = {}
        self._thumb_locks: dict[str, asyncio.Lock] = {}

    def status(self, serial: str) -> str:
        if serial in self.busy:
            return "busy"
        return "online" if self.devices.connected(device_id_of(serial)) else "offline"

    def view(self, phone: dict) -> dict:
        serial = phone["serial"]
        return {**phone, "link": "app", "status": self.status(serial), "run_id": self.busy.get(serial),
                "note": self.notes.get(serial, "")}

    def all(self) -> list[dict]:
        return [self.view(p) for p in self.db.phones()]

    def publish(self) -> None:
        self.broker.publish("phones", self.all())

    def set_note(self, serial: str, note: str) -> None:
        if self.notes.get(serial, "") == note:
            return
        if note:
            self.notes[serial] = note[:200]
        else:
            self.notes.pop(serial, None)
        self.publish()

    async def add_app_phone(self, device_id: str, name: str, android: str = "") -> dict:
        """Name and Android version follow what the app reports on each connect."""
        name = (name or "Phone").strip()[:60]
        phone = self.db.add_phone(app_serial(device_id), name, name, android)
        changes = {k: v for k, v in (("name", name), ("model", name), ("android", android)) if v and phone.get(k) != v}
        if changes:
            self.db.update_phone(phone["id"], **changes)
            phone = {**phone, **changes}
        self.publish()
        return self.view(phone)

    async def remove(self, phone_id: int) -> None:
        phone = self.db.phone(phone_id)
        if not phone:
            return
        serial = phone["serial"]
        self.db.delete_phone(phone_id)
        self._thumbs.pop(serial, None)
        self.notes.pop(serial, None)
        device_id = device_id_of(serial) or ""
        self.db.revoke_device_tokens(device_id)  # the app cannot silently come back
        if self.devices.connected(device_id):
            with contextlib.suppress(Exception):
                await self.devices.get(device_id).ws.close()
        self.publish()

    async def rename(self, phone_id: int, name: str) -> None:
        self.db.update_phone(phone_id, name=name.strip()[:60])
        self.publish()

    async def thumbnail(self, serial: str) -> bytes:
        """A small accessibility screenshot (480 px JPEG), cached briefly."""
        lock = self._thumb_locks.setdefault(serial, asyncio.Lock())
        async with lock:
            cached = self._thumbs.get(serial)
            if cached and time.monotonic() - cached[0] < THUMB_MAX_AGE:
                return cached[1]
            jpeg = await self.devices.get(device_id_of(serial) or "").screenshot(480, 60)
            self._thumbs[serial] = (time.monotonic(), jpeg)
            return jpeg
