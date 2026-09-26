"""FastAPI app: REST + SSE for the dashboard. v2: phones join through the FastAutomate v2 app and run
tasks themselves; this dashboard plans, assigns and records what they report."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import appconnect, auth
from .db import Db
from .devices import GLOBAL_ACTIONS, DeviceConn, DeviceError, DeviceHub, app_serial, device_id_of
from .events import Broker
from .orchestrator import Orchestrator
from .phone_keys import PhoneKeys, key_mode
from .phones import PhoneRegistry
from .settings import RUNTIME_DEFAULTS, SECRET_SETTINGS, Env, load_env
from .splitter import SplitError, credit_left, split, test_connection

log = logging.getLogger("mobile_rpa")
STATIC = Path(__file__).with_name("static")
LOW_CREDIT = 0.05  # USD: below this a task would die on its first model calls
CREDIT_POLL_SECONDS = 120
MAX_PHONES_PER_TASK = 50


class LoginBody(BaseModel):
    password: str


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
    out["ready"] = bool(values.get("api_key"))
    return out


def create_app(env: Env | None = None) -> FastAPI:
    env = env or load_env()
    db = Db(env.db_path)
    broker = Broker()
    devices = DeviceHub()  # phones connected through the FastAutomate v2 app
    phones = PhoneRegistry(db, broker, devices)
    devices.on_change = phones.publish
    orchestrator = Orchestrator(db, broker, phones, devices, env.data_dir / "shots")
    devices.on_message = orchestrator.on_phone_message
    phone_keys = PhoneKeys(db)
    credit: dict = {"value": None}
    jobs: set[asyncio.Task] = set()

    def spawn(coro) -> None:
        job = asyncio.get_running_loop().create_task(coro)
        jobs.add(job)
        job.add_done_callback(jobs.discard)

    async def refresh_credit() -> float | None:
        value = await credit_left(db.settings())
        if value != credit["value"]:
            credit["value"] = value
            broker.publish("credit", {"credit": value, "low": _low(value)})
        return value

    async def after_run(run: dict) -> None:
        with contextlib.suppress(Exception):
            await refresh_credit()

    orchestrator.on_finish = after_run

    async def credit_loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await refresh_credit()
            await asyncio.sleep(CREDIT_POLL_SECONDS)

    async def give_key(device_id: str, conn: DeviceConn, presented_hash: str) -> None:
        phone = db.phone_by_serial(app_serial(device_id))
        if phone:
            problem = await phone_keys.ensure(phone["id"], conn, presented_hash)
            phones.set_note(phone["serial"], problem or "")

    def on_phone_ready(device_id: str, conn: DeviceConn, presented_hash: str) -> None:
        spawn(give_key(device_id, conn, presented_hash))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        orchestrator.resume()
        poller = asyncio.create_task(credit_loop())
        yield
        poller.cancel()
        orchestrator.close()

    app = FastAPI(title="Mobile RPA v2", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.db, app.state.phones, app.state.devices = db, phones, devices
    app.state.orchestrator, app.state.phone_keys = orchestrator, phone_keys

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
            auth.COOKIE, auth.issue(env.session_secret), max_age=auth.SESSION_SECONDS, httponly=True,
            samesite="lax", secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
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
        return StreamingResponse(broker.stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- phones --------------------------------------------------------------------------
    @app.patch("/api/phones/{phone_id}")
    async def rename_phone(phone_id: int, body: RenameBody):
        phone_or_404(phone_id)
        await phones.rename(phone_id, body.name)
        return {"ok": True}

    @app.delete("/api/phones/{phone_id}")
    async def remove_phone(phone_id: int):
        phone = phone_or_404(phone_id)
        if phone["serial"] in phones.busy:
            raise HTTPException(409, "Stop the task running on this phone first.")
        await phone_keys.forget(phone)
        await phones.remove(phone_id)
        return {"ok": True}

    @app.get("/api/phones/{phone_id}/screen.jpg")
    async def screen(phone_id: int):
        phone = phone_or_404(phone_id)
        try:
            jpeg = await phones.thumbnail(phone["serial"])
        except (DeviceError, TimeoutError) as exc:
            raise HTTPException(503, f"Screen not available: {exc}") from exc
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/phones/{phone_id}/key")
    async def press_key(phone_id: int, body: KeyBody):
        phone = phone_or_404(phone_id)
        if body.key not in GLOBAL_ACTIONS:
            raise HTTPException(400, "Unknown key")
        try:
            await devices.get(device_id_of(phone["serial"]) or "").call("global", {"action": GLOBAL_ACTIONS[body.key]})
        except (DeviceError, TimeoutError) as exc:
            raise HTTPException(503, str(exc) or "The phone did not answer") from exc
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
        return {"instructions": [{"phone_id": p["id"], "name": p["name"], "instruction": line}
                                 for p, line in zip(picked, lines, strict=True)]}

    @app.post("/api/tasks")
    async def create_task(body: TaskBody):
        specs = [{"phone_id": r.phone_id, "instruction": r.instruction} for r in body.runs]
        return await launch(body.prompt, specs, body.reasoning, body.max_steps)

    async def launch(prompt: str, specs: list[dict], reasoning: bool | None = None, max_steps: int | None = None) -> dict:
        """Start a task on phones (dashboard, or a phone's own app): same checks for both."""
        values = db.settings()
        if _low(credit["value"]):
            value = await refresh_credit()  # maybe they just topped up
            if _low(value):
                raise HTTPException(402, f"The AI credit is empty (${value:.2f} left), so the task would stop on its "
                                         "first step. Add credit at openrouter.ai/settings/credits, then press Run again.")
        picked = {p["id"]: p for p in pick_phones([sp["phone_id"] for sp in specs])}
        for phone in picked.values():
            if not phone["key_hash"]:
                raise HTTPException(409, f"{phone['name']} has no AI key yet. Add the OpenRouter management key in "
                                         "Settings, then keep the phone connected for a moment.")
        runs = [{"phone_id": sp["phone_id"], "serial": picked[sp["phone_id"]]["serial"],
                 "phone_name": picked[sp["phone_id"]]["name"], "instruction": sp["instruction"].strip()}
                for sp in specs if sp["phone_id"] in picked]
        reasoning = values.get("reasoning") != "0" if reasoning is None else reasoning
        max_steps = max_steps or int(values.get("max_steps") or 30)
        task_id = db.create_task(prompt.strip(), reasoning, max_steps, runs)
        orchestrator.start_task(task_id)
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
        return {"stopped": [r["id"] for r in task["runs"] if await orchestrator.stop_run(r["id"])]}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: int):
        if not await orchestrator.stop_run(run_id):
            raise HTTPException(409, "That run is not running.")
        return {"ok": True}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: int):
        return db.events(run_id)

    @app.get("/api/runs/{run_id}/shot.jpg")
    async def run_shot(run_id: int):
        path = env.data_dir / "shots" / f"{run_id}.jpg"
        if not path.exists():
            raise HTTPException(404, "No final screenshot for this run")
        return FileResponse(path, media_type="image/jpeg")

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
            if key == "base_url" and not text.startswith(("http://", "https://")):
                raise HTTPException(400, "Base URL must start with http:// or https://")
            if key in ("max_steps", "timeout_minutes") and not text.isdigit():
                raise HTTPException(400, f"{key} must be a whole number")
            if key == "daily_cap_usd":
                try:
                    cap = float(text)
                except ValueError:
                    raise HTTPException(400, "The daily cap must be a number of dollars") from None
                if not 0.05 <= cap <= 100:
                    raise HTTPException(400, "The daily cap must be between $0.05 and $100")
                text = f"{cap:.2f}"
            clean[key] = text[:500]
        before = db.settings()
        db.set_settings(clean)
        if clean.keys() & {"api_key", "management_key"}:
            spawn(refresh_credit())
        if key_mode(db.settings()) != key_mode(before):  # new provider or key: connected phones get it now
            for phone in db.phones():
                device_id = device_id_of(phone["serial"]) or ""
                if devices.connected(device_id):
                    spawn(give_key(device_id, devices.get(device_id), ""))
        if "daily_cap_usd" in clean and clean["daily_cap_usd"] != before.get("daily_cap_usd"):
            spawn(phone_keys.apply_cap(float(clean["daily_cap_usd"])))
        values = masked_settings(db.settings())
        broker.publish("settings", values)
        return values

    @app.post("/api/settings/test")
    async def test_settings():
        values = db.settings()
        credit_value = await credit_left(values)
        try:
            reply = await test_connection(values)
        except SplitError as exc:
            return {"ok": False, "error": str(exc), "credit": credit_value}
        return {"ok": True, "reply": reply, "credit": credit_value}

    # ---- live view (silent accessibility screenshots) --------------------------------------
    @app.websocket("/ws/phones/{phone_id}")
    async def live(ws: WebSocket, phone_id: int):
        if not authed(ws.cookies):
            await ws.close(code=4401)
            return
        phone = db.phone(phone_id)
        if not phone:
            await ws.close(code=4404)
            return
        await ws.accept()
        try:
            await appconnect.live_view(ws, devices.get(device_id_of(phone["serial"]) or ""))
        except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
            pass
        except (DeviceError, TimeoutError) as exc:
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps({"type": "error", "error": f"Live view failed: {exc}"}))
                await ws.close()

    # Cloudflare caches .js/.css by URL, so every deploy gets new asset URLs.
    version = hashlib.sha1(b"".join((STATIC / name).read_bytes() for name in ("app.js", "app.css"))).hexdigest()[:10]
    index_html = ((STATIC / "index.html").read_text()
                  .replace('href="app.css"', f'href="app.css?v={version}"')
                  .replace('src="app.js"', f'src="app.js?v={version}"'))

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

    appconnect.register(app, env=env, db=db, phones=phones, orchestrator=orchestrator, devices=devices,
                        start_task=launch, on_phone_ready=on_phone_ready)
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
