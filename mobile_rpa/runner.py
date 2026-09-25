"""Starts one worker process per run, streams its events to the DB and the dashboard."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import signal
import sys

from .db import Db, now
from .devices import device_id_of
from .events import Broker
from .phones import PhoneRegistry
from .worker import PREFIX

log = logging.getLogger("mobile_rpa.runner")

LINE_LIMIT = 8 * 1024 * 1024  # the agent prints whole LLM replies to stdout
SPARES = 2  # warm worker processes kept ready (agent stack already imported)


class Runner:
    def __init__(self, db: Db, broker: Broker, phones: PhoneRegistry) -> None:
        self.db, self.broker, self.phones = db, broker, phones
        self.procs: dict[int, asyncio.subprocess.Process] = {}
        self.stopping: set[int] = set()
        self.tasks: set[asyncio.Task] = set()
        self.worker_cmd = [sys.executable, "-m", "mobile_rpa.worker"]
        self.spares: list[asyncio.subprocess.Process] = []
        self.on_finish = None  # optional callback after every run (e.g. refresh the credit)
        self.relay: tuple[str, str] | None = None  # (base url, key) for app-connected phones

    # ---- public --------------------------------------------------------------------------
    def start_task(self, task_id: int) -> None:
        task = self.db.task(task_id)
        if not task:
            return
        for run in task["runs"]:
            if run["status"] == "queued":
                self.phones.busy[run["serial"]] = run["id"]
                job = asyncio.create_task(self._run(task, run))
                self.tasks.add(job)
                job.add_done_callback(self.tasks.discard)
        self.phones.publish()

    async def stop_run(self, run_id: int) -> bool:
        proc = self.procs.get(run_id)
        if not proc:
            return False
        self.stopping.add(run_id)
        _kill(proc)
        return True

    async def _spawn(self) -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_exec(
            *self.worker_cmd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=LINE_LIMIT, start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

    async def fill_spares(self) -> None:
        self.spares = [p for p in self.spares if p.returncode is None]
        while len(self.spares) < SPARES:
            self.spares.append(await self._spawn())

    async def _take_worker(self) -> asyncio.subprocess.Process:
        while self.spares:
            proc = self.spares.pop(0)
            if proc.returncode is None:
                refill = asyncio.create_task(self.fill_spares())
                self.tasks.add(refill)
                refill.add_done_callback(self.tasks.discard)
                return proc
        return await self._spawn()

    async def shutdown(self) -> None:
        for proc in self.spares:
            _kill(proc)
        self.spares = []
        for run_id in list(self.procs):
            await self.stop_run(run_id)
        if self.tasks:
            await asyncio.wait(self.tasks, timeout=10)

    # ---- one run -------------------------------------------------------------------------
    async def _run(self, task: dict, run: dict) -> None:
        run_id = run["id"]
        settings = self.db.settings()
        timeout = max(1, int(settings.get("timeout_minutes") or 15)) * 60
        job = {
            "serial": run["serial"],
            "instruction": run["instruction"],
            "settings": settings,
            "reasoning": bool(task["reasoning"]),
            "max_steps": task["max_steps"],
            "timeout": timeout,
        }
        device_id = device_id_of(run["serial"])
        if device_id is not None and self.relay:
            # app-connected phone: the agent drives it through the dashboard's relay, no adb
            job["relay"] = {"url": f"{self.relay[0]}/v1/relay/{device_id}", "token": self.relay[1]}
        self._update(run_id, task["id"], status="running", started_at=now())
        self._event(run_id, task["id"], "phase", "Waking up the agent")
        outcome: dict = {}
        stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        try:
            proc = await self._take_worker()
            self.procs[run_id] = proc
            assert proc.stdin and proc.stdout and proc.stderr
            proc.stdin.write(json.dumps(job).encode())
            await proc.stdin.drain()
            proc.stdin.close()
            reader = asyncio.gather(
                self._read_events(proc.stdout, run_id, task["id"], outcome),
                _collect(proc.stderr, stderr_tail),
            )
            try:
                await asyncio.wait_for(reader, timeout)
            except TimeoutError:
                _kill(proc)
                outcome.setdefault("timeout", True)
            await proc.wait()
        except Exception as exc:  # never leave a run stuck in "running"
            log.exception("run %s crashed", run_id)
            outcome.setdefault("error", f"{type(exc).__name__}: {exc}")
        finally:
            self.procs.pop(run_id, None)
        if (run_id in self.stopping or outcome.get("timeout")) and device_id is None:
            # a killed agent leaves its own keyboard active; give the phone its default back
            with contextlib.suppress(Exception):
                await self.phones.adb.shell(run["serial"], "ime reset", timeout=10)
        self.phones.busy.pop(run["serial"], None)
        self._finish(run_id, task["id"], outcome, list(stderr_tail))
        self.phones.publish()
        if self.on_finish:
            with contextlib.suppress(Exception):
                await self.on_finish()

    async def _read_events(
        self, stream: asyncio.StreamReader, run_id: int, task_id: int, outcome: dict
    ) -> None:
        while True:
            try:
                raw = await stream.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue  # an oversized library print; events are always short lines
            if not raw:
                return
            line = raw.decode(errors="replace").strip()
            if not line.startswith(PREFIX):
                continue
            try:
                event = json.loads(line[len(PREFIX):])
            except json.JSONDecodeError:
                continue
            kind, text = event.get("kind", "status"), event.get("text", "")
            if "steps" in event:
                self.db.update_run(run_id, steps=int(event["steps"] or 0))
            if kind == "done":
                outcome.update(event)
                continue
            self._event(run_id, task_id, kind, text, steps=event.get("steps"))

    def _finish(self, run_id: int, task_id: int, outcome: dict, stderr: list[str]) -> None:
        if run_id in self.stopping:
            self.stopping.discard(run_id)
            status, result = "stopped", "Stopped by operator."
        elif outcome.get("timeout"):
            status, result = "failed", "Timed out (raise the limit in Settings)."
        elif "success" in outcome:
            status = "succeeded" if outcome["success"] else "failed"
            result = outcome.get("text") or ""
        else:
            status = "failed"
            result = outcome.get("error") or _last_error(stderr) or "The agent exited without a result."
        fields = {"status": status, "ended_at": now(), "result": result}
        if outcome.get("steps") is not None:
            fields["steps"] = int(outcome["steps"])
        self._update(run_id, task_id, **fields)
        self._event(run_id, task_id, "done" if status == "succeeded" else "fail", result)

    # ---- helpers -------------------------------------------------------------------------
    def _update(self, run_id: int, task_id: int, **fields) -> None:
        self.db.update_run(run_id, **fields)
        self.broker.publish("run", {**(self.db.run(run_id) or {}), "task_id": task_id})

    def _event(self, run_id: int, task_id: int, kind: str, text: str, steps: int | None = None) -> None:
        event = self.db.add_event(run_id, kind, text)
        self.broker.publish("run_event", {**event, "task_id": task_id, "steps": steps})


async def _collect(stream: asyncio.StreamReader, tail: collections.deque[str]) -> None:
    while True:
        try:
            raw = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError):
            continue
        if not raw:
            return
        line = raw.decode(errors="replace").rstrip()
        if line:
            tail.append(line)


def _last_error(lines: list[str]) -> str:
    for line in reversed(lines):
        if "Error" in line or "Exception" in line:
            return line[:500]
    return lines[-1][:500] if lines else ""


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)
    asyncio.get_running_loop().call_later(3, _hard_kill, proc)


def _hard_kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
