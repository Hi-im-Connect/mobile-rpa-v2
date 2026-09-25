import json

from mobile_rpa.agent_prompts import OPENROUTER, TOOL_NAMES, TOOLS, defaults_json, run_payload

TASK = {"reasoning": 1, "max_steps": 12}
RUN = {"uuid": "u1", "instruction": "open settings"}
SETTINGS = {"planner_model": "p/m", "executor_model": "e/m", "timeout_minutes": "15", "vision": "0"}


def test_tools_are_the_ones_the_app_implements():
    assert tuple(t["function"]["name"] for t in TOOLS) == TOOL_NAMES
    for tool in TOOLS:
        params = tool["function"]["parameters"]
        assert tool["type"] == "function" and params["type"] == "object"
        assert set(params["required"]) <= set(params["properties"])
    scroll = next(t for t in TOOLS if t["function"]["name"] == "scroll")["function"]["parameters"]
    assert scroll["properties"]["direction"]["enum"] == ["up", "down", "left", "right"]


def test_run_payload_carries_everything_the_phone_needs():
    payload = run_payload(TASK, RUN, SETTINGS)
    assert payload["uuid"] == "u1" and payload["instruction"] == "open settings"
    assert (payload["reasoning"], payload["max_steps"], payload["time_limit_s"], payload["vision"]) == (True, 12, 900, False)
    assert (payload["planner_model"], payload["executor_model"], payload["base_url"]) == ("p/m", "e/m", OPENROUTER)
    assert payload["prompts"]["tools"] == TOOLS and payload["prompts"]["planner"] and payload["prompts"]["executor"]
    json.dumps(payload)  # goes over the wire


def test_defaults_for_the_app_have_no_task():
    defaults = json.loads(defaults_json())
    assert "uuid" not in defaults and "instruction" not in defaults
    assert defaults["max_steps"] == 30 and defaults["vision"] is True and defaults["prompts"]["tools"] == TOOLS
