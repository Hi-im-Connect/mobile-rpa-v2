"""Async wrappers around the adb binary."""

from __future__ import annotations

import asyncio
import contextlib
import re

ADDRESS = re.compile(r"^[A-Za-z0-9._-]+(:\d{1,5})?$")
# All port probes share one budget (the service allows 8192 open files).
PROBE_SLOTS = asyncio.Semaphore(2500)


class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, binary: str = "adb") -> None:
        self.binary = binary

    async def run(self, *args: str, timeout: float = 20.0, serial: str | None = None) -> bytes:
        cmd = [self.binary, *(["-s", serial] if serial else []), *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise AdbError(f"adb {' '.join(args[:2])} timed out after {timeout:.0f}s") from None
        if proc.returncode != 0:
            message = (err or out).decode(errors="replace").strip() or f"exit {proc.returncode}"
            raise AdbError(message)
        return out

    async def text(self, *args: str, timeout: float = 20.0, serial: str | None = None) -> str:
        return (await self.run(*args, timeout=timeout, serial=serial)).decode(errors="replace")

    async def devices(self) -> dict[str, str]:
        """serial -> state ("device", "offline", "unauthorized", ...)."""
        out = await self.text("devices", timeout=10)
        found: dict[str, str] = {}
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                found[parts[0]] = parts[1]
        return found

    async def connect(self, address: str) -> str:
        """Connect a network phone. Returns the serial adb will use for it."""
        address = address.strip()
        if not ADDRESS.match(address):
            raise AdbError("Use host:port, for example 192.168.100.23:5555")
        if ":" not in address:
            address += ":5555"
        out = (await self.text("connect", address, timeout=15)).strip()
        if "connected to" not in out:
            raise AdbError(out or "adb could not connect")
        return address

    async def pair(self, address: str, code: str, timeout: float = 30) -> None:
        """Android 11+ wireless debugging: one-time pairing with the 6-digit code."""
        address, code = address.strip(), code.strip().replace(" ", "")
        if not ADDRESS.match(address) or ":" not in address:
            raise AdbError("Pairing needs the pairing IP:port shown next to the code")
        if not code.isalnum() or not 6 <= len(code) <= 32:
            raise AdbError("The pairing code is 6 digits")
        out = (await self.text("pair", address, code, timeout=timeout)).strip()
        if "Successfully paired" not in out:
            raise AdbError(out or "Pairing failed")

    async def open_ports(self, host: str, ports: range = range(30000, 50001)) -> list[int]:
        """Android picks random wireless-debugging ports (pairing and connect) in this range.
        Over NetBird mDNS discovery does not work, so probe the range instead."""
        if not ADDRESS.match(host) or ":" in host:
            raise AdbError("Use just the phone's IP address, for example 100.119.217.83")
        found: list[int] = []

        async def probe(port: int) -> None:
            async with PROBE_SLOTS:
                with contextlib.suppress(Exception):
                    _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 3)
                    found.append(port)
                    writer.close()

        await asyncio.gather(*(probe(p) for p in ports))
        return sorted(found)

    async def reachable(self, host: str, timeout: float = 5) -> bool:
        """One knock on a closed port: a live host refuses at once, an offline one times out.
        Keeps full port scans away from hosts that would make every probe wait."""
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, 9), timeout)
            writer.close()
            return True
        except ConnectionRefusedError:
            return True
        except (OSError, TimeoutError):
            return False

    async def connected_serial(self, host: str) -> str | None:
        """An already-connected adb serial for this host, if any."""
        for serial, state in (await self.devices()).items():
            if state == "device" and serial.startswith(host + ":"):
                return serial
        return None

    async def find_and_connect(self, host: str, ports: list[int] | None = None) -> str:
        known = await self.connected_serial(host)
        if known:
            return known
        for port in ports if ports is not None else await self.open_ports(host):
            with contextlib.suppress(AdbError):
                serial = await self.connect(f"{host}:{port}")
                if (await self.devices()).get(serial) == "device":
                    return serial
                await self.disconnect(serial)
        raise AdbError(f"Paired, but no wireless-debugging port answered on {host}. Is Wireless debugging still on?")

    async def disconnect(self, serial: str) -> None:
        if ":" in serial:
            try:
                await self.text("disconnect", serial, timeout=10)
            except AdbError:
                pass

    async def shell(self, serial: str, command: str, timeout: float = 20.0) -> str:
        return await self.text("shell", command, serial=serial, timeout=timeout)

    async def props(self, serial: str) -> dict[str, str]:
        out = await self.shell(
            serial,
            "getprop ro.product.manufacturer; getprop ro.product.model; "
            "getprop ro.build.version.release; getprop ro.product.marketname; "
            "getprop ro.product.vendor.marketname; getprop ro.config.marketing_name",
        )
        lines = [ln.strip() for ln in out.splitlines()] + [""] * 6
        # the name people know ("POCO F3") instead of the model code ("M2012K11AG")
        market = next((x for x in lines[3:6] if x), "")
        return {"manufacturer": lines[0], "model": market or lines[1], "android": lines[2]}
