import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mobile_rpa.adb import Adb, AdbError
from mobile_rpa.app import create_app
from mobile_rpa.settings import Env

FAKE_WORKER = [sys.executable, str(Path(__file__).with_name("fake_worker.py"))]


class FakeAdb(Adb):
    def __init__(self):
        super().__init__("adb")
        self.connected = {"emulator-5554": "device"}

    async def devices(self):
        return dict(self.connected)

    async def connect(self, address):
        if address.startswith("10.9."):
            raise AdbError("failed to connect to " + address)
        serial = address if ":" in address else address + ":5555"
        self.connected[serial] = "device"
        return serial

    async def pair(self, address, code):
        if code.replace(" ", "") != "123456":
            raise AdbError("Failed: Wrong password or connection was dropped.")
        self.paired = address

    async def disconnect(self, serial):
        self.connected.pop(serial, None)

    async def find_and_connect(self, host):
        serial = host + ":43307"
        self.connected[serial] = "device"
        return serial

    async def props(self, serial):
        return {"manufacturer": "Acme", "model": "P1", "android": "14"}

    async def shell(self, serial, command, timeout=20.0):
        return ""


@pytest.fixture
def client(tmp_path, monkeypatch):
    env = Env(password="connect", data_dir=tmp_path, session_secret=b"k" * 32, adb="adb")
    app = create_app(env, FakeAdb())
    monkeypatch.setattr(app.state.phones, "prepare", _no_prepare)
    app.state.runner.worker_cmd = FAKE_WORKER
    with TestClient(app) as c:
        yield c


async def _no_prepare(serial):
    return None


def login(c):
    assert c.post("/api/login", json={"password": "connect"}).status_code == 200


def test_api_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    login(client)
    assert client.get("/api/state").status_code == 200


def test_index_is_public_and_uncached(client):
    r = client.get("/")
    assert r.status_code == 200 and "Mobile RPA" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_add_rename_remove_phone(client):
    login(client)
    phone = client.post("/api/phones", json={"address": "192.168.1.40:5555"}).json()
    assert phone["name"] == "Acme P1" and phone["status"] == "online"
    assert client.post("/api/phones", json={"address": "10.9.9.9:5555"}).status_code == 400
    assert client.post("/api/phones", json={"address": "bad address!"}).status_code == 400
    client.patch(f"/api/phones/{phone['id']}", json={"name": "Desk"})
    assert client.get("/api/state").json()["phones"][0]["name"] == "Desk"
    assert client.delete(f"/api/phones/{phone['id']}").status_code == 200
    assert client.get("/api/state").json()["phones"] == []


def test_settings_never_return_the_key(client):
    login(client)
    r = client.put("/api/settings", json={"api_key": "sk-secret-123456", "planner_model": "m1", "bogus": "x"})
    body = r.json()
    assert "api_key" not in body and body["api_key_set"] and body["api_key_hint"] == "...3456"
    assert body["planner_model"] == "m1" and "bogus" not in body
    # blank key keeps the old one
    assert client.put("/api/settings", json={"api_key": ""}).json()["api_key_set"]
    assert client.put("/api/settings", json={"base_url": "ftp://x"}).status_code == 400
    assert client.put("/api/settings", json={"max_steps": "ten"}).status_code == 400


def wait_for(c, task_id, done, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = c.get(f"/api/tasks/{task_id}").json()
        if done(task):
            return task
        time.sleep(0.2)
    raise AssertionError("task did not settle: " + str(task))


def test_task_runs_on_two_phones_in_parallel(client):
    login(client)
    a = client.post("/api/phones", json={"address": "192.168.1.40:5555"}).json()
    b = client.post("/api/phones", json={"address": "192.168.1.41:5555", "name": "B"}).json()
    body = {"prompt": "x", "runs": [{"phone_id": a["id"], "instruction": "install Keep"},
                                    {"phone_id": b["id"], "instruction": "install Evernote"}]}
    assert client.post("/api/tasks", json=body).status_code == 400  # no API key yet
    client.put("/api/settings", json={"api_key": "sk-test-key-0000"})
    task = client.post("/api/tasks", json=body).json()
    task = wait_for(client, task["id"], lambda t: all(r["status"] == "succeeded" for r in t["runs"]))
    run = task["runs"][0]
    assert run["result"] == "finished 192.168.1.40:5555" and run["steps"] == 1
    kinds = [e["kind"] for e in run["events"]]
    assert kinds == ["phase", "plan", "action", "done"]
    assert client.get("/api/state").json()["stats"]["succeeded"] == 2


def test_busy_phone_rejected_and_stop_works(client):
    login(client)
    client.put("/api/settings", json={"api_key": "sk-test-key-0000"})
    a = client.post("/api/phones", json={"address": "192.168.1.40:5555"}).json()
    body = {"prompt": "x", "runs": [{"phone_id": a["id"], "instruction": "hang please"}]}
    task = client.post("/api/tasks", json=body).json()
    wait_for(client, task["id"], lambda t: t["runs"][0]["status"] == "running")
    time.sleep(0.5)
    again = client.post("/api/tasks", json=body)
    assert again.status_code == 409 and "busy" in again.json()["detail"]
    assert client.post(f"/api/tasks/{task['id']}/stop").json()["stopped"]
    task = wait_for(client, task["id"], lambda t: t["runs"][0]["status"] == "stopped")
    assert task["runs"][0]["result"] == "Stopped by operator."


def test_failed_run_keeps_the_reason(client):
    login(client)
    client.put("/api/settings", json={"api_key": "sk-test-key-0000"})
    a = client.post("/api/phones", json={"address": "192.168.1.40:5555"}).json()
    task = client.post("/api/tasks", json={"prompt": "x", "runs": [{"phone_id": a["id"], "instruction": "fail"}]}).json()
    task = wait_for(client, task["id"], lambda t: t["runs"][0]["status"] == "failed")
    assert task["runs"][0]["result"].startswith("finished")


def test_split_single_phone_is_verbatim(client):
    login(client)
    a = client.post("/api/phones", json={"address": "192.168.1.40:5555"}).json()
    r = client.post("/api/split", json={"prompt": "open settings", "phone_ids": [a["id"]]}).json()
    assert r["instructions"] == [{"phone_id": a["id"], "name": "Acme P1", "instruction": "open settings"}]


def test_orphaned_runs_are_interrupted_on_boot(tmp_path):
    from mobile_rpa.db import Db

    db = Db(tmp_path / "mobile-rpa.db")
    task_id = db.create_task("x", True, 10, [{"phone_id": 1, "serial": "s", "phone_name": "n", "instruction": "i"}])
    assert db.interrupt_orphans() == 1
    assert db.task(task_id)["runs"][0]["status"] == "interrupted"


def test_empty_credit_blocks_runs_with_a_clear_reason(client, monkeypatch):
    import mobile_rpa.app as app_mod

    async def broke(settings):
        return 0.01

    monkeypatch.setattr(app_mod, "credit_left", broke)
    login(client)
    client.put("/api/settings", json={"api_key": "sk-test-key-0000"})
    assert client.post("/api/credit/refresh").json() == {"credit": 0.01, "low": True}
    a = client.post("/api/phones", json={"address": "192.168.1.40:5555"}).json()
    r = client.post("/api/tasks", json={"prompt": "x", "runs": [{"phone_id": a["id"], "instruction": "go"}]})
    assert r.status_code == 402 and "credit is empty" in r.json()["detail"]
    assert client.get("/api/state").json()["credit"]["low"] is True


def test_virtual_phones_get_readable_names(client, monkeypatch):
    login(client)
    adb = client.app.state.phones.adb

    async def redroid(serial):
        return {"manufacturer": "redroid", "model": "redroid12_x86_64", "android": "12"}

    monkeypatch.setattr(adb, "props", redroid)
    first = client.post("/api/phones", json={"address": "10.0.0.5:5555"}).json()
    second = client.post("/api/phones", json={"address": "10.0.0.6:5555"}).json()
    assert (first["name"], second["name"]) == ("Virtual phone 1", "Virtual phone 2")


def test_two_formats_and_subscription_tokens_rejected(client):
    login(client)
    bad = client.put("/api/settings", json={"provider": "anthropic", "api_key": "sk-ant-oat01-abc"})
    assert bad.status_code == 400 and "console.anthropic.com" in bad.json()["detail"]
    ok = client.put("/api/settings", json={"provider": "anthropic", "api_key": "sk-ant-api03-abcd1234",
                                           "base_url": "https://api.anthropic.com"}).json()
    assert ok["provider"] == "anthropic" and ok["api_key_set"] and ok["ready"]
    assert "api_key" not in ok and ok["api_key_hint"] == "...1234"
    assert client.put("/api/settings", json={"provider": "openrouter"}).status_code == 400


def test_pairing_then_connect(client):
    login(client)
    bad = client.post("/api/phones", json={"address": "100.119.1.2:40001", "pair_address": "100.119.1.2:37001", "pair_code": "000000"})
    assert bad.status_code == 400 and "Wrong password" in bad.json()["detail"]
    ok = client.post("/api/phones", json={"address": "100.119.1.2:40001", "pair_address": "100.119.1.2:37001", "pair_code": "123 456"})
    assert ok.status_code == 200 and ok.json()["serial"] == "100.119.1.2:40001"


def test_pair_only_finds_the_connect_port_itself(client):
    login(client)
    r = client.post("/api/phones", json={"pair_address": "100.119.1.2:43095", "pair_code": "123456"})
    assert r.status_code == 200 and r.json()["serial"] == "100.119.1.2:43307"
    assert client.post("/api/phones", json={}).status_code == 400



def test_thumbnail_comes_only_from_the_compressed_stream(client, monkeypatch):
    login(client)
    a = client.post("/api/phones", json={"address": "192.168.1.40:5555"}).json()
    hub = client.app.state.hub

    async def small(serial):
        return b"\xff\xd8 tiny jpeg"

    monkeypatch.setattr(hub, "thumbnail", small)
    r = client.get(f"/api/phones/{a['id']}/screen.jpg")
    assert r.status_code == 200 and r.content.startswith(b"\xff\xd8")

    async def broken(serial):
        raise ConnectionError("stream down")

    client.app.state.phones._thumbs.clear()
    monkeypatch.setattr(hub, "thumbnail", broken)
    r = client.get(f"/api/phones/{a['id']}/screen.jpg")
    assert r.status_code == 503  # no silent 2 MB PNG fallback


def test_link_kind_labels():
    from mobile_rpa.phones import link_kind

    assert link_kind("100.119.217.83:43307", "POCO F3") == "netbird"
    assert link_kind("192.168.1.40:5555", "Pixel 8") == "wifi"
    assert link_kind("R58M123ABC", "SM-A515F") == "usb"
    assert link_kind("192.168.100.23:5555", "redroid12_x86_64") == "virtual"


def test_qr_endpoints(client):
    login(client)
    qr = client.post("/api/pair/qr").json()
    assert "<svg" in qr["svg"] and qr["state"] == "waiting"
    assert client.get(f"/api/pair/qr/{qr['qr_id']}").json()["state"] in ("waiting", "failed")
    assert client.delete(f"/api/pair/qr/{qr['qr_id']}").json()["ok"]
    assert client.get(f"/api/pair/qr/{qr['qr_id']}").status_code == 404
