import pytest

from mobile_rpa.splitter import SplitError, parse_instructions, split


def test_parses_fenced_json():
    reply = 'Sure!\n```json\n{"instructions": ["a", "b", "c"]}\n```'
    assert parse_instructions(reply, 3) == ["a", "b", "c"]


def test_extra_items_are_trimmed():
    assert parse_instructions('{"instructions": ["a", "b", "c"]}', 2) == ["a", "b"]


@pytest.mark.parametrize(
    "reply",
    ["no json here", '{"instructions": "a"}', '{"instructions": ["a"]}', '{"instructions": ["a", ""]}', "{broken"],
)
def test_bad_replies_raise(reply):
    with pytest.raises(SplitError):
        parse_instructions(reply, 2)


async def test_single_phone_needs_no_model_call():
    assert await split("  do it  ", ["Only"], {}) == ["do it"]


async def test_multi_phone_needs_a_key():
    with pytest.raises(SplitError, match="API key"):
        await split("x", ["a", "b"], {"api_key": ""})


async def test_claude_chat_uses_messages_api(monkeypatch):
    import httpx

    from mobile_rpa import splitter

    seen = {}

    async def fake_post(self, url, json=None, headers=None):
        seen.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"instructions": ["a", "b"]}'}]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    settings = {"provider": "anthropic", "api_key": "sk-ant-api03-k", "base_url": "", "planner_model": "claude-sonnet-5"}
    assert await splitter.split("x", ["p1", "p2"], settings) == ["a", "b"]
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-ant-api03-k" and "system" in seen["json"]
