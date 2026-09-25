from mobile_rpa import worker


def _event(name, **fields):
    return type(name, (), fields)()


def test_config_puts_planner_on_manager_and_executor_elsewhere():
    job = {"serial": "1.2.3.4:5555", "max_steps": 12, "reasoning": True, "settings": {
        "planner_model": "p", "executor_model": "e", "base_url": "http://x/v1", "api_key": "k", "vision": "0"}}
    cfg = worker.build_config(job)
    profiles = cfg["llm_profiles"]
    assert profiles["manager"]["model"] == "p"
    assert {profiles[r]["model"] for r in worker.ROLES_ON_EXECUTOR} == {"e"}
    assert profiles["executor"]["kwargs"]["max_tokens"] == worker.MAX_TOKENS
    assert cfg["device"]["serial"] == "1.2.3.4:5555" and cfg["agent"]["max_steps"] == 12
    assert cfg["telemetry"]["enabled"] is False


def test_translate_manager_and_executor_events():
    plan = _event("ManagerPlanDetailsEvent", plan="1. a\n2. b", subgoal="a", thought="", answer="")
    assert worker.translate(plan) == [("plan", "1. a\n2. b"), ("step", "a")]
    act = _event("ExecutorActionEvent", description="Tap OK", thought="")
    assert worker.translate(act) == [("action", "Tap OK")]
    bad = _event("ExecutorActionResultEvent", success=False, summary="Tap", error="gone")
    assert worker.translate(bad) == [("error", "Tap (gone)")]
    assert worker.translate(_event("ScreenshotEvent")) == []


def test_friendly_errors():
    assert "credit ran out" in worker.friendly_error(RuntimeError("Error code: 402 - more credits"))
    assert "API key" in worker.friendly_error(RuntimeError("Error code: 401 invalid api key"))
    assert "not reachable" in worker.friendly_error(ValueError("No connected Android devices found."))


def test_phase_lines_are_plain_words():
    assert worker.phase(_event("ManagerContextEvent")) == "Looking at the screen and planning"
    assert worker.phase(_event("ExecutorActionEvent", description="Tap Send")) == "Doing: Tap Send"
    assert worker.phase(_event("ScreenshotEvent")) is None


def test_claude_provider_builds_native_anthropic_profiles():
    job = {"serial": "s", "max_steps": 5, "reasoning": True, "settings": {
        "provider": "anthropic", "api_key": "sk-ant-api03-x", "base_url": "https://api.deepseek.com/anthropic/v1",
        "planner_model": "claude-sonnet-5", "executor_model": "claude-haiku-4-5", "vision": "0"}}
    profiles = worker.build_config(job)["llm_profiles"]
    assert profiles["manager"]["provider"] == "Anthropic" and profiles["manager"]["model"] == "claude-sonnet-5"
    assert profiles["executor"]["model"] == "claude-haiku-4-5"
    assert profiles["executor"]["kwargs"]["api_key"] == "sk-ant-api03-x"
    assert profiles["executor"]["kwargs"]["base_url"] == "https://api.deepseek.com/anthropic"


def test_claude_profile_loads_in_mobilerun():
    from mobilerun.config_manager import MobileConfig

    job = {"serial": "s", "max_steps": 5, "reasoning": True, "settings": {
        "provider": "anthropic", "api_key": "sk-ant-api03-x", "base_url": "https://api.deepseek.com/anthropic/v1",
        "planner_model": "claude-sonnet-5", "executor_model": "claude-haiku-4-5", "vision": "0"}}
    config = MobileConfig.from_dict(worker.build_config(job))
    assert config.llm_profiles["manager"].provider == "Anthropic"


def test_shrink_jpeg_makes_a_small_432px_image():
    import io

    from PIL import Image

    big = io.BytesIO()
    Image.new("RGB", (1080, 2400), (40, 120, 60)).save(big, "PNG")
    small = worker.shrink_jpeg(big.getvalue())
    img = Image.open(io.BytesIO(small))
    assert img.format == "JPEG" and img.size == (432, 960)
