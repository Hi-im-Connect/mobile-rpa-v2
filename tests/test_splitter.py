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


async def test_credit_is_only_known_for_openrouter(monkeypatch):
    from mobile_rpa import splitter

    def no_network(*args, **kwargs):
        raise AssertionError("credit_left must not call out for other providers")

    monkeypatch.setattr(splitter.httpx, "AsyncClient", no_network)
    assert await splitter.credit_left({"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "api_key": "g"}) is None
