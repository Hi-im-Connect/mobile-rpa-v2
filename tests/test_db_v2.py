import sqlite3

from mobile_rpa.db import Db

RUN = {"phone_id": 1, "serial": "app:x", "phone_name": "P", "instruction": "open settings"}


def test_runs_get_a_uuid_and_events_are_unique_per_seq(tmp_path):
    db = Db(tmp_path / "t.db")
    run = db.task(db.create_task("open settings", True, 30, [RUN]))["runs"][0]
    assert len(run["uuid"]) == 32 and run["origin"] == "dashboard"
    assert db.run_by_uuid(run["uuid"])["id"] == run["id"]
    assert db.add_event(run["id"], "action", "Tap Settings", seq=1)["text"] == "Tap Settings"
    assert db.add_event(run["id"], "action", "Tap Settings", seq=1) is None  # resent after a reconnect
    assert db.add_event(run["id"], "phase", "local note") is not None  # dashboard notes have no seq
    assert [e["text"] for e in db.events(run["id"])] == ["Tap Settings", "local note"]


def test_app_started_runs_keep_the_phone_uuid(tmp_path):
    db = Db(tmp_path / "t.db")
    run = db.task(db.create_task("x", False, 10, [{**RUN, "uuid": "u-1"}], origin="app"))["runs"][0]
    assert (run["uuid"], run["origin"]) == ("u-1", "app")


def test_a_phase1_database_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE phones (id INTEGER PRIMARY KEY, serial TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',"
        " model TEXT NOT NULL DEFAULT '', android TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL, last_seen TEXT);"
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, phone_id INTEGER NOT NULL,"
        " serial TEXT NOT NULL, phone_name TEXT NOT NULL, instruction TEXT NOT NULL, status TEXT NOT NULL,"
        " started_at TEXT, ended_at TEXT, steps INTEGER NOT NULL DEFAULT 0, result TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE run_events (id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, ts TEXT NOT NULL,"
        " kind TEXT NOT NULL, text TEXT NOT NULL);"
    )
    conn.close()
    db = Db(path)
    phone = db.add_phone("app:z", "Z", "Z", "14")
    db.update_phone(phone["id"], key_hash="h", paused=1)
    assert (db.phone(phone["id"])["key_hash"], db.phone(phone["id"])["paused"]) == ("h", 1)
    run = db.task(db.create_task("x", True, 5, [RUN]))["runs"][0]
    assert db.add_event(run["id"], "ok", "done", seq=3) and db.add_event(run["id"], "ok", "done", seq=3) is None
