"""Turn one operator prompt into one concrete instruction per selected phone."""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from .settings import llm

SYSTEM = """You assign work to Android phones that are each driven by an AI agent.
The operator wrote ONE task for N phones. Write exactly N instructions, one per phone, in order.

Rules:
- If the task asks the phones to differ (different apps, accounts, searches, items, ...),
  give every phone a distinct, concrete choice and name it explicitly
  (for example the exact Play Store app name).
- If the task does not ask for variation, give every phone the same instruction.
- Each instruction must stand alone: the agent on a phone never sees the others.
- Keep the operator's language, facts and constraints. Do not add steps they did not ask for.
- Reply with JSON only: {"instructions": ["...", "..."]}"""


class SplitError(RuntimeError):
    pass


def parse_instructions(content: str, count: int) -> list[str]:
    """Pull the instruction list out of a model reply (tolerates code fences and chatter)."""
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        raise SplitError("The model did not return JSON.")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise SplitError(f"The model returned broken JSON: {exc}") from exc
    items = data.get("instructions") if isinstance(data, dict) else None
    if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
        raise SplitError("The model's JSON has no instruction list.")
    items = [i.strip() for i in items]
    if len(items) < count:
        raise SplitError(f"The model wrote {len(items)} instructions for {count} phones.")
    return items[:count]


async def chat(settings: dict[str, str], system: str, user: str, max_tokens: int, timeout: float = 60) -> str:
    """One reply from the planner model of the active provider (OpenAI-style or Claude)."""
    conn = llm(settings)
    if not conn["key"]:
        raise SplitError("Add an API key in Settings first.")
    if conn["provider"] == "anthropic":
        url = conn["base_url"] + "/v1/messages"
        headers = {"x-api-key": conn["key"], "anthropic-version": "2023-06-01"}
        body = {"model": settings["planner_model"], "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": user}]}
        if system:
            body["system"] = system
    else:
        url = conn["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {conn['key']}"}
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
        body = {"model": settings["planner_model"], "max_tokens": max_tokens, "messages": messages}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise SplitError(f"Could not reach the model provider: {type(exc).__name__} {exc}".strip()) from exc
    if resp.status_code != 200:
        raise SplitError(f"Model provider said {resp.status_code}: {_error_text(resp)}")
    try:
        data = resp.json()
        if conn["provider"] == "anthropic":
            return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text").strip()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise SplitError("Unexpected reply from the model provider.") from exc


async def split(prompt: str, phone_names: list[str], settings: dict[str, str]) -> list[str]:
    count = len(phone_names)
    if count == 1:
        return [prompt.strip()]
    user = f"Task:\n{prompt.strip()}\n\nPhones ({count}): " + ", ".join(
        f"{i + 1}. {name}" for i, name in enumerate(phone_names)
    )
    return parse_instructions(await chat(settings, SYSTEM, user, 2000), count)


async def test_connection(settings: dict[str, str]) -> str:
    """One tiny call with the planner model; returns the model's reply or raises SplitError."""
    return await chat(settings, "", "Reply with the word OK.", 5, timeout=30)


async def credit_left(settings: dict[str, str]) -> float | None:
    """Remaining OpenRouter balance in USD, or None for other providers / on any error."""
    base = settings.get("base_url", "")
    if llm(settings)["provider"] != "openai" or "openrouter.ai" not in base or not settings.get("api_key"):
        return None
    for attempt in range(3):  # a blip (DNS, TLS) must not hide the balance
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    base.rstrip("/") + "/credits",
                    headers={"Authorization": f"Bearer {settings['api_key']}"},
                )
            data = resp.json()["data"]
            return round(float(data["total_credits"]) - float(data["total_usage"]), 4)
        except httpx.HTTPError:
            await asyncio.sleep(1 + attempt)
        except (KeyError, ValueError, TypeError):
            return None
    return None


def _error_text(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        return str(data.get("error", {}).get("message") or data)[:300]
    except ValueError:
        return resp.text[:300]
