"""Process configuration (env) and the runtime settings operators edit in the dashboard."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

# gemini-2.5-flash answers in ~1s vs ~9s for qwen3-vl-235b at the same price (measured 2026-09-25)
DEFAULT_MODEL = "google/gemini-2.5-flash"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

# Runtime settings: key -> default. Stored in the DB table `settings`, editable in the UI.
ANTHROPIC_BASE_URL = "https://api.anthropic.com"
# Two wire formats cover every provider: OpenAI chat-completions and Claude messages.
FORMATS = ("openai", "anthropic")

RUNTIME_DEFAULTS: dict[str, str] = {
    "provider": "openai",  # wire format: "openai" or "anthropic" (Claude)
    "api_key": "",
    "management_key": "",  # creates one capped key per phone
    "daily_cap_usd": "2.00",  # per phone, resets daily
    "base_url": DEFAULT_BASE_URL,
    "planner_model": DEFAULT_MODEL,
    "executor_model": DEFAULT_MODEL,
    "max_steps": "30",
    "reasoning": "1",
    "vision": "1",  # agent sees full-res Portal screenshots too (257 KB / 2.3 s on a relayed link)
    "timeout_minutes": "15",
}
SECRET_SETTINGS = {"api_key", "management_key"}


@dataclass(frozen=True)
class Env:
    password: str
    data_dir: Path
    session_secret: bytes
    adb: str
    netbird_peers_url: str = ""  # read-only NetBird peer list (QR pairing across NetBird)
    public_url: str = "https://digimate.fastautomate.com/mobile2"  # where phones reach the dashboard

    @property
    def db_path(self) -> Path:
        return self.data_dir / "mobile-rpa.db"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"


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
        adb=os.environ.get("MRPA_ADB", "adb"),
        netbird_peers_url=os.environ.get("MRPA_NETBIRD_PEERS", ""),
        public_url=os.environ.get("MRPA_PUBLIC_URL", "https://digimate.fastautomate.com/mobile2"),
    )


def llm(settings: dict[str, str]) -> dict[str, str]:
    """The active connection: wire format, key and base URL."""
    fmt = "anthropic" if settings.get("provider") == "anthropic" else "openai"
    base = (settings.get("base_url") or "").strip().rstrip("/")
    if fmt == "anthropic":
        base = base or ANTHROPIC_BASE_URL
        if base.endswith("/v1"):
            base = base[:-3]  # Claude-format servers take the root; the client adds /v1/messages
    else:
        base = base or DEFAULT_BASE_URL
    return {"provider": fmt, "key": settings.get("api_key", ""), "base_url": base}
