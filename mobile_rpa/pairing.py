"""Zero-typing QR pairing: show a QR code, the operator scans it on the phone, the phone appears.

The phone's Wireless debugging reads the QR (``WIFI:T:ADB;S:<name>;P:<password>;;``) and opens a
pairing port. adb normally finds it via mDNS, which works on the server's own LAN but does not cross
NetBird. So two searches run side by side:

- mDNS: ``adb mdns services`` lists ``<name>._adb-tls-pairing`` for phones on the same network.
- NetBird: the peer list (from the FA NetBird peer) tells which hosts exist; hosts with open
  wireless-debugging ports are phones. When one of them opens a new port, that is the pairing port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable

import httpx

from .adb import Adb, AdbError

log = logging.getLogger("mobile_rpa.pairing")

TIMEOUT_S = 180
POLL_S = 2.0


class QrPairing:
    def __init__(self, adb: Adb, peers_url: str, on_paired: Callable[[str], Awaitable[dict]]) -> None:
        self.adb, self.peers_url, self.on_paired = adb, peers_url, on_paired
        self.id = secrets.token_urlsafe(12)
        self.service = "mobile-rpa-" + secrets.token_hex(3)
        self.password = "".join(secrets.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(12))
        self.state = "waiting"  # waiting -> pairing -> connecting -> done | failed
        self.message = "Waiting for the phone to scan the code"
        self.phone: dict | None = None
        self.created = time.monotonic()
        self.task: asyncio.Task | None = None

    @property
    def qr_text(self) -> str:
        return f"WIFI:T:ADB;S:{self.service};P:{self.password};;"

    def view(self) -> dict:
        return {"state": self.state, "message": self.message, "phone": self.phone}

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    def cancel(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()

    async def _run(self) -> None:
        try:
            serial = await asyncio.wait_for(self._find(), TIMEOUT_S)
            self.state, self.message = "connecting", "Paired. Connecting..."
            self.phone = await self.on_paired(serial)
            self.state, self.message = "done", f"{self.phone.get('name', 'Phone')} added"
        except TimeoutError:
            self.state = "failed"
            self.message = "No phone paired within 3 minutes. Close this and try again."
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # shown to the operator as-is
            log.exception("QR pairing failed")
            self.state, self.message = "failed", str(exc) or type(exc).__name__

    async def _find(self) -> str:
        tried: dict[str, set[int]] = {}  # host -> ports that are not a pairing port
        phones: dict[str, set[int]] = {}  # NetBird hosts with wireless debugging on
        scouting = asyncio.create_task(self._scout(phones))
        try:
            while True:
                serial = await self._try_mdns()
                if serial:
                    return serial
                for host in list(phones):
                    serial = await self._try_host(host, phones, tried.setdefault(host, set()))
                    if serial:
                        return serial
                await asyncio.sleep(POLL_S)
        finally:
            scouting.cancel()

    async def _scout(self, phones: dict[str, set[int]]) -> None:
        """Find NetBird hosts that have wireless-debugging ports open (i.e. Android phones)."""
        hosts = await self._netbird_hosts()

        async def check(host: str) -> None:
            if not await self.adb.reachable(host):
                return
            with contextlib.suppress(AdbError):
                ports = await self.adb.open_ports(host)
                if ports:
                    phones[host] = set(ports)

        await asyncio.gather(*(check(h) for h in hosts))

    async def _netbird_hosts(self) -> list[str]:
        if not self.peers_url:
            return []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                peers = (await client.get(self.peers_url)).json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("NetBird peer list unavailable: %s", exc)
            return []
        return [p["ip"] for p in peers if p.get("ip") and p.get("status") != "Offline"]

    async def _try_mdns(self) -> str | None:
        with contextlib.suppress(AdbError):
            for line in (await self.adb.text("mdns", "services", timeout=10)).splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[0] == self.service and "pairing" in parts[1]:
                    self.state, self.message = "pairing", "Phone found. Pairing..."
                    await self.adb.pair(parts[2], self.password)
                    return await self.adb.find_and_connect(parts[2].rsplit(":", 1)[0])
        return None

    async def _try_host(self, host: str, phones: dict[str, set[int]], tried: set[int]) -> str | None:
        """Pair on any port of this phone not tried yet; connect ports refuse and are remembered."""
        with contextlib.suppress(AdbError):
            ports = set(await self.adb.open_ports(host))
            for port in sorted(ports - tried):
                try:
                    await self.adb.pair(f"{host}:{port}", self.password, timeout=12)
                except AdbError:
                    tried.add(port)
                    continue
                self.state, self.message = "pairing", "Phone found. Pairing..."
                return await self.adb.find_and_connect(host, sorted(tried | (ports - {port})))
            phones[host] = ports
        return None
