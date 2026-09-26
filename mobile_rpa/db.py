"""SQLite storage: phones, tasks, runs, run events and runtime settings."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .settings import RUNTIME_DEFAULTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS phones (
  id INTEGER PRIMARY KEY,
  serial TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  android TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL,
  last_seen TEXT,
  key_hash TEXT NOT NULL DEFAULT '',
  paused INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  prompt TEXT NOT NULL,
  created_at TEXT NOT NULL,
  reasoning INTEGER NOT NULL,
  max_steps INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id),
  phone_id INTEGER NOT NULL,
  serial TEXT NOT NULL,
  phone_name TEXT NOT NULL,
  instruction TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  steps INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL DEFAULT '',
  uuid TEXT,
  origin TEXT NOT NULL DEFAULT 'dashboard'
);
CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id);
CREATE TABLE IF NOT EXISTS run_events (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES runs(id),
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  seq INTEGER
);
CREATE INDEX IF NOT EXISTS run_events_run ON run_events(run_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_tokens (
  token TEXT PRIMARY KEY,
  device_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT
);
"""

# columns added after the first v2 release (phase 1 used the v1 schema)
MIGRATIONS = (
    ("phones", "key_hash", "TEXT NOT NULL DEFAULT ''"),
    ("phones", "paused", "INTEGER NOT NULL DEFAULT 0"),
    ("runs", "uuid", "TEXT"),
    ("runs", "origin", "TEXT NOT NULL DEFAULT 'dashboard'"),
    ("run_events", "seq", "INTEGER"),
)
INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS runs_uuid ON runs(uuid);
CREATE UNIQUE INDEX IF NOT EXISTS run_events_seq ON run_events(run_id, seq);
"""

ACTIVE_STATUSES = ("queued", "running", "paused")


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Db:
    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            for table, column, definition in MIGRATIONS:
                cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            self._conn.executescript(INDEXES)

    # ---- low level -----------------------------------------------------------------------
    def _all(self, sql: str, *args: Any) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, *args: Any) -> dict | None:
        rows = self._all(sql, *args)
        return rows[0] if rows else None

    def _run(self, sql: str, *args: Any) -> int:
        with self._lock:
            return self._conn.execute(sql, args).lastrowid or 0

    # ---- settings ------------------------------------------------------------------------
    def settings(self) -> dict[str, str]:
        stored = {r["key"]: r["value"] for r in self._all("SELECT key, value FROM settings")}
        # only known keys: a retired setting (e.g. an old secret) must never leak into responses
        return {**RUNTIME_DEFAULTS, **{k: v for k, v in stored.items() if k in RUNTIME_DEFAULTS}}

    def set_settings(self, values: dict[str, str]) -> None:
        for key, value in values.items():
            self._run(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                key,
                value,
            )

    # ---- phones --------------------------------------------------------------------------
    def phones(self) -> list[dict]:
        return self._all("SELECT * FROM phones ORDER BY id")

    def phone(self, phone_id: int) -> dict | None:
        return self._one("SELECT * FROM phones WHERE id = ?", phone_id)

    def phone_by_serial(self, serial: str) -> dict | None:
        return self._one("SELECT * FROM phones WHERE serial = ?", serial)

    def add_phone(self, serial: str, name: str, model: str, android: str) -> dict:
        existing = self.phone_by_serial(serial)
        if existing:
            return existing
        self._run(
            "INSERT INTO phones(serial, name, model, android, added_at) VALUES(?, ?, ?, ?, ?)",
            serial,
            name,
            model,
            android,
            now(),
        )
        return self.phone_by_serial(serial) or {}

    def update_phone(self, phone_id: int, **fields: Any) -> None:
        allowed = {"name", "model", "android", "last_seen", "serial", "key_hash", "paused"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        cols = ", ".join(f"{k} = ?" for k in sets)
        self._run(f"UPDATE phones SET {cols} WHERE id = ?", *sets.values(), phone_id)

    def delete_phone(self, phone_id: int) -> None:
        self._run("DELETE FROM phones WHERE id = ?", phone_id)

    # ---- tasks and runs ------------------------------------------------------------------
    def create_task(
        self, prompt: str, reasoning: bool, max_steps: int, runs: list[dict], origin: str = "dashboard"
    ) -> int:
        task_id = self._run(
            "INSERT INTO tasks(prompt, created_at, reasoning, max_steps) VALUES(?, ?, ?, ?)",
            prompt, now(), int(reasoning), max_steps,
        )
        for run in runs:
            self._run(
                "INSERT INTO runs(task_id, phone_id, serial, phone_name, instruction, status, uuid, origin) "
                "VALUES(?, ?, ?, ?, ?, 'queued', ?, ?)",
                task_id, run["phone_id"], run["serial"], run["phone_name"], run["instruction"],
                run.get("uuid") or uuid.uuid4().hex, origin,
            )
        return task_id

    def task(self, task_id: int) -> dict | None:
        task = self._one("SELECT * FROM tasks WHERE id = ?", task_id)
        if task:
            task["runs"] = self.runs_for_task(task_id)
        return task

    def tasks(self, limit: int = 50, offset: int = 0) -> list[dict]:
        tasks = self._all(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ? OFFSET ?", limit, offset
        )
        for task in tasks:
            task["runs"] = self.runs_for_task(task["id"])
        return tasks

    def runs_for_task(self, task_id: int) -> list[dict]:
        return self._all("SELECT * FROM runs WHERE task_id = ? ORDER BY id", task_id)

    def run(self, run_id: int) -> dict | None:
        return self._one("SELECT * FROM runs WHERE id = ?", run_id)

    def run_by_uuid(self, run_uuid: str) -> dict | None:
        return self._one("SELECT * FROM runs WHERE uuid = ?", run_uuid)

    def active_runs(self) -> list[dict]:
        return self._all(
            "SELECT * FROM runs WHERE status IN ('queued', 'running', 'paused') ORDER BY id"
        )

    def update_run(self, run_id: int, **fields: Any) -> None:
        allowed = {"status", "started_at", "ended_at", "steps", "result"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        cols = ", ".join(f"{k} = ?" for k in sets)
        self._run(f"UPDATE runs SET {cols} WHERE id = ?", *sets.values(), run_id)

    def add_event(
        self, run_id: int, kind: str, text: str, seq: int | None = None, ts: str | None = None
    ) -> dict | None:
        """None when this (run, seq) is already stored: a phone resending after a reconnect."""
        ts = ts or now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO run_events(run_id, ts, kind, text, seq) VALUES(?, ?, ?, ?, ?)",
                (run_id, ts, kind, text, seq),
            )
            if cur.rowcount == 0:
                return None
            event_id = cur.lastrowid
        return {"id": event_id, "run_id": run_id, "ts": ts, "kind": kind, "text": text, "seq": seq}

    def events(self, run_id: int) -> list[dict]:
        return self._all("SELECT * FROM run_events WHERE run_id = ? ORDER BY id", run_id)

    # ---- app connect tokens (FastAutomate app) --------------------------------------------
    def create_app_token(self, token: str, device_id: str | None = None, expires_at: str | None = None) -> None:
        self._run(
            "INSERT INTO app_tokens(token, device_id, created_at, expires_at) VALUES(?, ?, ?, ?)",
            token, device_id, now(), expires_at,
        )

    def app_token(self, token: str) -> dict | None:
        return self._one("SELECT * FROM app_tokens WHERE token = ?", token)

    def bind_app_token(self, token: str, device_id: str) -> None:
        """An invite token belongs to the first phone that uses it, forever."""
        self._run("UPDATE app_tokens SET device_id = ?, expires_at = NULL WHERE token = ?", device_id, token)

    def revoke_device_tokens(self, device_id: str) -> None:
        self._run("DELETE FROM app_tokens WHERE device_id = ?", device_id)

    def tasks_for_phone(self, phone_id: int, limit: int, offset: int) -> tuple[list[dict], int]:
        ids = [r["task_id"] for r in self._all(
            "SELECT DISTINCT task_id FROM runs WHERE phone_id = ? ORDER BY task_id DESC LIMIT ? OFFSET ?",
            phone_id, limit, offset,
        )]
        total = (self._one("SELECT COUNT(DISTINCT task_id) AS n FROM runs WHERE phone_id = ?", phone_id) or {}).get("n", 0)
        return [t for t in (self.task(i) for i in ids) if t], total

    def interrupt_orphans(self) -> int:
        """Runs still marked active from a previous process can never finish; close them."""
        orphans = self.active_runs()
        for run in orphans:
            self.update_run(
                run["id"], status="interrupted", ended_at=now(), result="The app restarted mid-run."
            )
        return len(orphans)

    def stats_today(self) -> dict[str, int]:
        day = now()[:10]
        row = self._one(
            "SELECT SUM(status = 'succeeded') AS ok, SUM(status IN ('failed','stopped','interrupted')) AS bad "
            "FROM runs WHERE substr(ended_at, 1, 10) = ?",
            day,
        ) or {}
        return {"succeeded": row.get("ok") or 0, "failed": row.get("bad") or 0}
