"""Everything the FastAutomate app talks to: phones connect themselves, no adb or VPN.

- ``GET /connect`` (from a dashboard QR) and ``GET|POST /connect/device`` (the app's "Connect with
  password" button): public pages that hand the app a token.
- ``GET /app/FastAutomate.apk``: the app download.
- ``WS /v1/providers/personal/join``: the app's live connection (token in the Authorization header).
- ``/v1/relay/{device}/...``: Portal-HTTP contract for the agent, relayed over that connection
  (local only).
- ``/v1/models``, ``/v1/tasks...``: the app's own prompt screen runs tasks on its phone.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from . import auth
from .devices import GLOBAL_ACTIONS, DeviceConn, DeviceError, DeviceHub, app_serial, device_id_of

log = logging.getLogger("mobile_rpa.appconnect")

APK = Path(__file__).with_name("fa-portal-v2.apk")
JOIN_PATH = "/v1/providers/personal/join"
INVITE_HOURS = 24
CALLBACK_SCHEMES = {"fastautomate2"}
APP_PACKAGE = "com.fastautomate.agent"
TASK_STATUS = {  # our run status -> what the app's task screen understands
    "queued": "created", "running": "running", "succeeded": "completed",
    "failed": "failed", "interrupted": "failed", "stopped": "cancelled",
}


def register(app: FastAPI, *, env, db, phones, runner, broker, devices: DeviceHub, start_task) -> str:
    """Add the routes; returns the relay key the runner hands to workers."""
    # derived from the install's secret: stable across restarts, never leaves the server
    relay_key = hmac.new(env.session_secret, b"relay", hashlib.sha256).hexdigest()
    public = env.public_url.rstrip("/")
    ws_url = public.replace("https://", "wss://").replace("http://", "ws://") + JOIN_PATH

    def new_token(device_id: str | None, hours: int | None) -> str:
        token = secrets.token_urlsafe(24)
        expires = (datetime.now(UTC) + timedelta(hours=hours)).isoformat() if hours else None
        db.create_app_token(token, device_id, expires)
        return token

    def token_device(request_or_ws) -> tuple[str, dict]:
        header = request_or_ws.headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip()
        row = db.app_token(token) if token else None
        if not row:
            raise HTTPException(401, "Unknown or revoked connect token")
        return token, row

    # ---- public pages ------------------------------------------------------------------------
    @app.get("/app/FastAutomate-v2.apk")
    async def download_app():
        if not APK.exists():
            raise HTTPException(404, "The app is not bundled on this server")
        return FileResponse(APK, media_type="application/vnd.android.package-archive", filename="FastAutomate-v2.apk")

    @app.get("/connect")
    async def invite_page(t: str = ""):
        row = db.app_token(t) if t else None
        valid = bool(row) and (row["device_id"] or not _expired(row))
        link = f"fastautomate2://connect?{urlencode({'token': t, 'url': ws_url})}"
        intent = (f"intent://connect?{urlencode({'token': t, 'url': ws_url})}"
                  f"#Intent;scheme=fastautomate2;package={APP_PACKAGE};end")
        body = f"""
        <ol class="steps">
          <li><b>Install the app</b><span>Download it, open the file and allow installing.</span>
            <a class="btn ghost" href="{public}/app/FastAutomate-v2.apk">Download FastAutomate v2</a></li>
          <li><b>Connect this phone</b><span>Opens the app and links it to the dashboard.</span>
            <a class="btn" href="{html.escape(intent)}" data-fallback="{html.escape(link)}">Connect</a></li>
          <li><b>Turn on the FastAutomate v2 service</b><span>In the app, tap <i>Accessibility Service</i> and switch it on. If the first FastAutomate app is also on this phone, switch its service off.
            If Android says the setting is restricted: Settings &gt; Apps &gt; FastAutomate v2 &gt; &#8942; &gt; Allow restricted settings, then try again.</span></li>
        </ol>""" if valid else "<p class='bad'>This link expired or was already used. Ask for a new one on the dashboard.</p>"
        return HTMLResponse(_page("Connect your phone", body, public))

    @app.get("/connect/device")
    async def device_login_page(deviceId: str = "", scheme: str = "fastautomate2"):
        return HTMLResponse(_page("Connect this phone", _login_form(deviceId, scheme, ""), public))

    @app.post("/connect/device")
    async def device_login(request: Request):
        form = dict(await request.form())
        device_id = str(form.get("deviceId", "")).strip()[:128]
        scheme = str(form.get("scheme", "fastautomate2"))
        if scheme not in CALLBACK_SCHEMES:
            scheme = "fastautomate2"
        if not auth.check_password(str(form.get("password", "")), env.password):
            await asyncio.sleep(0.6)
            return HTMLResponse(_page("Connect this phone", _login_form(device_id, scheme, "Wrong password"), public), 401)
        token = new_token(device_id or None, None)
        callback = f"{scheme}://auth-callback?{urlencode({'token': token, 'url': ws_url})}"
        intent = (f"intent://auth-callback?{urlencode({'token': token, 'url': ws_url})}"
                  f"#Intent;scheme={scheme};package={APP_PACKAGE};end")
        body = f"""<p>Signed in. Tap below to go back to the app.</p>
        <a class="btn" id="go" href="{html.escape(intent)}" data-fallback="{html.escape(callback)}">Open FastAutomate v2</a>
        <script>setTimeout(function(){{location.href={json.dumps(callback)};}}, 300);</script>"""
        return HTMLResponse(_page("Connected", body, public))

    # ---- dashboard: invite a phone -----------------------------------------------------------
    @app.post("/api/app/invite")
    async def invite():
        token = new_token(None, INVITE_HOURS)
        return {"url": f"{public}/connect?t={quote(token)}", "download": f"{public}/app/FastAutomate-v2.apk"}

    # ---- the app's live connection -------------------------------------------------------------
    @app.websocket(JOIN_PATH)
    async def join(ws: WebSocket):
        try:
            token, row = token_device(ws)
        except HTTPException:
            await ws.close(code=4401)
            return
        device_id = (ws.headers.get("x-device-id") or "").strip()[:128]
        if not device_id or (row["device_id"] and row["device_id"] != device_id) or (not row["device_id"] and _expired(row)):
            await ws.close(code=4403)
            return
        if not row["device_id"]:
            db.bind_app_token(token, device_id)
        await ws.accept()
        name = (ws.headers.get("x-device-name") or "Phone").strip()[:60]
        android = (ws.headers.get("x-android-version") or "").strip()[:12]
        await phones.add_app_phone(device_id, name, android)
        await devices.serve(ws, device_id, name)

    # ---- relay for the agent (Portal HTTP contract over the app connection) ---------------------
    @app.api_route("/v1/relay/{device_id}/{path:path}", methods=["GET", "POST"])
    async def relay(device_id: str, path: str, request: Request):
        if request.client is None or request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403)
        if request.headers.get("authorization", "") != f"Bearer {relay_key}":
            raise HTTPException(401)
        conn = _conn(devices, device_id)
        method = {"state_full": "state", "a11y_tree_full": "a11y_tree"}.get(path, path)
        try:
            if method == "screenshot":
                return Response(await conn.screenshot(960, 70), media_type="image/jpeg")
            params = dict(request.query_params) if request.method == "GET" else (await request.json() or {})
            if method == "state":
                params = {"filter": str(params.get("filter", "")).lower() == "true"}
            result = await conn.call(method, params, timeout=60)
        except DeviceError as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, 502)
        except TimeoutError:
            return JSONResponse({"status": "error", "error": "the phone did not answer in time"}, 504)
        return {"status": "success", "result": result}

    # ---- the app's own task screen: tasks for this phone only ----------------------------------
    def device_phone(request: Request) -> dict:
        _, row = token_device(request)
        phone = db.phone_by_serial(app_serial(row["device_id"] or ""))
        if not phone:
            raise HTTPException(404, "This phone is not on the dashboard")
        return phone

    def own_task(request: Request, task_id: str) -> tuple[dict, dict]:
        phone = device_phone(request)
        task = db.task(int(task_id)) if task_id.isdigit() else None
        if not task or not any(r["phone_id"] == phone["id"] for r in task["runs"]):
            raise HTTPException(404, "No such task")
        return phone, task

    @app.get("/v1/models")
    async def models(request: Request):
        device_phone(request)
        return {"models": ["fastautomate/agent"]}

    @app.post("/v1/tasks")
    async def create_task(request: Request):
        phone = device_phone(request)
        body = await request.json()
        prompt = str(body.get("task", "")).strip()
        if not prompt:
            raise HTTPException(400, "Write what the phone should do")
        reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), bool) else None
        steps = body.get("maxSteps")
        max_steps = steps if isinstance(steps, int) and 3 <= steps <= 200 else None
        task = await start_task(prompt, [{"phone_id": phone["id"], "instruction": prompt}], reasoning, max_steps)
        return {"id": str(task["id"]), "status": "created"}

    @app.get("/v1/tasks")
    async def list_tasks(request: Request, page: int = 1, pageSize: int = 20):
        phone = device_phone(request)
        size = min(max(pageSize, 1), 100)
        tasks, total = db.tasks_for_phone(phone["id"], size, (max(page, 1) - 1) * size)
        pages = max(1, (total + size - 1) // size)
        return {"items": [_app_task(t, phone) for t in tasks],
                "pagination": {"page": page, "pageSize": size, "total": total, "pages": pages,
                               "hasNext": page < pages, "hasPrev": page > 1}}

    @app.get("/v1/tasks/{task_id}")
    async def get_task(task_id: str, request: Request):
        phone, task = own_task(request, task_id)
        return {"task": _app_task(task, phone)}

    @app.get("/v1/tasks/{task_id}/status")
    async def task_status(task_id: str, request: Request):
        phone, task = own_task(request, task_id)
        return {"status": _app_task(task, phone)["status"]}

    @app.get("/v1/tasks/{task_id}/trajectory")
    async def trajectory(task_id: str, request: Request):
        phone, task = own_task(request, task_id)
        run = next(r for r in task["runs"] if r["phone_id"] == phone["id"])
        return {"trajectory": [_trajectory_event(e) for e in db.events(run["id"]) if e["kind"] not in ("phase", "status")]}

    @app.get("/v1/tasks/{task_id}/screenshots")
    async def screenshots(task_id: str, request: Request):
        own_task(request, task_id)
        return {"urls": []}

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str, request: Request):
        phone, task = own_task(request, task_id)
        for run in task["runs"]:
            if run["phone_id"] == phone["id"]:
                await runner.stop_run(run["id"])
        return {"status": "cancelling"}

    return relay_key


# ---- live view for app phones --------------------------------------------------------------------
# Accessibility screenshots, exactly what the agent sees: nothing shows on the phone (no screen
# sharing). Android allows about 3 a second; a frame goes out only when the screen changed, and the
# rate drops while nothing moves (any click or key brings it back up).
FAST_FRAME_S = 0.35
IDLE_FRAME_S = 1.0
IDLE_AFTER = 3  # unchanged frames in a row before slowing down
LIVE_SIDE, LIVE_QUALITY = 960, 60
FLAG_JPEG = 4


async def live_view(ws: WebSocket, conn: DeviceConn) -> None:
    """Live screen for an app phone. Clicks, drags, keys and typing become phone actions."""
    screen_w, screen_h = await conn.screen_size()
    size = {"w": 0, "h": 0}  # size of what the browser shows, for scaling its coordinates
    active = asyncio.Event()  # the user just did something: look again right away
    sender = await _start_frames(ws, conn, size, active)
    touch: dict = {}
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if not size["w"]:
                continue
            scale_x, scale_y = screen_w / size["w"], screen_h / size["h"]
            kind = msg.get("t")
            try:  # one refused action must not end the view
                if kind == "touch":
                    point = _on_screen(int(msg.get("x", 0)) * scale_x, int(msg.get("y", 0)) * scale_y,
                                       screen_w, screen_h)
                    action = int(msg.get("a", 0))
                    if action == 0:
                        touch = {"start": point, "end": point, "at": time.monotonic()}
                    elif action == 2 and touch:
                        touch["end"] = point
                    elif action == 1 and touch:
                        touch["end"] = point
                        gesture, touch = touch, {}
                        await _gesture(conn, gesture)
                        active.set()
                else:
                    await _control(conn, msg, scale_x, scale_y, (screen_w, screen_h))
                    active.set()
            except (DeviceError, TimeoutError) as exc:
                log.info("app phone %s: %s failed: %s", conn.device_id, kind, exc)
    finally:
        sender.cancel()


def _on_screen(x: float, y: float, width: int, height: int) -> tuple[int, int]:
    """Android refuses gestures with points off the screen (negative ones throw on the phone)."""
    return min(max(round(x), 0), width - 1), min(max(round(y), 0), height - 1)


async def _control(conn: DeviceConn, msg: dict, scale_x: float, scale_y: float, screen: tuple[int, int]) -> None:
    kind = msg.get("t")
    if kind == "key" and msg.get("key") in GLOBAL_ACTIONS:
        await conn.call("global", {"action": GLOBAL_ACTIONS[msg["key"]]})
    elif kind == "text" and msg.get("s"):
        encoded = base64.b64encode(str(msg["s"]).encode()).decode()
        await conn.call("keyboard/input", {"base64_text": encoded, "clear": False})
    elif kind == "scroll":
        width, height = screen
        x, y = _on_screen(int(msg.get("x", 0)) * scale_x, int(msg.get("y", 0)) * scale_y, width, height)
        _, end_y = _on_screen(x, y - float(msg.get("v", 0)) * height / 6, width, height)
        await conn.call("swipe", {"startX": x, "startY": y, "endX": x, "endY": end_y, "duration": 250})


async def _start_frames(ws: WebSocket, conn: DeviceConn, size: dict, active: asyncio.Event) -> asyncio.Task:
    first = await conn.screenshot(LIVE_SIDE, LIVE_QUALITY)
    await _send_meta(ws, size, first)
    await ws.send_bytes(bytes([FLAG_JPEG]) + first)

    async def frames() -> None:
        last, unchanged = first, 0
        while True:
            try:
                await asyncio.wait_for(active.wait(), IDLE_FRAME_S if unchanged >= IDLE_AFTER else FAST_FRAME_S)
                active.clear()
                unchanged = 0
            except TimeoutError:
                pass
            try:
                frame = await conn.screenshot(LIVE_SIDE, LIVE_QUALITY)
            except (DeviceError, TimeoutError):
                continue  # e.g. a secure screen or a busy phone: try again next round
            if frame == last:
                unchanged += 1
                continue
            last, unchanged = frame, 0
            if _jpeg_size(frame) != (size["w"], size["h"]):  # rotated
                await _send_meta(ws, size, frame)
            await ws.send_bytes(bytes([FLAG_JPEG]) + frame)

    return asyncio.create_task(frames())


async def _send_meta(ws: WebSocket, size: dict, frame: bytes) -> None:
    size["w"], size["h"] = _jpeg_size(frame)
    await ws.send_text(json.dumps({"type": "meta", "codec": "jpeg", "width": size["w"], "height": size["h"]}))


async def _gesture(conn: DeviceConn, touch: dict) -> None:
    (x0, y0), (x1, y1) = touch["start"], touch["end"]
    held_ms = int((time.monotonic() - touch["at"]) * 1000)
    if abs(x1 - x0) < 15 and abs(y1 - y0) < 15:
        if held_ms < 500:
            await conn.call("tap", {"x": x0, "y": y0})
        else:  # long press = a swipe that does not move
            await conn.call("swipe", {"startX": x0, "startY": y0, "endX": x0, "endY": y0, "duration": held_ms})
    else:
        await conn.call("swipe", {"startX": x0, "startY": y0, "endX": x1, "endY": y1,
                                  "duration": max(120, min(held_ms, 2000))})


def _jpeg_size(data: bytes) -> tuple[int, int]:
    import io

    from PIL import Image

    return Image.open(io.BytesIO(data)).size


def _conn(devices: DeviceHub, device_id: str) -> DeviceConn:
    try:
        return devices.get(device_id)
    except DeviceError as exc:
        raise HTTPException(503, str(exc)) from exc


def _expired(row: dict) -> bool:
    expires = row.get("expires_at")
    return bool(expires) and datetime.fromisoformat(expires) < datetime.now(UTC)


def _app_task(task: dict, phone: dict) -> dict:
    run = next((r for r in task["runs"] if r["phone_id"] == phone["id"]), task["runs"][0])
    status = TASK_STATUS.get(run["status"], "failed")
    return {
        "id": str(task["id"]), "status": status, "task": task["prompt"], "createdAt": task["created_at"],
        "finishedAt": run.get("ended_at"), "steps": run.get("steps") or 0,
        "succeeded": run["status"] == "succeeded" if status in ("completed", "failed", "cancelled") else None,
        "output": run.get("result") or None, "llmModel": "fastautomate/agent",
        "reasoning": bool(task["reasoning"]), "maxSteps": task["max_steps"],
        "deviceId": device_id_of(phone["serial"]),
    }


def _trajectory_event(event: dict) -> dict:
    """Our step log in mobilerun's event vocabulary, which the app knows how to show."""
    text = event["text"]
    mapping = {
        "plan": ("ManagerPlanDetailsEvent", {"plan": text}),
        "step": ("ManagerPlanDetailsEvent", {"subgoal": text}),
        "answer": ("ManagerPlanDetailsEvent", {"answer": text}),
        "action": ("ExecutorActionEvent", {"description": text}),
        "ok": ("ExecutorActionResultEvent", {"success": True, "summary": text}),
        "error": ("ExecutorActionResultEvent", {"success": False, "summary": text}),
        "think": ("FastAgentResponseEvent", {"thought": text}),
        "done": ("ResultEvent", {"success": True, "reason": text}),
        "fail": ("ResultEvent", {"success": False, "reason": text}),
    }
    name, data = mapping.get(event["kind"], ("InfoEvent", {"message": text}))
    return {"event": name, "data": data, "timestamp": event["ts"]}


# ---- small FA-styled pages for the phone's browser -----------------------------------------------
def _login_form(device_id: str, scheme: str, error: str) -> str:
    return f"""<p>Enter the team password to link this phone to the FastAutomate dashboard.</p>
    <form method="post" action="device">
      <input type="hidden" name="deviceId" value="{html.escape(device_id)}">
      <input type="hidden" name="scheme" value="{html.escape(scheme)}">
      <input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus>
      <button class="btn" type="submit">Connect</button>
      <p class="bad">{html.escape(error)}</p>
    </form>"""


def _page(title: str, body: str, base: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} - FastAutomate</title><link rel="icon" href="{base}/logo.png">
<style>
:root{{--ground:#F4F3EE;--surface:#fff;--ink:#1D2520;--muted:#6E7A72;--line:#E2E2D9;--accent:#12904F;--accent-soft:#E1F1E7;--red:#B3372F}}
@media (prefers-color-scheme:dark){{:root{{--ground:#0F1512;--surface:#171E1A;--ink:#E7EDE7;--muted:#8B978D;--line:#26302A;--accent:#2FB56F;--accent-soft:#12321F;--red:#E06A5F}}}}
*{{box-sizing:border-box;margin:0}}body{{font:15px/1.5 system-ui,-apple-system,Roboto,sans-serif;background:var(--ground);color:var(--ink);padding:24px 16px}}
.card{{max-width:440px;margin:0 auto;background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:24px}}
.brand{{display:flex;align-items:center;gap:10px;font-weight:700;font-size:17px;margin-bottom:18px}}.brand img{{width:36px;height:36px;border-radius:10px}}
h1{{font-size:21px;margin-bottom:8px}}p{{color:var(--muted);margin:6px 0 14px}}
.steps{{padding-left:0;list-style:none;counter-reset:s;display:flex;flex-direction:column;gap:16px}}
.steps li{{counter-increment:s;display:flex;flex-direction:column;gap:6px;padding-left:40px;position:relative}}
.steps li::before{{content:counter(s);position:absolute;left:0;top:0;width:28px;height:28px;border-radius:50%;background:var(--accent-soft);color:var(--accent);font-weight:700;display:grid;place-items:center}}
.steps span{{color:var(--muted);font-size:13.5px}}
input{{width:100%;font:inherit;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--ground);color:var(--ink);margin-bottom:12px}}
.btn{{display:block;text-align:center;width:100%;border:0;border-radius:12px;background:var(--accent);color:#fff;font:600 15px system-ui;padding:13px;text-decoration:none;cursor:pointer}}
.btn.ghost{{background:var(--accent-soft);color:var(--accent)}}.bad{{color:var(--red);font-weight:600}}
</style></head><body><div class="card"><div class="brand"><img src="{base}/logo.png" alt="">FastAutomate</div>
<h1>{html.escape(title)}</h1>{body}</div>
<script>/* Android browsers without intent:// support fall back to the plain app link */
document.querySelectorAll('[data-fallback]').forEach(function(a){{if(!/android/i.test(navigator.userAgent))a.href=a.dataset.fallback;}});</script>
</body></html>"""
