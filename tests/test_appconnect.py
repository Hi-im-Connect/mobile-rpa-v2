import asyncio
import base64
import io
import itertools
import json
import threading
import time

from PIL import Image

from mobile_rpa import appconnect, devices

from .test_app import client, login  # noqa: F401  (fixture + helper)

JOIN = appconnect.JOIN_PATH


def _jpeg(color=(18, 144, 79)) -> str:
    out = io.BytesIO()
    Image.new("RGB", (216, 480), color).save(out, "JPEG")
    return base64.b64encode(out.getvalue()).decode()


def fake_phone(ws, stop, calls=None, failing=(), screens=None):
    """Answers the dashboard like the FastAutomate app does (records calls, fails the listed methods;
    screens = JPEGs to show one after another, the last one repeating)."""
    while not stop.is_set():
        try:
            msg = ws.receive_json()
        except Exception:
            return
        method = msg["method"]
        if calls is not None:
            calls.append((method, msg["params"]))
        if method in failing:
            ws.send_json({"id": msg["id"], "status": "error", "error": f"{method} failed"})
            continue
        if method == "screenshot":
            result = screens.pop(0) if screens and len(screens) > 1 else (screens[0] if screens else _jpeg())
        elif method == "state":
            result = {"a11y_tree": {}, "device_context": {"screen_bounds": {"width": 1080, "height": 2400}}}
        else:
            result = "ok"
        ws.send_json({"id": msg["id"], "status": "success", "result": result})


def invite_token(c) -> str:
    url = c.post("/api/app/invite").json()["url"]
    return url.split("t=")[1]


def test_phone_connects_itself_and_shows_up(client):  # noqa: F811
    login(client)
    token = invite_token(client)
    assert client.get(f"/connect?t={token}").status_code == 200
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "dev-1", "X-Device-Name": "Pixel 8"}
    stop = threading.Event()
    with client.websocket_connect(JOIN, headers=headers) as ws:
        threading.Thread(target=fake_phone, args=(ws, stop), daemon=True).start()
        phone = next(p for p in client.get("/api/state").json()["phones"] if p["link"] == "app")
        assert phone["name"] == "Pixel 8" and phone["status"] == "online"
        shot = client.get(f"/api/phones/{phone['id']}/screen.jpg")
        assert shot.status_code == 200 and shot.content.startswith(b"\xff\xd8")
        # the token now belongs to this phone: another device cannot use it
        other = {**headers, "X-Device-ID": "dev-2"}
        try:
            with client.websocket_connect(JOIN, headers=other) as ws2:
                ws2.receive_text()
            reused = True
        except Exception:
            reused = False
        assert not reused
        stop.set()
    offline = next(p for p in client.get("/api/state").json()["phones"] if p["link"] == "app")
    assert offline["status"] == "offline"


def test_unknown_token_is_refused(client):  # noqa: F811
    try:
        with client.websocket_connect(JOIN, headers={"Authorization": "Bearer nope", "X-Device-ID": "x"}) as ws:
            ws.receive_text()
        accepted = True
    except Exception:
        accepted = False
    assert not accepted


def test_app_prompt_runs_a_task_on_its_own_phone(client):  # noqa: F811
    login(client)
    client.put("/api/settings", json={"api_key": "sk-test-key-0000"})
    token = invite_token(client)
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "dev-9", "X-Device-Name": "POCO F3"}
    stop = threading.Event()
    with client.websocket_connect(JOIN, headers=headers) as ws:
        threading.Thread(target=fake_phone, args=(ws, stop), daemon=True).start()
        auth = {"Authorization": f"Bearer {token}"}
        assert client.get("/v1/models", headers=auth).json()["models"]
        created = client.post("/v1/tasks", headers=auth, json={"deviceId": "dev-9", "task": "open settings"})
        assert created.status_code == 200, created.text
        task_id = created.json()["id"]
        task = client.get(f"/v1/tasks/{task_id}", headers=auth).json()["task"]
        assert task["task"] == "open settings" and task["deviceId"] == "dev-9"
        assert client.get("/v1/tasks", headers=auth).json()["pagination"]["total"] == 1
        # another phone's token cannot see it
        other = invite_token(client)
        assert client.get(f"/v1/tasks/{task_id}", headers={"Authorization": f"Bearer {other}"}).status_code == 404
        # the app's Reasoning and Max steps choices apply, like the dashboard's task form
        # (once the first run is over: a busy phone refuses a second task)
        assert wait_for(lambda: client.get(f"/v1/tasks/{task_id}", headers=auth).json()["task"]["status"] in ("completed", "failed", "cancelled"))
        quick = client.post("/v1/tasks", headers=auth, json={"task": "go home", "reasoning": False, "maxSteps": 12})
        shown = client.get(f"/v1/tasks/{quick.json()['id']}", headers=auth).json()["task"]
        assert shown["reasoning"] is False and shown["maxSteps"] == 12
        stop.set()


def test_removing_the_phone_revokes_its_token(client):  # noqa: F811
    login(client)
    token = invite_token(client)
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "dev-5", "X-Device-Name": "Galaxy"}
    stop = threading.Event()
    with client.websocket_connect(JOIN, headers=headers) as ws:
        threading.Thread(target=fake_phone, args=(ws, stop), daemon=True).start()
        phone = next(p for p in client.get("/api/state").json()["phones"] if p["link"] == "app")
        stop.set()
    assert client.delete(f"/api/phones/{phone['id']}").status_code == 200
    assert client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_trajectory_uses_mobilerun_event_names():
    event = appconnect._trajectory_event({"kind": "action", "text": "Tap OK", "ts": "t"})
    assert event == {"event": "ExecutorActionEvent", "data": {"description": "Tap OK"}, "timestamp": "t"}


def app_phone(client) -> dict:  # noqa: F811
    return next(p for p in client.get("/api/state").json()["phones"] if p["link"] == "app")


def wait_for(predicate, seconds: float = 5) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_name_and_android_version_follow_the_app(client):  # noqa: F811
    login(client)
    token = invite_token(client)
    first = {"Authorization": f"Bearer {token}", "X-Device-ID": "dev-7", "X-Device-Name": "Xiaomi M2012K11AG"}
    stop = threading.Event()
    with client.websocket_connect(JOIN, headers=first) as ws:
        threading.Thread(target=fake_phone, args=(ws, stop), daemon=True).start()
        assert app_phone(client)["name"] == "Xiaomi M2012K11AG"
        stop.set()
    # the updated app sends the marketing name and Android version: the same phone picks them up
    newer = {**first, "X-Device-Name": "POCO F3", "X-Android-Version": "16"}
    stop = threading.Event()
    with client.websocket_connect(JOIN, headers=newer) as ws:
        threading.Thread(target=fake_phone, args=(ws, stop), daemon=True).start()
        phones = [p for p in client.get("/api/state").json()["phones"] if p["link"] == "app"]
        assert len(phones) == 1
        assert (phones[0]["name"], phones[0]["model"], phones[0]["android"]) == ("POCO F3", "POCO F3", "16")
        stop.set()


def test_back_home_recents_go_through_the_app(client):  # noqa: F811
    login(client)
    token = invite_token(client)
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "dev-8", "X-Device-Name": "Pixel 8"}
    calls: list = []
    stop = threading.Event()
    with client.websocket_connect(JOIN, headers=headers) as ws:
        threading.Thread(target=fake_phone, args=(ws, stop, calls), daemon=True).start()
        phone_id = app_phone(client)["id"]
        assert client.post(f"/api/phones/{phone_id}/key", json={"key": "home"}).status_code == 200
        assert ("global", {"action": 2}) in calls
        assert client.post(f"/api/phones/{phone_id}/key", json={"key": "power"}).status_code == 400
        stop.set()


def test_live_view_survives_a_failed_action(client, monkeypatch):  # noqa: F811
    login(client)
    token = invite_token(client)
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "dev-6", "X-Device-Name": "Pixel 7"}
    calls: list = []
    stop = threading.Event()
    with client.websocket_connect(JOIN, headers=headers) as ws:
        threading.Thread(target=fake_phone, args=(ws, stop, calls, {"global"}), daemon=True).start()
        with client.websocket_connect(f"/ws/phones/{app_phone(client)['id']}") as live:
            assert live.receive_json()["codec"] == "jpeg"
            live.receive_bytes()
            live.send_json({"t": "key", "key": "back"})  # the phone refuses this one
            live.send_json({"t": "touch", "a": 0, "x": 10, "y": 10})
            live.send_json({"t": "touch", "a": 1, "x": 10, "y": 10})
            assert wait_for(lambda: any(m == "tap" for m, _ in calls)), calls
        stop.set()


def test_live_view_uses_screenshots_only_and_skips_unchanged_screens(client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(appconnect, "FAST_FRAME_S", 0.02)
    monkeypatch.setattr(appconnect, "IDLE_FRAME_S", 0.02)
    monkeypatch.setattr(devices, "SHOT_SPACING_S", 0.0)
    login(client)
    token = invite_token(client)
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "dev-3", "X-Device-Name": "POCO F3"}
    same, changed = _jpeg(), _jpeg((200, 30, 30))
    calls: list = []
    stop = threading.Event()
    with client.websocket_connect(JOIN, headers=headers) as ws:
        threading.Thread(target=fake_phone, args=(ws, stop, calls),
                         kwargs={"screens": [same, same, same, same, changed]}, daemon=True).start()
        with client.websocket_connect(f"/ws/phones/{app_phone(client)['id']}") as live:
            assert live.receive_json()["codec"] == "jpeg"
            assert live.receive_bytes()[1:] == base64.b64decode(same)
            assert live.receive_bytes()[1:] == base64.b64decode(changed)  # the repeats were not sent
        stop.set()
    shots = [m for m, _ in calls if m == "screenshot"]
    assert len(shots) >= 5
    assert not any(m.startswith("stream/") for m, _ in calls)  # never Android screen sharing


def test_screenshots_of_one_phone_are_spaced(monkeypatch):
    """Android refuses accessibility screenshots taken too close together: queue them instead."""
    monkeypatch.setattr(devices, "SHOT_SPACING_S", 0.15)
    sent: list[float] = []

    class FakeWS:
        async def send_text(self, text: str) -> None:
            msg = json.loads(text)
            sent.append(time.monotonic())
            asyncio.get_running_loop().call_soon(
                conn.feed, {"id": msg["id"], "status": "success", "result": _jpeg()})

    conn = devices.DeviceConn(FakeWS(), "dev", "Phone")

    async def three() -> None:
        await asyncio.gather(*(conn.screenshot() for _ in range(3)))

    asyncio.run(three())
    gaps = [b - a for a, b in itertools.pairwise(sent)]
    assert len(sent) == 3 and min(gaps) >= 0.14, gaps


def test_drags_and_scrolls_past_the_edge_stay_on_screen(client):  # noqa: F811
    """Android refuses gestures with points off the screen (a drag that leaves the picture, a big
    wheel flick near the top): they are kept inside the screen instead."""
    login(client)
    token = invite_token(client)
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "dev-2", "X-Device-Name": "Pixel 6"}
    calls: list = []
    stop = threading.Event()
    with client.websocket_connect(JOIN, headers=headers) as ws:
        threading.Thread(target=fake_phone, args=(ws, stop, calls), daemon=True).start()
        with client.websocket_connect(f"/ws/phones/{app_phone(client)['id']}") as live:
            live.receive_json()
            live.receive_bytes()
            live.send_json({"t": "touch", "a": 0, "x": 100, "y": 20})
            live.send_json({"t": "touch", "a": 1, "x": 300, "y": -40})  # dragged off the top-right
            live.send_json({"t": "scroll", "x": 100, "y": 20, "v": 4})  # big flick near the top
            assert wait_for(lambda: len([m for m, _ in calls if m == "swipe"]) == 2), calls
        stop.set()
    for _, p in [c for c in calls if c[0] == "swipe"]:
        assert 0 <= p["startX"] < 1080 and 0 <= p["endX"] < 1080, p
        assert 0 <= p["startY"] < 2400 and 0 <= p["endY"] < 2400, p
