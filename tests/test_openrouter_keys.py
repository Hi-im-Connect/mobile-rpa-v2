import json

import httpx
import pytest

from mobile_rpa.openrouter_keys import API, KeyApiError, KeyManager


def recording(log: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        log.append((request.method, str(request.url), body, request.headers["authorization"]))
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"hash": "h1", "limit": 2.0}, "key": "sk-or-v1-phone"})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"hash": "h1", "usage_daily": 0.4321}})
        if request.method == "PATCH":
            return httpx.Response(200, json={"data": {"hash": "h1", **body}})
        return httpx.Response(404, json={"error": {"message": "not found"}})
    return httpx.MockTransport(handler)


async def test_create_key_with_a_daily_cap():
    log: list = []
    keys = KeyManager("mgmt", recording(log))
    assert await keys.create("fastautomate-v2-3-POCO F3", 2.0) == ("sk-or-v1-phone", "h1")
    method, url, body, auth = log[0]
    assert (method, url, auth) == ("POST", API, "Bearer mgmt")
    assert body == {"name": "fastautomate-v2-3-POCO F3", "limit": 2.0, "limit_reset": "daily"}


async def test_spend_pause_cap_and_delete():
    log: list = []
    keys = KeyManager("mgmt", recording(log))
    assert await keys.spent_today("h1") == 0.4321
    await keys.set_disabled("h1", True)
    await keys.set_limit("h1", 1.5)
    await keys.delete("h1")  # OpenRouter says 404: already gone, which is fine
    assert [(m, u.rsplit("/", 1)[1], b) for m, u, b, _ in log] == [
        ("GET", "h1", None), ("PATCH", "h1", {"disabled": True}), ("PATCH", "h1", {"limit": 1.5}), ("DELETE", "h1", None),
    ]


async def test_errors_are_readable():
    refuse = httpx.MockTransport(lambda r: httpx.Response(401, json={"error": {"message": "Invalid management key"}}))
    with pytest.raises(KeyApiError, match="401: Invalid management key"):
        await KeyManager("bad", refuse).create("x", 1.0)
    with pytest.raises(KeyApiError, match="management key"):
        KeyManager("")
