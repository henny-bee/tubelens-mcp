from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from youtube_mcp.schemas.common import PaginationMeta


class TranscriptSource(StrEnum):
    MANUAL_CAPTION = "youtube_manual_caption"
    AUTO_CAPTION = "youtube_auto_caption"
    TRANSLATED_CAPTION = "youtube_translated_caption"
    WHISPER_ASR = "whisper_asr"


class TranscriptSegment(BaseModel):
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    text: str


class CaptionTrack(BaseModel):
    language: str
    name: str | None = None
    source: TranscriptSource
    is_generated: bool = False
    is_translatable: bool = False


class CaptionsListResponse(BaseModel):
    status: Literal["ready", "partial"] = "ready"
    video_id: str
    tracks: list[CaptionTrack] = Field(default_factory=list)
    cached: bool = False


class TranscriptRecord(BaseModel):
    video_id: str
    language: str
    source: TranscriptSource
    is_generated: bool
    segments: list[TranscriptSegment]
    extractor_version: str = "1.0"
    created_at: float = 0.0
    untrusted_content: bool = True


class TranscriptReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    video_id: str
    language: str
    source: TranscriptSource
    is_generated: bool
    cached: bool
    untrusted_content: bool = True
    segments: list[TranscriptSegment]
    pagination: PaginationMeta


class TranscriptPendingResponse(BaseModel):
    status: Literal["pending"] = "pending"
    video_id: str
    job_id: str
    job_state: str
    message: str = "Captions unavailable; ASR is queued"
    poll_tool: str = "youtube_get_job_status"


class TranscriptWindowResponse(BaseModel):
    status: Literal["ready"] = "ready"
    video_id: str
    language: str
    source: TranscriptSource
    start_sec: float
    end_sec: float
    truncated: bool = False
    untrusted_content: bool = True
    segments: list[TranscriptSegment]


class TranscriptSearchMatch(BaseModel):
    start: float
    end: float
    text: str
    segment_index: int


class TranscriptSearchResponse(BaseModel):
    status: Literal["ready"] = "ready"
    video_id: str
    query: str
    language: str
    source: TranscriptSource
    untrusted_content: bool = True
    matches: list[TranscriptSearchMatch]
