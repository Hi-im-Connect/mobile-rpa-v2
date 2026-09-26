"""Process configuration (env) and the runtime settings operators edit in the dashboard (v2)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

# gemini-2.5-flash answers in ~1s vs ~9s for qwen3-vl-235b at the same price (measured 2026-09-25)
DEFAULT_MODEL = "google/gemini-2.5-flash"
OPENROUTER = "https://openrouter.ai/api/v1"

# Runtime settings: key -> default. Stored in the DB table `settings`, editable in the UI.
RUNTIME_DEFAULTS: dict[str, str] = {
    "base_url": OPENROUTER,  # any OpenAI-compatible provider; OpenRouter gives per-phone capped keys
    "api_key": "",  # the dashboard's own key: splitting a task across phones, account credit
    "management_key": "",  # creates one capped key per phone (openrouter.ai/settings/management-keys)
    "daily_cap_usd": "2.00",  # per phone, resets daily
    "planner_model": DEFAULT_MODEL,
    "executor_model": DEFAULT_MODEL,
    "max_steps": "30",
    "reasoning": "1",
    "vision": "1",
    "timeout_minutes": "15",
}
SECRET_SETTINGS = {"api_key", "management_key"}


@dataclass(frozen=True)
class Env:
    password: str
    data_dir: Path
    session_secret: bytes
    public_url: str = "https://digimate.fastautomate.com/mobile2"  # where phones reach the dashboard

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mobile-rpa.db"


def _session_secret(data_dir: Path) -> bytes:
    """A per-install secret persisted next to the DB, so sessions survive restarts."""
    path = data_dir / "session.key"
    if path.exists():
        return path.read_bytes()
    key = secrets.token_bytes(32)
    path.write_bytes(key)
    path.chmod(0o600)
    return key


def load_env() -> Env:
    data_dir = Path(os.environ.get("MRPA_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Env(
        password=os.environ.get("MRPA_PASSWORD", "connect"),
        data_dir=data_dir,
        session_secret=_session_secret(data_dir),
        public_url=os.environ.get("MRPA_PUBLIC_URL", "https://digimate.fastautomate.com/mobile2"),
    )


def llm(settings: dict[str, str]) -> dict[str, str]:
    """The AI connection (OpenAI format): OpenRouter by default, or e.g. Gemini's OpenAI endpoint."""
    base = (settings.get("base_url") or OPENROUTER).strip().rstrip("/")
    return {"provider": "openai", "key": settings.get("api_key", ""), "base_url": base}


def is_openrouter(settings: dict[str, str]) -> bool:
    return "openrouter.ai" in llm(settings)["base_url"]
