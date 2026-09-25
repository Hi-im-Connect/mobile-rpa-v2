"""Tasks run on the phones themselves. The dashboard sends ``agent/run``; the phone's FastAutomate v2
app does every step and reports with ``agent/started`` (its own runs), ``agent/event`` and
``agent/finished``. Every report is acked with ``agent/ack`` so the phone can drop it from its
outbox; a report that arrives twice (after a reconnect) is stored once."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from .agent_prompts import run_payload
from .db import Db, now
from .devices import DeviceError, DeviceHub, app_serial, device_id_of
from .events import Broker

log = logging.getLogger("mobile_rpa.orchestrator")

ACTIVE = ("queued", "running")
FINAL = ("succeeded", "failed", "stopped")
SILENCE_GRACE_S = 300  # after the time limit, how long a silent phone gets before its run is failed


class Orchestrator:
    def __init__(self, db: Db, broker: Broker, phones, devices: DeviceHub, shots_dir: Path) -> None:
        self.db, self.broker, self.phones, self.devices = db, broker, phones, devices
        self.shots_dir = shots_dir
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.on_finish: Callable[[dict], Awaitable[None]] | None = None
        self._watchdogs: dict[int, asyncio.Task] = {}
        self._jobs: set[asyncio.Task] = set()

    # ---- dashboard -> phone ----------------------------------------------------------------
    def start_task(self, task_id: int) -> None:
        task = self.db.task(task_id)
        if not task:
            return
        for run in task["runs"]:
            if run["status"] == "queued":
                self.phones.busy[run["serial"]] = run["id"]
                self._spawn(self._send_run(task, run))
        self.phones.publish()

    async def _send_run(self, task: dict, run: dict) -> None:
        settings = self.db.settings()
        self._update(run, status="running", started_at=now())
        self._event(run, "phase", "Sending the task to the phone")
        try:
            conn = self.devices.get(device_id_of(run["serial"]) or "")
            await conn.call("agent/run", run_payload(task, run, settings), timeout=20)
        except (DeviceError, TimeoutError) as exc:
            self._finish(run, "failed", f"The phone did not take the task: {exc or 'no answer'}")
            return
        self._watch(run["id"], self._deadline_s(settings))

    async def stop_run(self, run_id: int) -> bool:
        run = self.db.run(run_id)
        if not run or run["status"] not in ACTIVE:
            return False
        try:
            conn = self.devices.get(device_id_of(run["serial"]) or "")
            await conn.call("agent/stop", {"uuid": run["uuid"]}, timeout=10)
        except (DeviceError, TimeoutError):  # offline: close it here, ignore its late reports
            self._finish(run, "stopped", "Stopped by operator (the phone was not reachable).")
        return True

    # ---- phone -> dashboard ----------------------------------------------------------------
    async def on_phone_message(self, device_id: str, message: dict) -> None:
        method, params = message.get("method"), message.get("params") or {}
        if method not in ("agent/started", "agent/event", "agent/finished"):
            return
        run_uuid, seq = str(params.get("uuid") or ""), params.get("seq")
        try:
            if method == "agent/started":
                self._phone_started(device_id, params)
                return
            run = self.db.run_by_uuid(run_uuid)
            if run is None:
                log.warning("phone %s reported on unknown run %s; dropped", device_id, run_uuid)
            elif method == "agent/event":
                self._phone_event(run, params)
            else:
                self._phone_finished(run, params)
        finally:  # always ack, so a report the dashboard cannot use never clogs the phone's outbox
            with contextlib.suppress(Exception):
                await self.devices.get(device_id).notify("agent/ack", {"uuid": run_uuid, "seq": seq})

    def _phone_started(self, device_id: str, params: dict) -> None:
        run_uuid = str(params.get("uuid") or "")
        if not run_uuid or self.db.run_by_uuid(run_uuid):
            return  # resent after a reconnect
        phone = self.db.phone_by_serial(app_serial(device_id))
        if not phone:
            return
        instruction = str(params.get("instruction") or "").strip()[:4000] or "(no instruction)"
        spec = {"phone_id": phone["id"], "serial": phone["serial"], "phone_name": phone["name"],
                "instruction": instruction, "uuid": run_uuid}
        task_id = self.db.create_task(instruction, bool(params.get("reasoning", True)),
                                      int(params.get("max_steps") or 30), [spec], origin="app")
        run = self.db.task(task_id)["runs"][0]
        self.phones.busy[phone["serial"]] = run["id"]
        self.db.update_run(run["id"], status="running", started_at=str(params.get("ts") or now()))
        self.broker.publish("task", self.db.task(task_id))
        self.phones.publish()
        self._watch(run["id"], self._deadline_s(self.db.settings()))

    def _phone_event(self, run: dict, params: dict) -> None:
        if run["status"] not in ACTIVE:
            return  # the run already ended here (stopped while offline): late reports are ignored
        event = self.db.add_event(run["id"], str(params.get("kind") or "status")[:20],
                                  str(params.get("text") or "")[:4000], seq=params.get("seq"), ts=params.get("ts"))
        if event is None:
            return  # already stored
        if params.get("steps") is not None:
            self.db.update_run(run["id"], steps=int(params["steps"]))
        self.broker.publish("run_event", {**event, "task_id": run["task_id"], "steps": params.get("steps")})

    def _phone_finished(self, run: dict, params: dict) -> None:
        if run["status"] not in ACTIVE:
            return
        status = params.get("status") if params.get("status") in FINAL else "failed"
        if params.get("shot"):
            with contextlib.suppress(ValueError, binascii.Error):
                (self.shots_dir / f"{run['id']}.jpg").write_bytes(base64.b64decode(params["shot"]))
        extra = {"steps": int(params["steps"])} if params.get("steps") is not None else {}
        self._finish(run, status, str(params.get("result") or "")[:4000], **extra)

    # ---- lifecycle ----------------------------------------------------------------------
    def resume(self) -> None:
        """After a dashboard restart: runs keep going on the phones, so watch them again."""
        settings = self.db.settings()
        for run in self.db.active_runs():
            self.phones.busy[run["serial"]] = run["id"]
            self._watch(run["id"], self._deadline_s(settings))

    def close(self) -> None:
        for dog in self._watchdogs.values():
            dog.cancel()
        self._watchdogs.clear()

    # ---- helpers -----------------------------------------------------------------------
    def _deadline_s(self, settings: dict[str, str]) -> float:
        return max(1, int(settings.get("timeout_minutes") or 15)) * 60 + SILENCE_GRACE_S

    def _watch(self, run_id: int, seconds: float) -> None:
        async def dog() -> None:
            await asyncio.sleep(seconds)
            self._watchdogs.pop(run_id, None)
            run = self.db.run(run_id)
            if run and run["status"] in ACTIVE:
                self._finish(run, "failed", "The phone stopped reporting (no result within the time limit).")

        old = self._watchdogs.pop(run_id, None)
        if old:
            old.cancel()
        self._watchdogs[run_id] = asyncio.get_running_loop().create_task(dog())

    def _finish(self, run: dict, status: str, result: str, **fields) -> None:
        dog = self._watchdogs.pop(run["id"], None)
        if dog and dog is not asyncio.current_task():
            dog.cancel()
        self._update(run, status=status, ended_at=now(), result=result, **fields)
        self._event(run, "done" if status == "succeeded" else "fail", result or status)
        self.phones.busy.pop(run["serial"], None)
        self.phones.publish()
        if self.on_finish:
            self._spawn(self.on_finish(self.db.run(run["id"]) or run))

    def _update(self, run: dict, **fields) -> None:
        self.db.update_run(run["id"], **fields)
        self.broker.publish("run", {**(self.db.run(run["id"]) or {}), "task_id": run["task_id"]})

    def _event(self, run: dict, kind: str, text: str) -> None:
        event = self.db.add_event(run["id"], kind, text)
        if event:
            self.broker.publish("run_event", {**event, "task_id": run["task_id"], "steps": None})

    def _spawn(self, coro) -> None:
        job = asyncio.get_running_loop().create_task(coro)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
