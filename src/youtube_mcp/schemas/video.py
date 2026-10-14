from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from youtube_mcp.schemas.common import CursorPaginationMeta


class VideoInfo(BaseModel):
    video_id: str
    canonical_url: str
    title: str
    description: str = ""
    description_truncated: bool = False
    channel_name: str | None = None
    channel_id: str | None = None
    channel_url: str | None = None
    upload_date: str | None = None
    duration_seconds: int | None = None
    view_count: int | None = None
    thumbnail_url: str | None = None
    is_live: bool = False
    availability: str = "public"
    extractor_version: str = "unknown"
    untrusted_content: bool = True


class VideoInfoResponse(BaseModel):
    status: Literal["ready", "partial"] = "ready"
    video: VideoInfo
    cached: bool = False
    provenance: str = "yt_dlp"


class MediaFormat(BaseModel):
    format_id: str
    ext: str
    resolution: str | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    abr_kbps: float | None = None
    tbr_kbps: float | None = None
    filesize_bytes: int | None = None
    note: str | None = None


class FormatsResponse(BaseModel):
    status: Literal["ready", "partial"] = "ready"
    video_id: str
    formats: list[MediaFormat] = Field(default_factory=list)
    cached: bool = False


class PlaylistItem(BaseModel):
    video_id: str
    canonical_url: str
    title: str
    duration_seconds: int | None = None
    channel_name: str | None = None
    index: int | None = None


class PlaylistResponse(BaseModel):
    status: Literal["ready", "partial"] = "ready"
    playlist_id: str
    title: str | None = None
    items: list[PlaylistItem] = Field(default_factory=list)
    pagination: CursorPaginationMeta


class ChannelResponse(BaseModel):
    status: Literal["ready", "partial"] = "ready"
    channel_ref: str
    channel_name: str | None = None
    items: list[PlaylistItem] = Field(default_factory=list)
    pagination: CursorPaginationMeta


class CommentEntry(BaseModel):
    comment_id: str
    author: str | None = None
    text: str
    like_count: int | None = None
    published_time_text: str | None = None
    is_pinned: bool = False
    untrusted_content: bool = True


class CommentsResponse(BaseModel):
    status: Literal["ready", "partial"] = "ready"
    video_id: str
    coverage: Literal["complete", "partial", "empty"] = "partial"
    comments: list[CommentEntry] = Field(default_factory=list)
    reason: str | None = None


class SearchResponse(BaseModel):
    status: Literal["ready", "partial"] = "ready"
    query: str
    items: list[PlaylistItem] = Field(default_factory=list)
    limit: int
