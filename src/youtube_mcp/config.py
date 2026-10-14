from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_SECRETS = {
    "",
    "REPLACE_WITH_RANDOM_SECRET",
    "changeme",
    "secret",
    "password",
    "token",
}

SENSITIVE_PATTERNS = re.compile(
    r"(Bearer\s+[A-Za-z0-9\-._~+/]+=*|token=[^&\s]+|cookie[s]?=[^;\s]+|MCP_BEARER_TOKEN=\S+)",
    re.IGNORECASE,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    mcp_host: str = Field(default="0.0.0.0", alias="MCP_HOST")
    mcp_port: int = Field(default=8000, alias="MCP_PORT", ge=1, le=65535)
    mcp_public_base_url: str = Field(
        default="https://youtube.example.com", alias="MCP_PUBLIC_BASE_URL"
    )
    auth_mode: Literal["bearer", "trusted_ingress"] = Field(
        default="bearer", alias="AUTH_MODE"
    )
    mcp_bearer_token: SecretStr = Field(
        default=SecretStr(""), alias="MCP_BEARER_TOKEN"
    )
    mcp_default_scopes: list[str] = Field(
        default_factory=lambda: ["read"], alias="MCP_DEFAULT_SCOPES"
    )
    mcp_tokens_json: str = Field(default="", alias="MCP_TOKENS_JSON")
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")

    whisper_enabled: bool = Field(default=True, alias="WHISPER_ENABLED")
    whisper_model: Literal["tiny", "base", "small"] = Field(
        default="base", alias="WHISPER_MODEL"
    )
    whisper_compute_type: Literal["int8", "int8_float32", "float32"] = Field(
        default="int8", alias="WHISPER_COMPUTE_TYPE"
    )
    whisper_cpu_threads: int = Field(default=1, alias="WHISPER_CPU_THREADS", ge=1, le=8)
    whisper_beam_size: int = Field(default=1, alias="WHISPER_BEAM_SIZE", ge=1, le=5)
    whisper_vad_enabled: bool = Field(default=True, alias="WHISPER_VAD_ENABLED")

    max_asr_concurrency: int = Field(default=1, alias="MAX_ASR_CONCURRENCY", ge=1, le=4)
    max_queue_size: int = Field(default=10, alias="MAX_QUEUE_SIZE", ge=1, le=100)
    max_asr_duration_seconds: int = Field(
        default=2700, alias="MAX_ASR_DURATION_SECONDS", ge=10, le=14400
    )
    max_audio_work_bytes: int = Field(
        default=262_144_000, alias="MAX_AUDIO_WORK_BYTES", ge=1_048_576
    )
    asr_timeout_seconds: int = Field(
        default=1800, alias="ASR_TIMEOUT_SECONDS", ge=10, le=7200
    )
    media_download_timeout_seconds: int = Field(
        default=900, alias="MEDIA_DOWNLOAD_TIMEOUT_SECONDS", ge=10, le=3600
    )
    metadata_timeout_seconds: int = Field(
        default=30, alias="METADATA_TIMEOUT_SECONDS", ge=2, le=120
    )
    caption_timeout_seconds: int = Field(
        default=20, alias="CAPTION_TIMEOUT_SECONDS", ge=2, le=120
    )
    max_mcp_response_chars: int = Field(
        default=12_000, alias="MAX_MCP_RESPONSE_CHARS", ge=500, le=100_000
    )
    max_segments_per_page: int = Field(
        default=100, alias="MAX_SEGMENTS_PER_PAGE", ge=1, le=500
    )

    metadata_cache_ttl_seconds: int = Field(
        default=21_600, alias="METADATA_CACHE_TTL_SECONDS", ge=60
    )
    caption_cache_ttl_seconds: int = Field(
        default=604_800, alias="CAPTION_CACHE_TTL_SECONDS", ge=60
    )
    asr_cache_ttl_seconds: int = Field(
        default=2_592_000, alias="ASR_CACHE_TTL_SECONDS", ge=60
    )
    artifact_ttl_seconds: int = Field(
        default=86_400, alias="ARTIFACT_TTL_SECONDS", ge=60
    )
    max_storage_bytes: int = Field(
        default=5_368_709_120, alias="MAX_STORAGE_BYTES", ge=10_485_760
    )

    rate_limit_requests_per_minute: int = Field(
        default=60, alias="RATE_LIMIT_REQUESTS_PER_MINUTE", ge=1, le=10_000
    )
    max_concurrent_tool_calls: int = Field(
        default=8, alias="MAX_CONCURRENT_TOOL_CALLS", ge=1, le=100
    )
    cleanup_interval_seconds: int = Field(
        default=300, alias="CLEANUP_INTERVAL_SECONDS", ge=10
    )
    max_job_attempts: int = Field(default=2, alias="MAX_JOB_ATTEMPTS", ge=1, le=5)

    enable_media_download: bool = Field(default=False, alias="ENABLE_MEDIA_DOWNLOAD")
    enable_comment_extraction: bool = Field(
        default=True, alias="ENABLE_COMMENT_EXTRACTION"
    )
    enable_search: bool = Field(default=True, alias="ENABLE_SEARCH")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )

    @field_validator("mcp_public_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        stripped = value.strip().rstrip("/")
        if not stripped.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ValueError(
                "MCP_PUBLIC_BASE_URL must use https:// (or http://localhost for local testing)"
            )
        return stripped

    @model_validator(mode="after")
    def validate_auth_secrets(self) -> "Settings":
        if self.auth_mode == "bearer":
            raw_token = self.mcp_bearer_token.get_secret_value().strip()
            has_tokens_map = bool(self.mcp_tokens_json.strip())
            if not has_tokens_map and (raw_token in PLACEHOLDER_SECRETS or len(raw_token) < 16):
                raise ValueError(
                    "MCP_BEARER_TOKEN must be set to a non-placeholder secret of at least 16 chars when AUTH_MODE=bearer"
                )
        return self

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "app.sqlite3"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def metadata_cache_dir(self) -> Path:
        return self.cache_dir / "metadata"

    @property
    def captions_cache_dir(self) -> Path:
        return self.cache_dir / "captions"

    @property
    def asr_cache_dir(self) -> Path:
        return self.cache_dir / "asr"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    def ensure_directories(self) -> None:
        for directory in (
            self.state_dir,
            self.metadata_cache_dir,
            self.captions_cache_dir,
            self.asr_cache_dir,
            self.models_dir,
            self.artifacts_dir,
            self.tmp_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def redacted_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        data["mcp_bearer_token"] = "***REDACTED***"
        if data.get("mcp_tokens_json"):
            data["mcp_tokens_json"] = "***REDACTED***"
        data["data_dir"] = str(self.data_dir)
        return data


def redact_text(text: str, extra_secrets: list[str] | None = None) -> str:
    if not text:
        return text
    cleaned = SENSITIVE_PATTERNS.sub("[REDACTED]", text)
    for secret in extra_secrets or []:
        if secret and len(secret) >= 6:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned


class StructuredJsonFormatter(logging.Formatter):
    def __init__(self, secrets_to_redact: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets_to_redact or []) if s]

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage(), self._secrets),
        }
        for field in (
            "request_id",
            "principal_id",
            "tool",
            "elapsed_ms",
            "cache_hit",
            "job_id",
            "job_state",
            "error_code",
            "video_id",
        ):
            val = getattr(record, field, None)
            if val is not None:
                payload[field] = val
        return json.dumps(payload, ensure_ascii=True)


def configure_logging(settings: Settings) -> logging.Logger:
    logger = logging.getLogger("youtube_mcp")
    logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
    logger.handlers.clear()
    handler = logging.StreamHandler()
    raw_secret = settings.mcp_bearer_token.get_secret_value()
    handler.setFormatter(StructuredJsonFormatter(secrets_to_redact=[raw_secret]))
    logger.addHandler(handler)
    logger.propagate = False
    return logger
