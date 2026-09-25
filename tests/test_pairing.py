import asyncio

from mobile_rpa import pairing
from mobile_rpa.adb import AdbError


class PhoneOnNetBird:
    """A NetBird phone with Wireless debugging on (connect port 41000) that opens a pairing
    port (37000) once the QR has been scanned."""

    def __init__(self):
        self.scanned = False
        self.paired_with = None
        self.connected = {}

    async def text(self, *args, timeout=10):
        return "List of discovered mdns services\n"  # not on the server's LAN

    async def reachable(self, host):
        return True

    async def open_ports(self, host):
        if host != "100.119.1.2":
            return []
        return [37000, 41000] if self.scanned else [41000]

    async def pair(self, address, code, timeout=30):
        if address != "100.119.1.2:37000":
            raise AdbError("protocol fault")
        self.paired_with = code

    async def find_and_connect(self, host, ports=None):
        assert 41000 in ports
        return f"{host}:41000"


async def test_scan_is_enough_no_ip_typed(monkeypatch):
    phone = PhoneOnNetBird()
    added = {}

    async def on_paired(serial):
        added["serial"] = serial
        return {"name": "POCO F3", "serial": serial}

    qr = pairing.QrPairing(phone, "http://peers", on_paired)

    async def hosts(self):
        return ["100.119.1.9", "100.119.1.2"]  # a PC and the phone

    monkeypatch.setattr(pairing.QrPairing, "_netbird_hosts", hosts)
    monkeypatch.setattr(pairing, "POLL_S", 0.01)
    qr.start()
    await asyncio.sleep(0.1)
    assert qr.state == "waiting"
    phone.scanned = True  # the operator scans the QR on the phone
    for _ in range(100):
        if qr.state == "done":
            break
        await asyncio.sleep(0.02)
    assert qr.state == "done", qr.message
    assert phone.paired_with == qr.password and added["serial"] == "100.119.1.2:41000"
    assert qr.qr_text == f"WIFI:T:ADB;S:{qr.service};P:{qr.password};;"
