from __future__ import annotations

from youtube_mcp.config import Settings
from youtube_mcp.jobs.manager import JobManager
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.job import JobStatus
from youtube_mcp.schemas.transcript import (
    CaptionsListResponse,
    TranscriptPendingResponse,
    TranscriptReadyResponse,
    TranscriptRecord,
    TranscriptSearchResponse,
    TranscriptWindowResponse,
)
from youtube_mcp.services.transcript_api_adapter import TranscriptApiAdapter
from youtube_mcp.services.transcript_normalizer import (
    paginate_segments,
    search_segments,
    window_segments,
)
from youtube_mcp.services.url_validator import parse_video_id
from youtube_mcp.services.yt_dlp_adapter import YtDlpAdapter
from youtube_mcp.storage.cache import CacheRepository, compute_cache_key


class TranscriptResolver:
    def __init__(
        self,
        *,
        settings: Settings,
        cache_repo: CacheRepository,
        transcript_api: TranscriptApiAdapter,
        yt_dlp: YtDlpAdapter,
        job_manager: JobManager,
    ) -> None:
        self._settings = settings
        self._cache_repo = cache_repo
        self._transcript_api = transcript_api
        self._yt_dlp = yt_dlp
        self._job_manager = job_manager

    async def list_captions(self, url_or_id: str) -> CaptionsListResponse:
        target = parse_video_id(url_or_id)
        try:
            tracks = await self._transcript_api.list_tracks(target.video_id)
            return CaptionsListResponse(video_id=target.video_id, tracks=tracks, cached=False)
        except AppError as exc:
            if exc.code in (ErrorCode.YOUTUBE_ACCESS_BLOCKED, ErrorCode.VIDEO_UNAVAILABLE):
                raise
            tracks = await self._yt_dlp.list_caption_tracks(
                target.video_id, target.canonical_url
            )
            return CaptionsListResponse(video_id=target.video_id, tracks=tracks, cached=False)

    def _get_cached_transcript(
        self, video_id: str, languages: list[str], allow_other_language: bool
    ) -> TranscriptRecord | None:
        cached = self._cache_repo.get_by_video(
            video_id=video_id,
            languages=None if allow_other_language else languages,
        )
        if cached and "segments" in cached:
            rec = TranscriptRecord(**cached)
            if allow_other_language or rec.language in languages or rec.source == "whisper_asr":
                return rec
        return None

    def _save_caption_to_cache(self, record: TranscriptRecord) -> str:
        key = compute_cache_key(
            provider="captions",
            video_id=record.video_id,
            language=record.language,
            source_policy=record.source.value,
            version=record.extractor_version,
        )
        self._cache_repo.put(
            key=key,
            kind="captions",
            video_id=record.video_id,
            payload=record.model_dump(),
            language=record.language,
            source=record.source.value,
            extractor_version=record.extractor_version,
        )
        return key

    async def resolve_record(
        self,
        *,
        video_id: str,
        canonical_url: str,
        languages: list[str],
        allow_other_language: bool = False,
    ) -> tuple[TranscriptRecord, bool]:
        cached_rec = self._get_cached_transcript(video_id, languages, allow_other_language)
        if cached_rec is not None:
            return cached_rec, True

        try:
            rec = await self._transcript_api.fetch_transcript(
                video_id, languages=languages, allow_other_language=allow_other_language
            )
            self._save_caption_to_cache(rec)
            return rec, False
        except AppError as api_err:
            if api_err.code in (ErrorCode.YOUTUBE_ACCESS_BLOCKED, ErrorCode.VIDEO_UNAVAILABLE):
                raise

        rec = await self._yt_dlp.fetch_caption_transcript(
            video_id,
            canonical_url,
            languages=languages,
            allow_other_language=allow_other_language,
        )
        self._save_caption_to_cache(rec)
        return rec, False

    async def get_transcript(
        self,
        *,
        url_or_id: str,
        principal_id: str,
        languages: list[str] | None = None,
        allow_asr: bool = True,
        allow_other_language: bool = False,
        offset: int = 0,
        limit: int = 500,
    ) -> TranscriptReadyResponse | TranscriptPendingResponse:
        target = parse_video_id(url_or_id)
        req_langs = [l.strip() for l in (languages or ["en", "id"]) if l.strip()] or ["en", "id"]
        safe_limit = max(1, min(limit, self._settings.max_segments_per_page))
        safe_offset = max(0, offset)

        try:
            record, is_cached = await self.resolve_record(
                video_id=target.video_id,
                canonical_url=target.canonical_url,
                languages=req_langs,
                allow_other_language=allow_other_language,
            )
            page_segments, page_meta = paginate_segments(
                record.segments,
                offset=safe_offset,
                limit=safe_limit,
                max_chars=self._settings.max_mcp_response_chars,
            )
            return TranscriptReadyResponse(
                video_id=record.video_id,
                language=record.language,
                source=record.source,
                is_generated=record.is_generated,
                cached=is_cached,
                segments=page_segments,
                pagination=page_meta,
            )
        except AppError as exc:
            if exc.code != ErrorCode.CAPTION_UNAVAILABLE:
                raise
            if not allow_asr or not self._settings.whisper_enabled:
                raise

        known_duration = await self._preflight_check_duration(
            target.video_id, target.canonical_url
        )
        job = self._job_manager.submit_asr_job(
            principal_id=principal_id,
            video_id=target.video_id,
            language=req_langs[0],
            known_duration_seconds=known_duration,
        )

        if job.status == JobStatus.SUCCEEDED and job.result_ref:
            cached_payload = self._cache_repo.get(job.result_ref)
            if cached_payload:
                record = TranscriptRecord(**cached_payload)
                page_segments, page_meta = paginate_segments(
                    record.segments,
                    offset=safe_offset,
                    limit=safe_limit,
                    max_chars=self._settings.max_mcp_response_chars,
                )
                return TranscriptReadyResponse(
                    video_id=record.video_id,
                    language=record.language,
                    source=record.source,
                    is_generated=record.is_generated,
                    cached=True,
                    segments=page_segments,
                    pagination=page_meta,
                )

        return TranscriptPendingResponse(
            video_id=target.video_id,
            job_id=job.id,
            job_state=job.status.value,
        )

    async def _preflight_check_duration(self, video_id: str, canonical_url: str) -> int | None:
        meta_key = compute_cache_key(provider="yt_dlp_metadata", video_id=video_id)
        cached_meta = self._cache_repo.get(meta_key)
        if cached_meta and cached_meta.get("duration_seconds") is not None:
            return int(cached_meta["duration_seconds"])
        info = await self._yt_dlp.extract_video_info(video_id, canonical_url)
        self._cache_repo.put(
            key=meta_key,
            kind="metadata",
            video_id=video_id,
            payload=info.model_dump(),
            extractor_version=info.extractor_version,
        )
        if info.is_live:
            raise AppError(
                ErrorCode.UNSUPPORTED_OPERATION,
                "Live stream transcription is not supported.",
                details={"video_id": video_id},
            )
        return info.duration_seconds

    async def get_transcript_window(
        self,
        *,
        video_id_or_url: str,
        start_sec: float,
        end_sec: float,
        languages: list[str] | None = None,
        max_chars: int | None = None,
    ) -> TranscriptWindowResponse:
        if start_sec < 0 or end_sec < start_sec:
            raise AppError(
                ErrorCode.INVALID_ID,
                "Invalid time window: require 0 <= start_sec <= end_sec.",
            )
        target = parse_video_id(video_id_or_url)
        req_langs = [l.strip() for l in (languages or ["en", "id"]) if l.strip()] or ["en", "id"]
        record, _ = await self.resolve_record(
            video_id=target.video_id,
            canonical_url=target.canonical_url,
            languages=req_langs,
            allow_other_language=True,
        )
        effective_max = min(
            max(100, max_chars or self._settings.max_mcp_response_chars),
            self._settings.max_mcp_response_chars,
        )
        segs, truncated = window_segments(
            record.segments,
            start_sec=start_sec,
            end_sec=end_sec,
            max_chars=effective_max,
        )
        return TranscriptWindowResponse(
            video_id=record.video_id,
            language=record.language,
            source=record.source,
            start_sec=start_sec,
            end_sec=end_sec,
            truncated=truncated,
            segments=segs,
        )

    async def search_transcript(
        self,
        *,
        video_id_or_url: str,
        query: str,
        limit: int = 20,
        languages: list[str] | None = None,
    ) -> TranscriptSearchResponse:
        cleaned_query = query.strip()
        if not cleaned_query or len(cleaned_query) > 200:
            raise AppError(
                ErrorCode.INVALID_ID,
                "Transcript search query must be between 1 and 200 characters.",
            )
        target = parse_video_id(video_id_or_url)
        req_langs = [l.strip() for l in (languages or ["en", "id"]) if l.strip()] or ["en", "id"]
        record, _ = await self.resolve_record(
            video_id=target.video_id,
            canonical_url=target.canonical_url,
            languages=req_langs,
            allow_other_language=True,
        )
        matches = search_segments(record.segments, query=cleaned_query, limit=limit)
        return TranscriptSearchResponse(
            video_id=record.video_id,
            query=cleaned_query,
            language=record.language,
            source=record.source,
            matches=matches,
        )
