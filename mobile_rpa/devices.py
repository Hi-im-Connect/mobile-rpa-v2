"""Phones that connect themselves: the FastAutomate app opens a WebSocket to the dashboard.

No adb, no VPN. The app keeps one outgoing connection (it works on any Wi-Fi or mobile data) and
answers JSON-RPC requests: ``{"id", "method", "params"}`` -> ``{"id", "status", "result"|"error"}``.
Methods are the Portal's actions (tap, swipe, global, state, screenshot, keyboard/input, app, ...).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import itertools
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

log = logging.getLogger("mobile_rpa.devices")

APP_PREFIX = "app:"  # phone serials for app-connected devices: "app:<device id>"
GLOBAL_ACTIONS = {"back": 1, "home": 2, "recents": 3}  # AccessibilityService.GLOBAL_ACTION_*
# Android refuses accessibility screenshots requested too close together (~3 a second allowed),
# so each phone's screenshots (live view, thumbnails, agent) queue up with this spacing.
SHOT_SPACING_S = 0.4


class DeviceError(RuntimeError):
    pass


def app_serial(device_id: str) -> str:
    return APP_PREFIX + device_id


def device_id_of(serial: str) -> str | None:
    return serial[len(APP_PREFIX):] if serial.startswith(APP_PREFIX) else None


class DeviceConn:
    def __init__(self, ws: WebSocket, device_id: str, name: str) -> None:
        self.ws, self.device_id, self.name = ws, device_id, name
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._send_lock = asyncio.Lock()
        self.screen: tuple[int, int] | None = None  # real screen size, for scaling taps
        self._shot_lock = asyncio.Lock()
        self._last_shot = 0.0

    async def call(self, method: str, params: dict | None = None, timeout: float = 30) -> Any:
        request_id = next(self._ids)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with self._send_lock:
                await self.ws.send_text(json.dumps({"id": request_id, "method": method, "params": params or {}}))
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)
        if reply.get("status") == "error" or "error" in reply and "result" not in reply:
            raise DeviceError(str(reply.get("error") or "the phone reported an error"))
        return reply.get("result")

    async def notify(self, method: str, params: dict | None = None) -> None:
        """A message the phone does not answer, such as agent/ack."""
        async with self._send_lock:
            await self.ws.send_text(json.dumps({"method": method, "params": params or {}}))

    def feed(self, message: dict) -> None:
        future = self._pending.get(message.get("id"))  # type: ignore[arg-type]
        if future and not future.done():
            future.set_result(message)

    def fail_all(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DeviceError(reason))

    async def screen_size(self) -> tuple[int, int]:
        if self.screen is None:
            state = await self.call("state", {"filter": True})
            bounds = (state or {}).get("device_context", {}).get("screen_bounds", {})
            self.screen = (int(bounds.get("width") or 1080), int(bounds.get("height") or 2400))
        return self.screen

    async def screenshot(self, max_side: int = 960, quality: int = 70) -> bytes:
        """A JPEG made on the phone from an accessibility screenshot (no screen sharing)."""
        async with self._shot_lock:
            wait = self._last_shot + SHOT_SPACING_S - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_shot = time.monotonic()
            data = await self.call(
                "screenshot", {"hideOverlay": True, "format": "jpeg", "maxSide": max_side, "quality": quality}
            )
        raw = base64.b64decode(data) if isinstance(data, str) else bytes(data or b"")
        if not raw.startswith(b"\xff\xd8"):  # an older app build: PNG, shrink it here
            raw = await asyncio.to_thread(_shrink, raw, max_side, quality)
        return raw


def _shrink(image: bytes, max_side: int, quality: int) -> bytes:
    from PIL import Image

    img = Image.open(io.BytesIO(image)).convert("RGB")
    img.thumbnail((max_side, max_side))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


class DeviceHub:
    def __init__(self) -> None:
        self.conns: dict[str, DeviceConn] = {}
        self.on_change = None  # callback when a phone connects/disconnects
        self.on_message = None  # async (device_id, message) for agent/* reports

    def connected(self, device_id: str | None) -> bool:
        return bool(device_id) and device_id in self.conns

    def get(self, device_id: str | None) -> DeviceConn:
        conn = self.conns.get(device_id or "")
        if conn is None:
            raise DeviceError("The phone is not connected (open the FastAutomate app on it).")
        return conn

    async def serve(self, ws: WebSocket, device_id: str, name: str, on_ready=None) -> None:
        """Run one phone's connection until it drops."""
        old = self.conns.get(device_id)
        if old is not None:  # the app reconnected: the new socket wins
            old.fail_all("replaced by a newer connection")
            with contextlib.suppress(Exception):
                await old.ws.close()
        conn = DeviceConn(ws, device_id, name)
        self.conns[device_id] = conn
        self._changed()
        if on_ready:
            on_ready(conn)
        try:
            while True:
                incoming = await ws.receive()
                if incoming.get("type") == "websocket.disconnect":
                    break
                message = None
                with contextlib.suppress(ValueError, TypeError):
                    message = json.loads(incoming.get("text") or "")
                if not isinstance(message, dict):
                    continue
                if "method" not in message:
                    conn.feed(message)
                elif self.on_message and str(message["method"]).startswith("agent/"):
                    try:
                        await self.on_message(device_id, message)
                    except Exception:
                        log.exception("phone %s: report %s failed", device_id, message.get("method"))
        except Exception as exc:  # WebSocketDisconnect and friends
            log.info("phone %s disconnected: %s", device_id, type(exc).__name__)
        finally:
            conn.fail_all("the phone disconnected")
            if self.conns.get(device_id) is conn:
                del self.conns[device_id]
            self._changed()

    def _changed(self) -> None:
        if self.on_change:
            self.on_change()
