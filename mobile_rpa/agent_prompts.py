"""What the agent on each phone is told. Sent with every task (and bundled in the app for its own
runs), so behaviour can change without an app update. Tool names and arguments must match the
app's agent/Actions.kt."""

from __future__ import annotations

import json

from .settings import OPENROUTER, RUNTIME_DEFAULTS, llm

PROMPTS_VERSION = "2026-09-25.1"

PLANNER_SYSTEM = """You plan work for an AI agent that operates an Android phone by tapping, typing and scrolling.
Given the task and the current screen, write the shortest list of concrete goals (at most 6) that completes the task.
Each goal is one visible outcome, for example "Settings app is open" or "Search results for 'weather' are shown".
If the agent got stuck, choose a different route than the one that failed.
Reply with JSON only: {"goals": ["...", "..."]}"""

EXECUTOR_SYSTEM = """You operate an Android phone to complete a task. Each turn you get the task, the plan (when there is
one), your recent actions with their results, and the current screen as a numbered list of elements (sometimes also a
screenshot). Choose exactly ONE action by calling one tool.

Rules:
- Prefer tapping elements by their number. Use tap_at only when what you need is not in the list.
- To type, give the number of the text field so it gets focused first. Use enter to submit a search.
- If an action did not change the screen, try something different; never repeat it more than twice.
- To reach something off screen, scroll. To leave a screen, use back.
- Call done(success=true, answer=...) as soon as the task is complete, and put anything the task asked you to find in answer.
- Call done(success=false, answer=<why>) when the task is impossible, for example a login you have no credentials for.
- Never type passwords, payment details or personal data unless the task gives them to you."""


def _tool(name: str, description: str, properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": list(required)},
        },
    }


_INDEX = {"type": "integer", "description": "Element number from the screen list"}

TOOLS = [
    _tool("tap", "Tap a screen element by its number.", {"index": _INDEX}, ("index",)),
    _tool("tap_at", "Tap a point on the screen, in screen pixels.",
          {"x": {"type": "integer"}, "y": {"type": "integer"}}, ("x", "y")),
    _tool("type", "Type text into a field. Give the field's number to focus it first. Existing text is replaced.",
          {"text": {"type": "string"}, "index": _INDEX}, ("text",)),
    _tool("scroll", "Scroll the screen, or the numbered scrollable element, to reveal more. 'down' shows what is below.",
          {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "index": _INDEX}, ("direction",)),
    _tool("back", "Press the Back button.", {}),
    _tool("home", "Go to the home screen.", {}),
    _tool("enter", "Press Enter / Search on the keyboard.", {}),
    _tool("open_app", "Open an installed app by the name shown under its icon.", {"name": {"type": "string"}}, ("name",)),
    _tool("wait", "Wait for the screen to finish loading.",
          {"seconds": {"type": "number", "description": "1 to 10"}}, ("seconds",)),
    _tool("done", "Finish the task.",
          {"success": {"type": "boolean"}, "answer": {"type": "string", "description": "Result or reason"}},
          ("success", "answer")),
]
TOOL_NAMES = tuple(t["function"]["name"] for t in TOOLS)


def _common(reasoning: bool, max_steps: int, settings: dict[str, str]) -> dict:
    return {
        "reasoning": bool(reasoning),
        "max_steps": int(max_steps),
        "time_limit_s": max(1, int(settings.get("timeout_minutes") or 15)) * 60,
        "vision": settings.get("vision", "1") != "0",
        "planner_model": settings.get("planner_model") or RUNTIME_DEFAULTS["planner_model"],
        "executor_model": settings.get("executor_model") or RUNTIME_DEFAULTS["executor_model"],
        "base_url": llm(settings)["base_url"],
        "prompts": {"version": PROMPTS_VERSION, "planner": PLANNER_SYSTEM, "executor": EXECUTOR_SYSTEM, "tools": TOOLS},
    }


def run_payload(task: dict, run: dict, settings: dict[str, str]) -> dict:
    """The agent/run message for one phone."""
    return {"uuid": run["uuid"], "instruction": run["instruction"],
            **_common(bool(task["reasoning"]), int(task["max_steps"]), settings)}


def defaults_json() -> str:
    """Settings + instructions the app uses for its own runs before the dashboard has sent any."""
    d = RUNTIME_DEFAULTS
    return json.dumps(_common(d["reasoning"] != "0", int(d["max_steps"]), d), indent=1)
