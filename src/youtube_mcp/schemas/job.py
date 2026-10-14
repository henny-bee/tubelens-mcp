from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class JobKind(StrEnum):
    ASR_TRANSCRIBE = "asr_transcribe"
    MEDIA_DOWNLOAD = "media_download"


class JobRecord(BaseModel):
    id: str
    principal_id: str
    kind: JobKind
    job_key: str
    video_id: str
    options: dict[str, Any] = Field(default_factory=dict)
    status: JobStatus
    progress: float = Field(default=0.0, ge=0.0, le=100.0)
    error_code: str | None = None
    error_message: str | None = None
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    attempts: int = 0
    result_ref: str | None = None
    expires_at: float | None = None


class JobStatusResponse(BaseModel):
    status: Literal["ready"] = "ready"
    job_id: str
    kind: JobKind
    video_id: str
    job_state: JobStatus
    progress: float = 0.0
    error_code: str | None = None
    error_message: str | None = None
    result_ref: str | None = None
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None


class JobCancelResponse(BaseModel):
    status: Literal["ready"] = "ready"
    job_id: str
    cancelled: bool
    job_state: JobStatus
    message: str


class ArtifactMeta(BaseModel):
    id: str
    job_id: str
    path: str
    mime: str
    size_bytes: int
    created_at: float
    expires_at: float


class ArtifactResponse(BaseModel):
    status: Literal["ready"] = "ready"
    artifact_id: str
    job_id: str
    mime: str
    size_bytes: int
    expires_at: float
    download_url: str
