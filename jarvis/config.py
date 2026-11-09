from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import timezone, timedelta


def state_home() -> Path:
    if os.name == "nt":
        # Packaged desktop hosts can virtualize LOCALAPPDATA. Use one stable owner
        # directory for CLI, pythonw and Task Scheduler processes.
        return Path.home() / "AppData" / "Local" / "JARVIS"
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "JARVIS" if base else Path.home() / ".local" / "state" / "jarvis"


@dataclass(frozen=True)
class Config:
    owner_id: int = 0
    telegram_api_id: int = 0
    timezone: str = "Europe/Istanbul"
    quiet_start: int = 23
    quiet_end: int = 9
    max_workers: int = 1
    max_call_seconds: int = 900
    blocker_delay_seconds: int = 60
    hourly_calls: int = 2
    daily_calls: int = 6
    call_cooldown_seconds: int = 60
    calls_enabled: bool = False
    automatic_calls: bool = False
    free_cloud_confirmed: bool = False
    gemini_model: str = "gemini-3.8-live"
    gemini_voice: str = "Charon"
    voice_style: str = "Speak in a refined Southern British English accent (Received Pronunciation), consistently throughout the call. Use a cultured adult male voice with a smooth, warm lower register, crisp diction, measured conversational pacing, quiet confidence and restrained dry wit. Sound intelligent, composed and attentive, like an elegant science-fiction personal assistant. Keep the delivery natural and understated, never theatrical or exaggerated. Do not drift into an American accent or copy the caller's accent."
    browser_channel: str = "msedge"
    codex_bin: str = "codex"
    codex_task_mode: str = "background"
    existing_chat_transport: str = "interface"
    whisper_bin: str = "whisper-cli"
    whisper_model: str = ""
    piper_bin: str = "piper"
    piper_model: str = ""

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        data = tomllib.loads(path.read_text("utf-8")) if path else {}
        allowed = {f.name: f for f in fields(cls)}
        if set(data) - allowed.keys():
            raise ValueError("Unknown configuration keys: " + ", ".join(set(data) - allowed.keys()))
        default = cls()
        for name, value in data.items():
            if type(value) is not type(getattr(default, name)):
                raise ValueError(f"Invalid type for {name}")
        result = cls(**data)
        if result.browser_channel not in {"msedge", "chrome", "chromium"}:
            raise ValueError("browser_channel must be msedge, chrome or chromium")
        if result.codex_task_mode not in {"frontend", "visible", "background"}:
            raise ValueError("codex_task_mode must be frontend, visible or background")
        if result.existing_chat_transport not in {"interface", "app_server"}:
            raise ValueError("existing_chat_transport must be interface or app_server")
        if not result.gemini_voice.isalpha() or len(result.gemini_voice) > 50:
            raise ValueError("Invalid Gemini voice name")
        if not result.voice_style.strip() or len(result.voice_style) > 2000:
            raise ValueError("Voice style must contain 1 to 2000 characters")
        if result.owner_id < 0 or result.telegram_api_id < 0:
            raise ValueError("Telegram IDs must be positive")
        if result.max_workers not in (1, 2):
            raise ValueError("max_workers must be 1 or 2")
        if not (0 <= result.quiet_start < 24 and 0 <= result.quiet_end < 24):
            raise ValueError("Quiet hours must be between 0 and 23")
        if not 30 <= result.max_call_seconds <= 3600:
            raise ValueError("Call duration must be between 30 and 3600 seconds")
        if min(result.hourly_calls, result.daily_calls) < 1:
            raise ValueError("Call limits must be positive")
        if min(result.blocker_delay_seconds, result.call_cooldown_seconds) < 0:
            raise ValueError("Delays cannot be negative")
        result.tz()
        return result

    def tz(self):
        try:
            return ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            # Windows has no system IANA database. Istanbul has fixed UTC+3.
            if self.timezone == "Europe/Istanbul":
                return timezone(timedelta(hours=3), self.timezone)
            raise ValueError("Install tzdata before using this timezone") from None

