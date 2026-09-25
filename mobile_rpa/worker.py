"""One agent run on one phone, in its own process.

Usage: python -m mobile_rpa.worker   (reads a JSON job on stdin)
       python -m mobile_rpa.worker --prepare SERIAL

Writes one event per line to stdout, each prefixed with ``@@MRPA `` so library prints never
get mistaken for events. Running in a subprocess keeps mobilerun's global state (logging,
adb forwards, telemetry) per phone, and Stop is simply killing the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .settings import llm

PREFIX = "@@MRPA "
MAX_TOKENS = 4096  # replies are short; a 32k default makes low-credit accounts fail
ROLES_ON_EXECUTOR = ("executor", "fast_agent", "app_opener", "structured_output")


def emit(kind: str, text: str = "", **extra: Any) -> None:
    sys.stdout.write(PREFIX + json.dumps({"kind": kind, "text": text, **extra}) + "\n")
    sys.stdout.flush()


def _profile(model: str, temperature: float, base_url: str, api_key: str, provider: str = "openai") -> dict:
    if provider == "anthropic":
        return {
            "provider": "Anthropic",
            "model": model,
            "temperature": temperature,
            "api_key_source": "file",
            "auth_mode": "api_key",
            "credential_path": None,
            "kwargs": {"api_key": api_key, "base_url": base_url, "max_tokens": MAX_TOKENS},
        }
    return {
        "provider": "OpenAILike",
        "model": model,
        "temperature": temperature,
        "api_key_source": "file",
        "base_url": base_url,
        "api_base": base_url,
        "provider_family": "openai_like",
        "auth_mode": "api_key",
        "credential_path": None,
        "kwargs": {"api_key": api_key, "is_chat_model": True, "max_tokens": MAX_TOKENS},
    }


def build_config(job: dict) -> dict:
    """mobilerun config dict for one run (planner = manager, executor = everything else)."""
    s = job["settings"]
    conn = llm(s)
    vision = s.get("vision") == "1"
    make = lambda model, temp: _profile(model, temp, conn["base_url"], conn["key"], conn["provider"])  # noqa: E731
    profiles = {"manager": make(s["planner_model"], 0.2)}
    for role in ROLES_ON_EXECUTOR:
        profiles[role] = make(s["executor_model"], 0.0 if role in ("app_opener", "structured_output") else 0.1)
    return {
        "agent": {
            "name": "mobilerun",
            "max_steps": int(job["max_steps"]),
            "reasoning": bool(job["reasoning"]),
            "streaming": False,
            "manager": {"vision": vision},
            "executor": {"vision": vision},
            "fast_agent": {"vision": vision},
        },
        "llm_profiles": profiles,
        "device": {"serial": job["serial"], "auto_setup": True, "platform": "android"},
        "telemetry": {"enabled": False},
        "tracing": {"enabled": False},
        "logging": {"debug": False, "save_trajectory": "none", "rich_text": False},
    }


def _clip(text: str | None, limit: int = 400) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


# What the agent is busy with, in plain words. Shown as the live "Now" line, not in the step log.
PHASES = {
    "ManagerContextEvent": "Looking at the screen and planning",
    "ManagerResponseEvent": "Plan received",
    "ExecutorInputEvent": "Choosing the next action",
    "ExecutorActionResultEvent": "Checking what happened",
    "FastAgentInputEvent": "Looking at the screen and thinking",
    "FastAgentToolCallEvent": "Acting on the phone",
}


def phase(event: Any) -> str | None:
    name = type(event).__name__
    if name == "ExecutorActionEvent":
        return "Doing: " + _clip(getattr(event, "description", "") or "the action", 120)
    return PHASES.get(name)


def translate(event: Any) -> list[tuple[str, str]]:
    """mobilerun workflow event -> dashboard (kind, text) lines. Unknown events -> []."""
    name = type(event).__name__
    get = lambda attr: getattr(event, attr, "") or ""  # noqa: E731
    if name == "ManagerPlanDetailsEvent":
        lines = []
        if get("plan"):
            lines.append(("plan", _clip(get("plan"), 1200)))
        if get("subgoal"):
            lines.append(("step", _clip(get("subgoal"))))
        if get("answer"):
            lines.append(("answer", _clip(get("answer"), 1200)))
        return lines
    if name == "ExecutorActionEvent":
        return [("action", _clip(get("description") or get("thought")))]
    if name == "ExecutorActionResultEvent":
        ok = bool(getattr(event, "success", False))
        text = get("summary") if ok else f"{get('summary')} ({get('error') or 'failed'})"
        return [("ok" if ok else "error", _clip(text))]
    if name == "FastAgentResponseEvent":
        return [("think", _clip(get("thought")))] if get("thought") else []
    if name == "FastAgentOutputEvent":
        out = str(getattr(event, "output", "") or "")
        if not out:
            return []
        bad = "Error" in out or "Exception" in out
        return [("error" if bad else "ok", _clip(out))]
    return []


def friendly_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    if "402" in text and "credit" in text.lower():
        return "Stopped: the AI credit ran out. Add credit (openrouter.ai/settings/credits) and run it again."
    if "401" in text or "invalid api key" in text.lower() or "No auth credentials" in text:
        return "The API key was rejected. Check it in Settings."
    lower = text.lower()
    if "no connected android devices" in lower or "device offline" in lower or (
        "device" in lower and "not found" in lower
    ):
        return "The phone is not reachable over adb. Check that it is online."
    return _clip(text, 2000)


class CompressedScreens:
    """The agent's screenshots come from the phone's low-quality video stream: one keyframe decoded
    to a ~40 KB 432x960 JPEG instead of a full 1080x2400 capture. mobilerun taps by accessibility
    element, not by pixel, and the vision test showed the model reads every word at this size
    (docs/research/2026-09-25-slow-link-streaming.md)."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self.session = None

    async def grab(self) -> bytes:
        from . import scrcpy
        from .adb import Adb

        if self.session is None or self.session.closed.is_set():
            self.session = scrcpy.ScrcpySession(Adb(os.environ.get("MRPA_ADB", "adb")), self.serial, "low")
            await asyncio.wait_for(self.session.start(), 30)
        raw = await self.session.snapshot(8)
        return await asyncio.to_thread(scrcpy.decode_jpeg, raw, self.session.codec, 432)

    def install(self) -> None:
        from mobilerun_core_local.driver.android.adb import AndroidDriver

        original = AndroidDriver.screenshot
        screens = self

        async def screenshot(driver, hide_overlay: bool = True) -> bytes:
            for _ in range(2):  # a stream hiccup gets one fresh session
                try:
                    return await screens.grab()
                except Exception:
                    await screens.close()
                    screens.session = None
            # last resort: a normal capture, but shrunk the same way before the model sees it
            return await asyncio.to_thread(shrink_jpeg, await original(driver, hide_overlay))

        AndroidDriver.screenshot = screenshot

    async def close(self) -> None:
        if self.session is not None:
            await self.session.stop()


def shrink_jpeg(image: bytes, width: int = 432) -> bytes:
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(image)).convert("RGB")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=72)
    return out.getvalue()


async def run_job(job: dict) -> int:
    from mobilerun import MobileAgent
    from mobilerun.config_manager import MobileConfig

    relay = job.get("relay")
    driver = None
    screens = None
    if relay:  # phone connected through the FastAutomate app: no adb, the dashboard relays
        from mobilerun_core_local.driver.android.http import AndroidPortalHttpDriver

        driver = AndroidPortalHttpDriver(relay["url"], relay["token"], timeout=60)
    else:
        use_fa_portal()
        screens = CompressedScreens(job["serial"])
        screens.install()
    config = MobileConfig.from_dict(build_config(job))
    emit("phase", "Connecting to the phone")
    agent = MobileAgent(
        goal=job["instruction"], config=config, timeout=int(job.get("timeout", 900)), driver=driver
    )
    steps = 0
    last_phase = ""
    try:
        handler = agent.run()
        async for event in handler.stream_events():
            now = phase(event)
            if now and now != last_phase:
                last_phase = now
                emit("phase", now, steps=steps)
            for kind, text in translate(event):
                if kind == "action":
                    steps += 1
                emit(kind, text, steps=steps)
        result = await handler
        emit("done", _clip(result.reason, 2000), success=bool(result.success), steps=result.steps or steps)
        return 0 if result.success else 1
    except Exception as exc:  # the dashboard must always get a reason
        emit("done", friendly_error(exc), success=False, steps=steps)
        return 2
    finally:
        if screens is not None:
            await screens.close()
            await _release_keyboard(job["serial"])


async def _release_keyboard(serial: str) -> None:
    """Give the phone its normal keyboard back, like `mobilerun run` does on exit."""
    try:
        from async_adbutils import adb
        from mobilerun_core_local.driver.android.portal import PORTAL_PACKAGE_NAME, portal_ime_id

        device = await adb.device(serial=serial)
        await device.shell(f"ime disable {portal_ime_id(PORTAL_PACKAGE_NAME)}")
    except Exception:
        pass


FA_PORTAL_APK = Path(__file__).with_name("fa-portal.apk")
FA_PORTAL_VERSION = "0.7.25"  # the Portal version mobilerun 0.6.19 expects; our build keeps it


def use_fa_portal() -> None:
    """Make mobilerun use our FastAutomate-branded Portal build instead of downloading the
    official one: it reports our version as the expected one, and "setup" installs our APK."""
    if not FA_PORTAL_APK.exists():
        return  # dev checkout without the built APK: keep mobilerun's own behaviour
    from mobilerun_core_local.driver.android import portal

    portal.get_compatible_portal_version = lambda *_a, **_k: (FA_PORTAL_VERSION, "", True)

    async def setup_portal(device, debug: bool = False) -> bool:
        await install_fa_portal(device)
        await portal.enable_portal_accessibility(device)
        return True

    portal.setup_portal = setup_portal


async def install_fa_portal(device) -> bool:
    """Install our APK unless the phone already runs exactly this build. The official Portal is
    signed with another key, so it is uninstalled first. Returns True if it installed."""
    import hashlib

    ours = hashlib.sha256(FA_PORTAL_APK.read_bytes()).hexdigest()
    path = (await device.shell("pm path com.mobilerun.portal")).strip().removeprefix("package:")
    if path:
        theirs = (await device.shell(f"sha256sum {path}")).split(" ")[0].strip()
        if theirs == ours:
            return False

    async def adb_install() -> bytes:
        proc = await asyncio.create_subprocess_exec(
            os.environ.get("MRPA_ADB", "adb"), "-s", device.serial, "install", "-r", "-g", str(FA_PORTAL_APK),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return out

    out = await adb_install()  # our own update: installs on top, settings kept
    if b"Success" not in out and b"UPDATE_INCOMPATIBLE" in out:
        await device.shell("pm uninstall com.mobilerun.portal")  # the official build: other signing key
        out = await adb_install()
    if b"Success" not in out:
        raise RuntimeError("FastAutomate app install failed: " + out.decode(errors="replace")[-300:])
    # file upload/download needs "All files access" (Android 11+); grant it instead of asking
    await device.shell("appops set com.mobilerun.portal MANAGE_EXTERNAL_STORAGE allow")
    return True


async def prepare(serial: str) -> int:
    """Install / update / enable the FastAutomate Portal on a freshly added phone."""
    from async_adbutils import adb
    from mobilerun_core_local.driver.android.portal import enable_portal_accessibility, ensure_portal_ready

    use_fa_portal()
    try:
        device = await adb.device(serial=serial)
        if FA_PORTAL_APK.exists() and await install_fa_portal(device):
            await enable_portal_accessibility(device)
        await ensure_portal_ready(device)
        emit("done", "FastAutomate app ready", success=True)
        return 0
    except Exception as exc:
        emit("done", f"{type(exc).__name__}: {exc}", success=False)
        return 1


def main() -> int:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    os.environ.setdefault("MOBILERUN_TELEMETRY", "false")
    if len(sys.argv) == 3 and sys.argv[1] == "--prepare":
        return asyncio.run(prepare(sys.argv[2]))
    # Import the heavy agent stack BEFORE waiting for a job: the runner keeps a warm spare
    # process blocked here, so a new task skips the multi-second import.
    import mobilerun  # noqa: F401
    from mobilerun.config_manager import MobileConfig  # noqa: F401

    raw = sys.stdin.read()
    if not raw.strip():
        return 0  # a spare that was never used
    return asyncio.run(run_job(json.loads(raw)))


if __name__ == "__main__":
    sys.exit(main())
