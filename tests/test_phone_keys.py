from mobile_rpa.db import Db
from mobile_rpa.devices import DeviceError, app_serial
from mobile_rpa.openrouter_keys import KeyApiError
from mobile_rpa.phone_keys import PhoneKeys


class FakeManager:
    def __init__(self, log):
        self.log, self.n = log, 0

    async def create(self, name, daily_cap):
        self.n += 1
        self.log.append(("create", name, daily_cap))
        return f"sk-or-v1-key{len(self.log)}", f"hash{len(self.log)}"

    async def delete(self, key_hash):
        self.log.append(("delete", key_hash))

    async def set_disabled(self, key_hash, disabled):
        self.log.append(("disabled", key_hash, disabled))

    async def set_limit(self, key_hash, daily_cap):
        self.log.append(("limit", key_hash, daily_cap))

    async def spent_today(self, key_hash):
        return 0.25


class FakeConn:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def call(self, method, params=None, timeout=30):
        self.calls.append((method, params))
        if self.fail:
            raise DeviceError("gone")
        return {"saved": True}


def setup(tmp_path, management_key="mgmt"):
    db = Db(tmp_path / "t.db")
    db.set_settings({"management_key": management_key, "daily_cap_usd": "1.50"})
    phone = db.add_phone(app_serial("d1"), "POCO F3", "POCO F3", "16")
    log: list = []

    def managers(key):
        if not key:
            raise KeyApiError("Add the OpenRouter management key in Settings.")
        return FakeManager(log)

    return db, phone, log, PhoneKeys(db, managers)


async def test_a_new_phone_gets_its_own_capped_key(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    conn = FakeConn()
    assert await keys.ensure(phone["id"], conn, "") is None
    assert log == [("create", f"fastautomate-v2-{phone['id']}-POCO F3", 1.5)]
    method, creds = conn.calls[0]
    assert method == "agent/credentials" and len(conn.calls) == 1
    assert (creds["key"], creds["hash"], creds["base_url"]) == ("sk-or-v1-key1", "hash1", "https://openrouter.ai/api/v1")
    assert db.phone(phone["id"])["key_hash"] == "hash1"


async def test_a_phone_that_still_has_its_key_is_left_alone(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], key_hash="hash-old")
    conn = FakeConn()
    assert await keys.ensure(phone["id"], conn, "hash-old") is None
    assert log == [] and conn.calls == []


async def test_a_reinstalled_app_gets_a_fresh_key_and_the_old_one_is_deleted(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], key_hash="hash-old")
    assert await keys.ensure(phone["id"], FakeConn(), "") is None
    assert log[0] == ("delete", "hash-old") and log[1][0] == "create"


async def test_no_management_key_is_a_readable_problem(tmp_path):
    db, phone, log, keys = setup(tmp_path, management_key="")
    conn = FakeConn()
    assert "management key" in await keys.ensure(phone["id"], conn, "")
    assert conn.calls == []


async def test_a_paused_phone_gets_a_disabled_key(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], paused=1)
    await keys.ensure(phone["id"], FakeConn(), "")
    assert log[-1] == ("disabled", "hash1", True)


async def test_forget_pause_cap_and_spend(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], key_hash="h9")
    phone = db.phone(phone["id"])
    await keys.pause(phone, True)
    assert db.phone(phone["id"])["paused"] == 1
    await keys.apply_cap(3.0)
    assert await keys.spent_today(phone) == 0.25
    await keys.forget(phone)
    assert log == [("disabled", "h9", True), ("limit", "h9", 3.0), ("delete", "h9")]


async def test_phone_that_drops_before_receiving_its_key_reports_it(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    assert "Could not give the phone its AI key" in await keys.ensure(phone["id"], FakeConn(fail=True), "")


GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"


async def test_other_providers_share_one_key(tmp_path):
    db, phone, log, keys = setup(tmp_path, management_key="")
    db.set_settings({"base_url": GEMINI, "api_key": "gemini-key"})
    conn = FakeConn()
    assert await keys.ensure(phone["id"], conn, "") is None
    method, creds = conn.calls[0]
    assert method == "agent/credentials" and creds["key"] == "gemini-key" and creds["base_url"] == GEMINI
    assert creds["hash"].startswith("shared-") and db.phone(phone["id"])["key_hash"] == creds["hash"]
    assert log == []  # no OpenRouter key management involved
    again = FakeConn()
    assert await keys.ensure(phone["id"], again, creds["hash"]) is None
    assert again.calls == []  # the phone already holds it


async def test_shared_keys_are_not_deleted_capped_or_disabled(tmp_path):
    db, phone, log, keys = setup(tmp_path, management_key="")
    db.update_phone(phone["id"], key_hash="shared-abc")
    phone = db.phone(phone["id"])
    await keys.pause(phone, True)
    await keys.apply_cap(5.0)
    await keys.forget(phone)
    assert log == [] and db.phone(phone["id"])["paused"] == 1
    assert await keys.spent_today(phone) is None


async def test_a_new_key_is_issued_even_if_the_old_one_cannot_be_deleted(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], key_hash="hash-old")

    class Refusing(FakeManager):
        async def delete(self, key_hash):
            raise KeyApiError("OpenRouter said 403: not your key")

    keys._managers = lambda key: Refusing(log)
    assert await keys.ensure(phone["id"], FakeConn(), "") is None
    assert db.phone(phone["id"])["key_hash"] != "hash-old"


async def test_a_new_key_that_cannot_be_paused_is_deleted_again(tmp_path):
    db, phone, log, keys = setup(tmp_path)
    db.update_phone(phone["id"], paused=1)

    class NoPause(FakeManager):
        async def set_disabled(self, key_hash, disabled):
            raise KeyApiError("OpenRouter said 500: busy")

    keys._managers = lambda key: NoPause(log)
    assert "500" in await keys.ensure(phone["id"], FakeConn(), "")
    assert ("delete", "hash1") in log and db.phone(phone["id"])["key_hash"] == ""
