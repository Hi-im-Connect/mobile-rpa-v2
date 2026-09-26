import base64
import io
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from mobile_rpa import app as app_module
from mobile_rpa.appconnect import JOIN_PATH
from mobile_rpa.app import create_app
from mobile_rpa.settings import Env


@pytest.fixture
def client(tmp_path):
    env = Env(password="connect", data_dir=tmp_path, session_secret=b"k" * 32)
    with TestClient(create_app(env)) as c:
        yield c


def login(c):
    assert c.post("/api/login", json={"password": "connect"}).status_code == 200


def jpeg_b64() -> str:
    out = io.BytesIO()
    Image.new("RGB", (216, 480), (18, 144, 79)).save(out, "JPEG")
    return base64.b64encode(out.getvalue()).decode()


def wait_for(predicate, seconds: float = 5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


class FakeKeys:
    """Stands in for OpenRouter's key management."""
    def __init__(self):
        self.log = []

    async def create(self, name, daily_cap):
        self.log.append(("create", name, daily_cap))
        return "sk-or-v1-phone", f"hash-{len(self.log)}"

    async def delete(self, key_hash):
        self.log.append(("delete", key_hash))

    async def set_disabled(self, key_hash, disabled):
        self.log.append(("disabled", key_hash, disabled))

    async def set_limit(self, key_hash, cap):
        self.log.append(("limit", key_hash, cap))

    async def spent_today(self, key_hash):
        return 0.12


def ready(client, monkeypatch) -> FakeKeys:
    """Keys in Settings, credit on the account, OpenRouter faked."""
    fake = FakeKeys()
    client.app.state.phone_keys._managers = lambda key: fake
    async def credit(settings):
        return 5.0
    monkeypatch.setattr(app_module, "credit_left", credit)
    client.put("/api/settings", json={"api_key": "sk-or-v1-dash-1234", "management_key": "sk-or-v1-mgmt-9876"})
    return fake


class AgentPhone:
    """A FastAutomate v2 app: takes every task, reports one action, then the result."""
    def __init__(self, ws):
        self.ws, self.calls, self.stop = ws, [], threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                msg = self.ws.receive_json()
            except Exception:
                return
            self.calls.append(msg)
            if msg.get("id") is None:
                continue  # acks need no answer
            method, p = msg["method"], msg.get("params") or {}
            result = jpeg_b64() if method == "screenshot" else {"accepted": True}
            if method == "state":
                result = {"a11y_tree": {}, "device_context": {"screen_bounds": {"width": 1080, "height": 2400}}}
            self.ws.send_json({"id": msg["id"], "status": "success", "result": result})
            if method == "agent/run" and "hang" not in p["instruction"]:
                budget = "budget" in p["instruction"]
                status = "failed" if budget or "fail" in p["instruction"] else "succeeded"
                result = "This phone's daily AI budget is used up" if budget else "did " + p["instruction"]
                self.ws.send_json({"method": "agent/event", "params": {"uuid": p["uuid"], "seq": 1, "kind": "action", "text": "Tap Settings", "steps": 1}})
                self.ws.send_json({"method": "agent/finished", "params": {"uuid": p["uuid"], "seq": 2, "status": status, "result": result, "steps": 1, "shot": jpeg_b64()}})
            if method == "agent/stop":
                self.ws.send_json({"method": "agent/finished", "params": {"uuid": p["uuid"], "seq": 9, "status": "stopped", "result": "Stopped on the phone", "steps": 0}})

    def sent(self, method):
        return [m for m in self.calls if m.get("method") == method]


def join(client, device_id="dev-1", key_hash=""):
    token = client.post("/api/app/invite").json()["url"].split("t=")[1]
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": device_id, "X-Device-Name": "POCO F3", "X-Android-Version": "16"}
    if key_hash:
        headers["X-Agent-Key-Hash"] = key_hash
    return client.websocket_connect(JOIN_PATH, headers=headers)


def the_phone(client):
    return wait_for(lambda: next(iter(client.get("/api/state").json()["phones"]), None))


def keyed_phone(client):
    phone = the_phone(client)
    wait_for(lambda: client.app.state.db.phone(phone["id"])["key_hash"])
    return phone


def run_task(client, phone, instruction="open settings"):
    r = client.post("/api/tasks", json={"prompt": instruction, "runs": [{"phone_id": phone["id"], "instruction": instruction}]})
    assert r.status_code == 200, r.text
    return r.json()


def test_api_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    login(client)
    assert client.get("/api/state").status_code == 200


def test_index_is_public_and_uncached(client):
    r = client.get("/")
    assert r.status_code == 200 and "Mobile RPA" in r.text and r.headers["cache-control"] == "no-store"


def test_settings_never_return_secrets(client):
    login(client)
    s = client.put("/api/settings", json={"api_key": "sk-or-v1-dash-1234", "management_key": "sk-or-v1-mgmt-9876", "daily_cap_usd": "1.50"}).json()
    assert "api_key" not in s and "management_key" not in s
    assert (s["api_key_hint"], s["management_key_hint"], s["daily_cap_usd"], s["ready"]) == ("...1234", "...9876", "1.50", True)
    assert client.put("/api/settings", json={"daily_cap_usd": "0"}).status_code == 400
    assert client.put("/api/settings", json={"daily_cap_usd": "abc"}).status_code == 400
    assert client.put("/api/settings", json={"provider": "anthropic"}).json().get("provider") is None


def test_phone_gets_its_own_key_when_it_joins(client, monkeypatch):
    login(client)
    fake = ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        keyed_phone(client)
        creds = wait_for(lambda: phone.sent("agent/credentials"))[0]["params"]
        assert creds == {"key": "sk-or-v1-phone", "hash": "hash-1", "base_url": "https://openrouter.ai/api/v1"}
        assert fake.log == [("create", f"fastautomate-v2-{the_phone(client)['id']}-POCO F3", 2.0)]
        phone.stop.set()


def test_task_runs_on_the_phone_itself(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        task = run_task(client, keyed_phone(client))
        run = wait_for(lambda: next((r for r in client.get(f"/api/tasks/{task['id']}").json()["runs"] if r["status"] == "succeeded"), None))
        assert (run["result"], run["steps"]) == ("did open settings", 1)
        sent = phone.sent("agent/run")[0]["params"]
        assert sent["uuid"] == run["uuid"] and sent["executor_model"] == "google/gemini-2.5-flash" and sent["prompts"]["tools"]
        assert "sk-or-v1-dash" not in json.dumps(sent) and "sk-or-v1-mgmt" not in json.dumps(sent)
        assert [e["kind"] for e in run["events"]][-2:] == ["action", "done"]
        assert client.get(f"/api/runs/{run['id']}/shot.jpg").content.startswith(b"\xff\xd8")
        wait_for(lambda: [m["params"]["seq"] for m in phone.sent("agent/ack")] == [1, 2])
        phone.stop.set()


def test_busy_phone_rejected_and_stop_asks_the_phone(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = keyed_phone(client)
        task = run_task(client, p, "hang around")
        wait_for(lambda: phone.sent("agent/run"))
        again = client.post("/api/tasks", json={"prompt": "x", "runs": [{"phone_id": p["id"], "instruction": "x"}]})
        assert again.status_code == 409
        assert client.post(f"/api/tasks/{task['id']}/stop").json()["stopped"]
        wait_for(lambda: client.get(f"/api/tasks/{task['id']}").json()["runs"][0]["status"] == "stopped")
        phone.stop.set()


def test_phone_without_a_key_cannot_take_tasks(client, monkeypatch):
    login(client)
    async def credit(settings):
        return 5.0
    monkeypatch.setattr(app_module, "credit_left", credit)
    client.put("/api/settings", json={"api_key": "sk-or-v1-dash-1234"})  # no management key
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = the_phone(client)
        r = client.post("/api/tasks", json={"prompt": "x", "runs": [{"phone_id": p["id"], "instruction": "x"}]})
        assert r.status_code == 409 and "AI key" in r.json()["detail"]
        assert "management key" in wait_for(lambda: the_phone(client)["note"])
        phone.stop.set()


def test_empty_credit_blocks_runs_with_a_clear_reason(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    async def empty(settings):
        return 0.01
    monkeypatch.setattr(app_module, "credit_left", empty)
    client.post("/api/credit/refresh")  # the dashboard learns the balance on refresh (also every 2 min)
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = keyed_phone(client)
        r = client.post("/api/tasks", json={"prompt": "x", "runs": [{"phone_id": p["id"], "instruction": "x"}]})
        assert r.status_code == 402 and "credit" in r.json()["detail"]
        phone.stop.set()


def test_removing_a_phone_deletes_its_key(client, monkeypatch):
    login(client)
    fake = ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = keyed_phone(client)
        assert client.delete(f"/api/phones/{p['id']}").status_code == 200
        assert ("delete", "hash-1") in fake.log
        phone.stop.set()


def test_split_single_phone_is_verbatim(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        p = the_phone(client)
        r = client.post("/api/split", json={"prompt": "Open Settings", "phone_ids": [p["id"]]}).json()
        assert r["instructions"][0]["instruction"] == "Open Settings"
        phone.stop.set()


def test_switching_to_another_provider_gives_phones_the_shared_key(client, monkeypatch):
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        keyed_phone(client)
        gemini = "https://generativelanguage.googleapis.com/v1beta/openai"
        client.put("/api/settings", json={"base_url": gemini, "api_key": "gemini-key", "planner_model": "gemini-3.1-flash-lite-preview"})
        creds = wait_for(lambda: [m for m in phone.sent("agent/credentials") if m["params"]["base_url"] == gemini])[0]["params"]
        assert creds["key"] == "gemini-key" and creds["hash"].startswith("shared-")
        assert client.put("/api/settings", json={"base_url": "ftp://x"}).status_code == 400
        phone.stop.set()



def test_phones_always_have_the_current_settings(client, monkeypatch):
    """The app's own Run button uses the dashboard's provider and models: sent on join and on change."""
    login(client)
    ready(client, monkeypatch)
    with join(client) as ws:
        phone = AgentPhone(ws)
        first = wait_for(lambda: phone.sent("agent/settings"))[0]["params"]["defaults"]
        assert first["executor_model"] == "google/gemini-2.5-flash" and first["prompts"]["tools"] and "uuid" not in first
        client.put("/api/settings", json={"executor_model": "gemini-3.1-flash-lite-preview"})
        changed = wait_for(lambda: [m for m in phone.sent("agent/settings")
                                    if m["params"]["defaults"]["executor_model"] == "gemini-3.1-flash-lite-preview"])
        assert changed
        phone.stop.set()
