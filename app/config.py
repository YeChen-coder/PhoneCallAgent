"""Runtime settings (env / .env) and the clinic's static configuration (clinic.json)."""

from __future__ import annotations

import json
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""
    realtime_model: str = "gpt-realtime-2.1"
    realtime_voice: str = "marin"
    transcribe_model: str = "gpt-4o-mini-transcribe"
    # End-of-turn detection. semantic_vad waits until the caller *sounds* finished (can lag seconds
    # on 8 kHz phone audio); server_vad fires after a fixed silence. Tune with scripts/simulate_call.
    # Measured with scripts/simulate_call (2026-10-17): semantic_vad medium lagged up to ~5 s after long
    # sentences; server_vad 500 ms gave a steady ~1.5 s. 600 ms leaves room for callers' mid-sentence pauses.
    vad_type: str = "server_vad"
    vad_eagerness: str = "high"  # semantic_vad: low | medium | high | auto
    vad_silence_ms: int = 600  # server_vad
    text_model: str = "gpt-5-mini"
    openai_webhook_secret: str = ""
    # Override for Azure OpenAI or a local fake server in tests
    openai_realtime_url: str = "wss://api.openai.com/v1/realtime"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_validate_signature: bool = True

    public_base_url: str = "http://localhost:8000"

    database_url: str = "sqlite:///./data/clinic.db"
    clinic_config_path: str = "./clinic.json"
    knowledge_dir: str = "./knowledge"
    app_secret: str = "change-me"
    admin_token: str = "change-me-too"
    send_sms_confirmations: bool = False
    summarize_calls: bool = True


class Service(BaseModel):
    key: str
    name: str
    minutes: int


class ProviderCfg(BaseModel):
    name: str
    name_zh: str | None = None
    aliases: list[str] = []  # what callers actually say: "沈医生", "Dr. Shen"
    languages: list[str] = ["English"]
    services: list[str]
    accepting_new_patients: bool = True


_TITLES = ("doctor", "dr.", "dr ", "nurse", "医生", "大夫", "护士")


def _norm(s: str) -> str:
    s = s.lower()
    for t in _TITLES:
        s = s.replace(t, "")
    return s.replace(" ", "").strip()


class ClinicConfig(BaseModel):
    name: str
    name_zh: str | None = None
    timezone: str
    phone_transfer_number: str
    front_desk_languages: list[str] = ["English"]
    slot_minutes: int = 15
    min_booking_lead_minutes: int = 60
    max_booking_days_ahead: int = 60
    hours: dict[str, list[tuple[time, time]]]
    closed_dates: list[date] = []
    services: list[Service]
    providers: list[ProviderCfg]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def service(self, key: str) -> Service | None:
        return next((s for s in self.services if s.key == key), None)

    def provider_cfg(self, provider_name: str) -> ProviderCfg | None:
        return next((p for p in self.providers if p.name == provider_name), None)

    def accepts_new(self, provider_name: str) -> bool:
        cfg = self.provider_cfg(provider_name)
        return cfg.accepting_new_patients if cfg else True

    def provider_matches(self, provider_name: str, query: str) -> bool:
        """Does what the caller/model said ("沈医生", "Dr. Shen", "shen") refer to this provider?"""
        q = _norm(query)
        if not q:
            return False
        cfg = self.provider_cfg(provider_name)
        candidates = [provider_name, *(cfg.aliases if cfg else []), *([cfg.name_zh] if cfg and cfg.name_zh else [])]
        return any(q in _norm(c) or (_norm(c) and _norm(c) in q) for c in candidates)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_clinic() -> ClinicConfig:
    return ClinicConfig.model_validate(json.loads(Path(get_settings().clinic_config_path).read_text("utf-8")))
