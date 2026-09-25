import asyncio
import base64

import pytest

from mobile_rpa.db import Db
from mobile_rpa.devices import DeviceError, app_serial
from mobile_rpa.events import Broker
from mobile_rpa.orchestrator import Orchestrator


class FakeConn:
    def __init__(self):
        self.calls, self.notes, self.refuse = [], [], None

    async def call(self, method, params=None, timeout=30):
        self.calls.append((method, params))
        if self.refuse:
            raise DeviceError(self.refuse)
        return {"accepted": True}

    async def notify(self, method, params=None):
        self.notes.append((method, params))


class FakeDevices:
    def __init__(self, conn):
        self.conn = conn

    def get(self, device_id):
        if self.conn is None:
            raise DeviceError("The phone is not connected")
        return self.conn


class FakePhones:
    def __init__(self):
        self.busy = {}

    def publish(self):
        pass


@pytest.fixture
def world(tmp_path):
    db = Db(tmp_path / "t.db")
    phone = db.add_phone(app_serial("dev-1"), "POCO F3", "POCO F3", "16")
    conn = FakeConn()
    orch = Orchestrator(db, Broker(), FakePhones(), FakeDevices(conn), tmp_path / "shots")
    return db, orch, conn, phone


def new_task(db, phone, text="open settings"):
    spec = {"phone_id": phone["id"], "serial": phone["serial"], "phone_name": phone["name"], "instruction": text}
    return db.create_task(text, True, 30, [spec])


async def started(db, orch, phone, text="open settings"):
    task_id = new_task(db, phone, text)
    orch.start_task(task_id)
    for _ in range(10):
        await asyncio.sleep(0)
    return db.task(task_id)["runs"][0]


def event(run, seq, kind="action", text="Tap Settings", **extra):
    return {"method": "agent/event", "params": {"uuid": run["uuid"], "seq": seq, "kind": kind, "text": text, **extra}}


async def test_task_goes_to_the_phone_and_reports_come_back(world):
    db, orch, conn, phone = world
    run = await started(db, orch, phone)
    method, payload = conn.calls[0]
    assert method == "agent/run" and payload["uuid"] == run["uuid"] and payload["instruction"] == "open settings"
    assert run["status"] == "running" and orch.phones.busy[phone["serial"]] == run["id"]
    await orch.on_phone_message("dev-1", event(run, 1, steps=1))
    shot = base64.b64encode(b"\xff\xd8jpeg").decode()
    done = {"uuid": run["uuid"], "seq": 2, "status": "succeeded", "result": "Settings is open", "steps": 1, "shot": shot}
    await orch.on_phone_message("dev-1", {"method": "agent/finished", "params": done})
    run = db.run(run["id"])
    assert (run["status"], run["result"], run["steps"]) == ("succeeded", "Settings is open", 1)
    assert (orch.shots_dir / f"{run['id']}.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert conn.notes == [("agent/ack", {"uuid": run["uuid"], "seq": 1}), ("agent/ack", {"uuid": run["uuid"], "seq": 2})]
    assert phone["serial"] not in orch.phones.busy
    assert [e["kind"] for e in db.events(run["id"])][-2:] == ["action", "done"]


async def test_duplicate_events_are_stored_once(world):
    db, orch, conn, phone = world
    run = await started(db, orch, phone)
    await orch.on_phone_message("dev-1", event(run, 1))
    await orch.on_phone_message("dev-1", event(run, 1))  # resent after a reconnect
    assert [e["text"] for e in db.events(run["id"]) if e["kind"] == "action"] == ["Tap Settings"]
    assert len(conn.notes) == 2  # both copies acked


async def test_unknown_run_events_are_acked_and_dropped(world):
    db, orch, conn, _ = world
    await orch.on_phone_message("dev-1", {"method": "agent/event", "params": {"uuid": "nope", "seq": 4, "kind": "ok", "text": "x"}})
    assert conn.notes == [("agent/ack", {"uuid": "nope", "seq": 4})]


async def test_app_started_runs_appear_on_the_dashboard(world):
    db, orch, conn, phone = world
    start = {"uuid": "u-9", "seq": 0, "instruction": "check the weather", "reasoning": False, "max_steps": 12}
    await orch.on_phone_message("dev-1", {"method": "agent/started", "params": start})
    await orch.on_phone_message("dev-1", {"method": "agent/started", "params": start})  # resent
    run = db.run_by_uuid("u-9")
    task = db.task(run["task_id"])
    assert (task["prompt"], task["reasoning"], task["max_steps"]) == ("check the weather", 0, 12)
    assert (run["origin"], run["status"], len(db.tasks())) == ("app", "running", 1)
    assert orch.phones.busy[phone["serial"]] == run["id"]


async def test_a_phone_that_refuses_fails_the_run(world):
    db, orch, conn, phone = world
    conn.refuse = "Turn on FastAutomate v2 in Accessibility"
    run = await started(db, orch, phone)
    run = db.run(run["id"])
    assert run["status"] == "failed" and "Accessibility" in run["result"]
    assert phone["serial"] not in orch.phones.busy


async def test_stop_asks_the_phone_and_offline_phones_stop_here(world):
    db, orch, conn, phone = world
    run = await started(db, orch, phone)
    assert await orch.stop_run(run["id"])
    assert conn.calls[-1] == ("agent/stop", {"uuid": run["uuid"]})
    orch.devices.conn = None  # the phone went offline
    assert await orch.stop_run(run["id"])
    assert db.run(run["id"])["status"] == "stopped"
    await orch.on_phone_message("dev-1", event(run, 7))  # a late report after that is ignored
    assert not [e for e in db.events(run["id"]) if e["seq"] == 7]


async def test_silent_phone_fails_after_the_time_limit(world, monkeypatch):
    db, orch, conn, phone = world
    monkeypatch.setattr(Orchestrator, "_deadline_s", lambda self, settings: 0.05)
    run = await started(db, orch, phone)
    await asyncio.sleep(0.2)
    run = db.run(run["id"])
    assert run["status"] == "failed" and "stopped reporting" in run["result"]


async def test_resume_after_a_dashboard_restart(world, monkeypatch):
    db, orch, conn, phone = world
    monkeypatch.setattr(Orchestrator, "_deadline_s", lambda self, settings: 30)
    run = await started(db, orch, phone)
    fresh = Orchestrator(db, Broker(), FakePhones(), FakeDevices(conn), orch.shots_dir)
    fresh.resume()
    assert fresh.phones.busy[phone["serial"]] == run["id"] and run["id"] in fresh._watchdogs
    fresh.close()
    orch.close()
