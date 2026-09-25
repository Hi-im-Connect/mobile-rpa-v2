"""FastAPI app: REST + SSE for the dashboard, a websocket per live phone view."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import os
import time
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import appconnect, auth, scrcpy
from .adb import Adb, AdbError
from .db import Db
from .devices import GLOBAL_ACTIONS, DeviceError, DeviceHub, device_id_of
from .events import Broker
from .pairing import QrPairing
from .phones import PhoneRegistry
from .runner import Runner
from .settings import FORMATS, RUNTIME_DEFAULTS, SECRET_SETTINGS, Env, llm, load_env
from .splitter import SplitError, credit_left, split, test_connection

log = logging.getLogger("mobile_rpa")
STATIC = Path(__file__).with_name("static")
LOW_CREDIT = 0.05  # USD: below this a task would die on its first model calls
CREDIT_POLL_SECONDS = 120
KEYCODES = {"back": 4, "home": 3, "recents": 187, "enter": 66, "delete": 67, "power": 26}
MAX_PHONES_PER_TASK = 50


# ---- request bodies ------------------------------------------------------------------------
class LoginBody(BaseModel):
    password: str


class PhoneBody(BaseModel):
    address: str = Field(default="", max_length=120)
    name: str = Field(default="", max_length=60)
    pair_address: str = Field(default="", max_length=120)
    pair_code: str = Field(default="", max_length=12)


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class SplitBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    phone_ids: list[int] = Field(min_length=1, max_length=MAX_PHONES_PER_TASK)


class RunSpec(BaseModel):
    phone_id: int
    instruction: str = Field(min_length=1, max_length=4000)


class TaskBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    reasoning: bool = True
    max_steps: int = Field(default=30, ge=3, le=200)
    runs: list[RunSpec] = Field(min_length=1, max_length=MAX_PHONES_PER_TASK)


class KeyBody(BaseModel):
    key: str


def masked_settings(values: dict[str, str]) -> dict[str, str | bool]:
    out: dict[str, str | bool] = {k: v for k, v in values.items() if k not in SECRET_SETTINGS}
    for name in SECRET_SETTINGS:
        key = values.get(name, "")
        out[f"{name}_set"] = bool(key)
        out[f"{name}_hint"] = f"...{key[-4:]}" if len(key) >= 8 else ""
    out["ready"] = bool(llm(values)["key"])  # the active provider has a key
    return out


def create_app(env: Env | None = None, adb: Adb | None = None) -> FastAPI:
    env = env or load_env()
    db = Db(env.db_path)
    adb = adb or Adb(env.adb)
    broker = Broker()
    phones = PhoneRegistry(db, adb, broker)
    runner = Runner(db, broker, phones)
    hub = scrcpy.StreamHub(adb)
    devices = DeviceHub()  # phones that connected themselves through the FastAutomate app
    phones.devices = devices
    devices.on_change = phones.publish
    credit: dict = {"value": None}

    async def refresh_credit() -> float | None:
        value = await credit_left(db.settings())
        if value != credit["value"]:
            credit["value"] = value
            broker.publish("credit", {"credit": value, "low": _low(value)})
        return value

    runner.on_finish = refresh_credit

    async def credit_loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await refresh_credit()
            await asyncio.sleep(CREDIT_POLL_SECONDS)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        interrupted = db.interrupt_orphans()
        if interrupted:
            log.warning("marked %d runs from the previous process as interrupted", interrupted)
        phones.start()
        poller = asyncio.create_task(credit_loop())
        with contextlib.suppress(Exception):
            await runner.fill_spares()
        yield
        poller.cancel()
        await runner.shutdown()
        await hub.close()
        await phones.stop()

    app = FastAPI(title="Mobile RPA", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.db, app.state.phones, app.state.runner, app.state.hub = db, phones, runner, hub

    def authed(request_cookies: dict) -> bool:
        return auth.valid(env.session_secret, request_cookies.get(auth.COOKIE))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        public = path in ("/api/login", "/api/commerce/balance")  # balance: asked by the phone app
        if path.startswith("/api/") and not public and not authed(request.cookies):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    def phone_or_404(phone_id: int) -> dict:
        phone = db.phone(phone_id)
        if not phone:
            raise HTTPException(404, "No such phone")
        return phone

    # ---- auth ----------------------------------------------------------------------------
    @app.post("/api/login")
    async def login(body: LoginBody, request: Request):
        if not auth.check_password(body.password, env.password):
            await asyncio.sleep(0.6)  # slows guessing without a lockout to manage
            return JSONResponse({"error": "Wrong password"}, status_code=401)
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.COOKIE,
            auth.issue(env.session_secret),
            max_age=auth.SESSION_SECONDS,
            httponly=True,
            samesite="lax",
            secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
        )
        return response

    @app.post("/api/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(auth.COOKIE)
        return response

    # ---- state + events ------------------------------------------------------------------
    @app.get("/api/state")
    async def state():
        active_task_ids = sorted({r["task_id"] for r in db.active_runs()}, reverse=True)
        return {
            "phones": phones.all(),
            "active_tasks": [db.task(t) for t in active_task_ids],
            "stats": db.stats_today(),
            "settings": masked_settings(db.settings()),
            "credit": {"credit": credit["value"], "low": _low(credit["value"])},
        }

    @app.post("/api/credit/refresh")
    async def credit_refresh():
        value = await refresh_credit()
        return {"credit": value, "low": _low(value)}

    @app.get("/api/events")
    async def events():
        return StreamingResponse(
            broker.stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- phones --------------------------------------------------------------------------
    @app.post("/api/phones")
    async def add_phone(body: PhoneBody):
        try:
            if not body.address.strip() and not body.pair_code.strip():
                raise AdbError("Type the phone's address, or pair it with the code from the phone.")
            return await phones.add(body.address, body.name, body.pair_address, body.pair_code)
        except AdbError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ---- QR pairing: scan and done (pairing.py finds the phone) -----------------------------
    pairings: dict[str, QrPairing] = {}

    async def paired(serial: str) -> dict:
        return await phones.add(serial)

    @app.post("/api/pair/qr")
    async def new_qr():
        for key in [k for k, p in pairings.items() if time.monotonic() - p.created > 900]:
            pairings.pop(key).cancel()
        pairing = QrPairing(adb, env.netbird_peers_url, paired)
        pairings[pairing.id] = pairing
        pairing.start()
        return {"qr_id": pairing.id, "svg": _qr_svg(pairing.qr_text), **pairing.view()}

    @app.get("/api/pair/qr/{qr_id}")
    async def qr_status(qr_id: str):
        pairing = pairings.get(qr_id)
        if not pairing:
            raise HTTPException(404, "This QR code expired. Open Add phone again.")
        return pairing.view()

    @app.delete("/api/pair/qr/{qr_id}")
    async def qr_cancel(qr_id: str):
        pairing = pairings.pop(qr_id, None)
        if pairing:
            pairing.cancel()
        return {"ok": True}

    @app.patch("/api/phones/{phone_id}")
    async def rename_phone(phone_id: int, body: RenameBody):
        phone_or_404(phone_id)
        await phones.rename(phone_id, body.name)
        return {"ok": True}

    @app.delete("/api/phones/{phone_id}")
    async def remove_phone(phone_id: int):
        phone_or_404(phone_id)
        try:
            await phones.remove(phone_id)
        except AdbError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/phones/{phone_id}/prepare")
    async def prepare_phone(phone_id: int):
        phone = phone_or_404(phone_id)
        asyncio.create_task(phones.prepare(phone["serial"]))
        return {"ok": True}

    @app.get("/api/phones/{phone_id}/screen.jpg")
    async def screen(phone_id: int):
        phone = phone_or_404(phone_id)
        try:
            jpeg = await phones.thumbnail(phone["serial"], hub)
        except Exception as exc:  # no PNG fallback: a missing thumbnail beats a 2 MB transfer
            raise HTTPException(503, f"Screen not available: {exc}") from exc
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/phones/{phone_id}/key")
    async def press_key(phone_id: int, body: KeyBody):
        phone = phone_or_404(phone_id)
        code = KEYCODES.get(body.key)
        if code is None:
            raise HTTPException(400, "Unknown key")
        device_id = device_id_of(phone["serial"])
        if device_id is not None:  # app phone: no adb, the app presses it
            if body.key not in GLOBAL_ACTIONS:
                raise HTTPException(400, "That key is not available on phones connected through the app")
            try:
                await devices.get(device_id).call("global", {"action": GLOBAL_ACTIONS[body.key]})
            except (DeviceError, TimeoutError) as exc:
                raise HTTPException(503, str(exc) or "The phone did not answer") from exc
            return {"ok": True}
        try:
            await adb.shell(phone["serial"], f"input keyevent {code}")
        except AdbError as exc:
            raise HTTPException(503, str(exc)) from exc
        return {"ok": True}

    # ---- tasks ---------------------------------------------------------------------------
    def pick_phones(ids: list[int]) -> list[dict]:
        picked, seen = [], set()
        for phone_id in ids:
            if phone_id in seen:
                continue
            seen.add(phone_id)
            phone = phone_or_404(phone_id)
            status = phones.status(phone["serial"])
            if status != "online":
                raise HTTPException(409, f"{phone['name']} is {status}, pick an online phone.")
            picked.append(phone)
        return picked

    @app.post("/api/split")
    async def split_task(body: SplitBody):
        picked = pick_phones(body.phone_ids)
        try:
            lines = await split(body.prompt, [p["name"] for p in picked], db.settings())
        except SplitError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {
            "instructions": [
                {"phone_id": p["id"], "name": p["name"], "instruction": line}
                for p, line in zip(picked, lines, strict=True)
            ]
        }

    @app.post("/api/tasks")
    async def create_task(body: TaskBody):
        specs = [{"phone_id": r.phone_id, "instruction": r.instruction} for r in body.runs]
        return await launch(body.prompt, specs, body.reasoning, body.max_steps)

    async def launch(prompt: str, specs: list[dict], reasoning: bool | None = None, max_steps: int | None = None) -> dict:
        """Start a task (dashboard or a phone's own app): same checks for both."""
        values = db.settings()
        if not llm(values)["key"]:
            raise HTTPException(400, "Add an API key in Settings first.")
        if _low(credit["value"]):
            value = await refresh_credit()  # maybe they just topped up
            if _low(value):
                raise HTTPException(
                    402,
                    f"The AI credit is empty (${value:.2f} left), so the task would stop on its first step. "
                    "Add credit at openrouter.ai/settings/credits, then press Run again.",
                )
        picked = {p["id"]: p for p in pick_phones([sp["phone_id"] for sp in specs])}
        runs = [
            {
                "phone_id": sp["phone_id"],
                "serial": picked[sp["phone_id"]]["serial"],
                "phone_name": picked[sp["phone_id"]]["name"],
                "instruction": sp["instruction"].strip(),
            }
            for sp in specs
            if sp["phone_id"] in picked
        ]
        reasoning = values.get("reasoning") != "0" if reasoning is None else reasoning
        max_steps = max_steps or int(values.get("max_steps") or 30)
        task_id = db.create_task(prompt.strip(), reasoning, max_steps, runs)
        runner.start_task(task_id)
        task = db.task(task_id)
        broker.publish("task", task)
        return task

    @app.get("/api/tasks")
    async def list_tasks(limit: int = 30, offset: int = 0):
        return db.tasks(min(max(limit, 1), 100), max(offset, 0))

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: int):
        task = db.task(task_id)
        if not task:
            raise HTTPException(404, "No such task")
        for run in task["runs"]:
            run["events"] = db.events(run["id"])
        return task

    @app.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: int):
        task = db.task(task_id)
        if not task:
            raise HTTPException(404, "No such task")
        stopped = [r["id"] for r in task["runs"] if await runner.stop_run(r["id"])]
        return {"stopped": stopped}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: int):
        if not await runner.stop_run(run_id):
            raise HTTPException(409, "That run is not running.")
        return {"ok": True}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: int):
        return db.events(run_id)

    # ---- settings ------------------------------------------------------------------------
    @app.get("/api/settings")
    async def get_settings():
        return masked_settings(db.settings())

    @app.put("/api/settings")
    async def put_settings(body: dict):
        clean: dict[str, str] = {}
        for key, value in body.items():
            if key not in RUNTIME_DEFAULTS or value is None:
                continue
            text = str(value).strip()
            if key in SECRET_SETTINGS and not text:
                continue  # blank means "keep the current key"
            if key == "provider" and text not in FORMATS:
                raise HTTPException(400, "Format must be openai or anthropic")
            if key == "api_key" and text.startswith("sk-ant-oat"):
                raise HTTPException(
                    400,
                    "That is a Claude subscription token (Claude app / Claude Code). Anthropic only lets "
                    "Claude Code use it. Use an API key from console.anthropic.com (starts with sk-ant-api).",
                )
            if key in ("max_steps", "timeout_minutes") and not text.isdigit():
                raise HTTPException(400, f"{key} must be a whole number")
            if key == "base_url" and not text.startswith(("http://", "https://")):
                raise HTTPException(400, "Base URL must start with http:// or https://")
            clean[key] = text[:500]
        db.set_settings(clean)
        if clean.keys() & {"api_key", "base_url", "provider"}:
            asyncio.create_task(refresh_credit())
        values = masked_settings(db.settings())
        broker.publish("settings", values)
        return values

    @app.post("/api/settings/test")
    async def test_settings():
        values = db.settings()
        credit = await credit_left(values)
        try:
            reply = await test_connection(values)
        except SplitError as exc:
            return {"ok": False, "error": str(exc), "credit": credit}
        return {"ok": True, "reply": reply, "credit": credit}

    # ---- live stream ---------------------------------------------------------------------
    @app.websocket("/ws/phones/{phone_id}")
    async def live(ws: WebSocket, phone_id: int, h265: int = 0):
        if not authed(ws.cookies):
            await ws.close(code=4401)
            return
        phone = db.phone(phone_id)
        if not phone:
            await ws.close(code=4404)
            return
        await ws.accept()
        device_id = device_id_of(phone["serial"])
        if device_id is not None:  # app-connected phone: JPEG frames over its own connection
            try:
                await appconnect.live_view(ws, devices.get(device_id))
            except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
                pass
            except (DeviceError, TimeoutError) as exc:
                with contextlib.suppress(Exception):
                    await ws.send_text(json.dumps({"type": "error", "error": f"Live view failed: {exc}"}))
                    await ws.close()
            return
        try:
            session, viewer = await hub.join(phone["serial"], h265=bool(h265))
        except Exception as exc:
            await ws.send_text(json.dumps({"type": "error", "error": f"Live view failed: {exc}"}))
            await ws.close()
            return
        sender = asyncio.create_task(_send_frames(ws, viewer))
        try:
            while True:
                message = await ws.receive_text()
                packet = control_packet(json.loads(message), session)
                if packet:
                    await session.send(packet)
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        finally:
            sender.cancel()
            hub.leave(phone["serial"], viewer)

    # Cloudflare caches .js/.css by URL, so every deploy gets new asset URLs.
    version = hashlib.sha1(
        b"".join((STATIC / name).read_bytes() for name in ("app.js", "app.css"))
    ).hexdigest()[:10]
    index_html = (
        (STATIC / "index.html").read_text()
        .replace('href="app.css"', f'href="app.css?v={version}"')
        .replace('src="app.js"', f'src="app.js?v={version}"')
    )

    @app.get("/")
    async def index():
        return HTMLResponse(index_html, headers={"Cache-Control": "no-store"})

    @app.get("/api/qr")
    async def qr_svg(text: str):
        if not text or len(text) > 500:
            raise HTTPException(400, "Nothing to encode")
        return {"svg": _qr_svg(text)}

    @app.get("/api/commerce/balance")
    async def app_balance():
        raise HTTPException(404, "Credits are managed on the dashboard")

    relay_key = appconnect.register(
        app, env=env, db=db, phones=phones, runner=runner, broker=broker, devices=devices, start_task=launch
    )
    runner.relay = (f"http://127.0.0.1:{os.environ.get('MRPA_PORT', '8090')}", relay_key)

    app.mount("/", StaticFiles(directory=STATIC), name="static")
    return app


def _qr_svg(text: str) -> str:
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def _low(value: float | None) -> bool:
    return value is not None and value < LOW_CREDIT


async def _send_frames(ws: WebSocket, viewer: scrcpy.Viewer) -> None:
    with contextlib.suppress(Exception):
        while True:
            kind, payload = await viewer.queue.get()
            if kind == "frame":
                await ws.send_bytes(payload)
            elif kind == "meta":
                await ws.send_text(json.dumps(payload))
            else:
                await ws.send_text(json.dumps({"type": "end"}))
                await ws.close()
                return


def control_packet(msg: dict, session: scrcpy.ScrcpySession) -> bytes | None:
    """Browser input (coordinates in video pixels) -> scrcpy control message."""
    kind = msg.get("t")
    w, h = session.width, session.height
    if kind == "touch":
        action = int(msg.get("a", 0))
        if action not in (0, 1, 2):
            return None
        x = min(max(int(msg.get("x", 0)), 0), w)
        y = min(max(int(msg.get("y", 0)), 0), h)
        return scrcpy.pack_touch(action, x, y, w, h)
    if kind == "scroll":
        x, y = int(msg.get("x", 0)), int(msg.get("y", 0))
        return scrcpy.pack_scroll(x, y, w, h, float(msg.get("h", 0)), float(msg.get("v", 0)))
    if kind == "key":
        code = KEYCODES.get(str(msg.get("key")))
        if code is None:
            return None
        return scrcpy.pack_key(scrcpy.KEY_DOWN, code) + scrcpy.pack_key(scrcpy.KEY_UP, code)
    if kind == "text":
        text = str(msg.get("s", ""))
        return scrcpy.pack_text(text) if text else None
    return None
