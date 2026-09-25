"""Saved phones, their live adb state, reconnects and screenshot thumbnails."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import sys
import time

from .adb import Adb, AdbError
from .db import Db, now
from .devices import DeviceHub, app_serial, device_id_of
from .events import Broker

log = logging.getLogger("mobile_rpa.phones")

POLL_SECONDS = 4.0
RECONNECT_SECONDS = 20.0
FOLLOW_SECONDS = 120.0  # port search is heavier: at most every 2 minutes per phone
THUMB_MAX_AGE = 2.0  # cards ask every 2.5 s
VIRTUAL_HINTS = ("redroid", "sdk_gphone", "emulator", "genymotion", "waydroid", "android sdk built")


class PhoneRegistry:
    def __init__(self, db: Db, adb: Adb, broker: Broker) -> None:
        self.db, self.adb, self.broker = db, adb, broker
        self.states: dict[str, str] = {}  # serial -> adb state ("device", "offline", "missing")
        self.busy: dict[str, int] = {}  # serial -> run id
        self.preparing: set[str] = set()
        self.notes: dict[str, str] = {}  # serial -> last problem, shown on the card
        self._last_reconnect: dict[str, float] = {}
        self._thumbs: dict[str, tuple[float, bytes]] = {}
        self._thumb_locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        self._following: set[int] = set()  # phone ids whose new port is being searched
        self.devices: DeviceHub | None = None  # phones connected through the FastAutomate app

    # ---- view ----------------------------------------------------------------------------
    def status(self, serial: str) -> str:
        if serial in self.busy:
            return "busy"
        if serial in self.preparing:
            return "preparing"
        if device_id_of(serial) is not None:  # app phone: online while its connection is open
            return "online" if self.devices and self.devices.connected(device_id_of(serial)) else "offline"
        state = self.states.get(serial, "missing")
        if state == "device":
            return "online"
        if state == "unauthorized":
            return "unauthorized"
        return "offline"

    def view(self, phone: dict) -> dict:
        serial = phone["serial"]
        return {
            **phone,
            "link": link_kind(serial, phone.get("model", "")),
            "status": self.status(serial),
            "run_id": self.busy.get(serial),
            "note": self.notes.get(serial, ""),
        }

    def all(self) -> list[dict]:
        return [self.view(p) for p in self.db.phones()]

    def publish(self) -> None:
        self.broker.publish("phones", self.all())

    # ---- lifecycle -----------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("phone poll failed")
            await asyncio.sleep(POLL_SECONDS)

    async def refresh(self) -> None:
        try:
            found = await self.adb.devices()
        except AdbError as exc:
            log.warning("adb devices failed: %s", exc)
            found = {}
        changed = False
        for phone in self.db.phones():
            serial = phone["serial"]
            if device_id_of(serial) is not None:
                continue  # app phones are not on adb; their status comes from the device hub
            state = found.get(serial, "missing")
            if state != "device" and ":" in serial:
                state = await self._maybe_reconnect(serial, state)
                if state != "device" and serial not in self.busy and phone["id"] not in self._following:
                    self._following.add(phone["id"])
                    asyncio.create_task(self._follow_in_background(phone))
            if self.states.get(serial) != state:
                self.states[serial] = state
                changed = True
            if state == "device":
                self.db.update_phone(phone["id"], last_seen=now())
                if self.notes.pop(serial, None):
                    changed = True
        if changed:
            self.publish()

    async def _maybe_reconnect(self, serial: str, state: str) -> str:
        last = self._last_reconnect.get(serial, 0.0)
        if time.monotonic() - last < RECONNECT_SECONDS:
            return state
        self._last_reconnect[serial] = time.monotonic()
        try:
            if state == "offline":
                await self.adb.disconnect(serial)
            await self.adb.connect(serial)
            return (await self.adb.devices()).get(serial, "missing")
        except AdbError as exc:
            self.notes[serial] = str(exc)[:160]
            return state

    async def _follow_in_background(self, phone: dict) -> None:
        try:
            if await self._maybe_follow_port(phone):
                self.publish()
        finally:
            self._following.discard(phone["id"])

    async def _maybe_follow_port(self, phone: dict) -> str | None:
        """Wireless debugging gets a new port after a reboot or toggle. The phone is already
        paired, so find the new port and keep the same card instead of making the user re-add it."""
        old = phone["serial"]
        key = "follow:" + old
        if time.monotonic() - self._last_reconnect.get(key, 0.0) < FOLLOW_SECONDS:
            return None
        self._last_reconnect[key] = time.monotonic()
        host = old.rsplit(":", 1)[0]
        try:
            new = await self.adb.find_and_connect(host)
        except AdbError:
            return None
        if new == old or self.db.phone_by_serial(new):
            return None
        self.db.update_phone(phone["id"], serial=new)
        self.states.pop(old, None)
        self.notes.pop(old, None)
        self.states[new] = "device"
        log.info("phone %s moved %s -> %s", phone["name"], old, new)
        asyncio.create_task(self.prepare(new))
        return new

    # ---- add / remove --------------------------------------------------------------------
    async def add(self, address: str, name: str = "", pair_address: str = "", pair_code: str = "") -> dict:
        if pair_code.strip():
            await self.adb.pair(pair_address, pair_code)
        serial = address.strip()
        if not serial and pair_address.strip():
            # the connect port differs from the pairing port; find it instead of asking for it
            serial = await self.adb.find_and_connect(pair_address.strip().split(":", 1)[0])
        elif ":" in serial or "." in serial:
            serial = await self.adb.connect(serial)
        elif (await self.adb.devices()).get(serial) != "device":
            raise AdbError(f"{serial} is not connected to adb on the server.")
        props = await self.adb.props(serial)
        label = name.strip() or self._default_name(props, serial)
        phone = self.db.add_phone(serial, label[:60], props["model"], props["android"])
        self.states[serial] = "device"
        self.publish()
        asyncio.create_task(self.prepare(serial))
        return self.view(phone)

    def _default_name(self, props: dict[str, str], serial: str) -> str:
        """Readable default: "Virtual phone 2" for emulators/redroid, "Samsung SM-A515F" otherwise."""
        maker, model = props["manufacturer"].strip(), props["model"].strip()
        if any(k in f"{maker} {model}".lower() for k in VIRTUAL_HINTS):
            taken = {p["name"] for p in self.db.phones()}
            n = 1
            while f"Virtual phone {n}" in taken:
                n += 1
            return f"Virtual phone {n}"
        if maker and model.lower().startswith(maker.lower()):
            maker = ""
        return " ".join(x for x in (maker.title(), model) if x) or serial

    async def add_app_phone(self, device_id: str, name: str, android: str = "") -> dict:
        """A phone that connected itself through the FastAutomate app; name and Android version
        follow what the app reports on each connect."""
        name = (name or "Phone").strip()[:60]
        phone = self.db.add_phone(app_serial(device_id), name, name, android)
        changes = {k: v for k, v in (("name", name), ("model", name), ("android", android))
                   if v and phone.get(k) != v}
        if changes:
            self.db.update_phone(phone["id"], **changes)
            phone = {**phone, **changes}
        self.publish()
        return self.view(phone)

    async def prepare(self, serial: str) -> None:
        """Install/enable the Mobilerun Portal in the background so the first task starts fast."""
        if device_id_of(serial) is not None:
            return  # the app is already there: it is how the phone connects
        self.preparing.add(serial)
        self.publish()
        with contextlib.suppress(AdbError):  # refresh the display name / Android version too
            props = await self.adb.props(serial)
            phone = self.db.phone_by_serial(serial)
            if phone:
                self.db.update_phone(phone["id"], model=props["model"], android=props["android"])
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "mobile_rpa.worker", "--prepare", serial,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), 300)
            if proc.returncode != 0:
                tail = out.decode(errors="replace").strip().splitlines()[-1:] or ["setup failed"]
                self.notes[serial] = "Portal setup: " + tail[0].replace("@@MRPA ", "")[:160]
        except TimeoutError:
            self.notes[serial] = "Portal setup timed out"
        finally:
            self.preparing.discard(serial)
            self.publish()

    async def remove(self, phone_id: int) -> None:
        phone = self.db.phone(phone_id)
        if not phone:
            return
        if phone["serial"] in self.busy:
            raise AdbError("Stop the task running on this phone first.")
        self.db.delete_phone(phone_id)
        self.states.pop(phone["serial"], None)
        self._thumbs.pop(phone["serial"], None)
        device_id = device_id_of(phone["serial"])
        if device_id is not None:  # forget the app's token so it cannot silently come back
            self.db.revoke_device_tokens(device_id)
            if self.devices and self.devices.connected(device_id):
                with contextlib.suppress(Exception):
                    await self.devices.get(device_id).ws.close()
        else:
            await self.adb.disconnect(phone["serial"])
        self.publish()

    async def rename(self, phone_id: int, name: str) -> None:
        self.db.update_phone(phone_id, name=name.strip()[:60])
        self.publish()

    # ---- thumbnails ----------------------------------------------------------------------
    async def thumbnail(self, serial: str, stream) -> bytes:
        """A compressed video keyframe as JPEG (~10-20 KB), cached briefly so several open
        dashboards share one capture."""
        lock = self._thumb_locks.setdefault(serial, asyncio.Lock())
        async with lock:
            cached = self._thumbs.get(serial)
            if cached:
                taken, jpeg = cached
                if time.monotonic() - taken < THUMB_MAX_AGE:
                    return jpeg
            device_id = device_id_of(serial)
            if device_id is not None:
                jpeg = await self.devices.get(device_id).screenshot(480, 60)
            else:
                jpeg = await stream.thumbnail(serial)
            self._thumbs[serial] = (time.monotonic(), jpeg)
            return jpeg


NETBIRD = ipaddress.ip_network("100.64.0.0/10")


def link_kind(serial: str, model: str = "") -> str:
    """How the server reaches the phone: app, usb, virtual, netbird or wifi (shown as an icon)."""
    if device_id_of(serial) is not None:
        return "app"
    if any(k in model.lower() for k in VIRTUAL_HINTS) or serial.startswith("emulator-"):
        return "virtual"
    if ":" not in serial:
        return "usb"
    try:
        return "netbird" if ipaddress.ip_address(serial.rsplit(":", 1)[0]) in NETBIRD else "wifi"
    except ValueError:
        return "wifi"
