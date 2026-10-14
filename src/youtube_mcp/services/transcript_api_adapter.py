from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from youtube_transcript_api import YouTubeTranscriptApi

from youtube_mcp.config import Settings
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.transcript import (
    CaptionTrack,
    TranscriptRecord,
    TranscriptSource,
)
from youtube_mcp.services.transcript_normalizer import normalize_segments


def map_transcript_api_error(exc: Exception, video_id: str) -> AppError:
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return AppError(
            ErrorCode.UPSTREAM_TIMEOUT,
            "Timed out while querying YouTube transcript API.",
            retryable=True,
            details={"video_id": video_id},
        )

    cls_name = exc.__class__.__name__.lower()
    msg = str(exc).lower()

    if any(
        token in cls_name or token in msg
        for token in ("requestblocked", "ipblocked", "too many requests", "429", "captcha", "bot")
    ):
        return AppError(
            ErrorCode.YOUTUBE_ACCESS_BLOCKED,
            "YouTube blocked transcript retrieval from this server IP.",
            retryable=False,
            details={"video_id": video_id},
        )

    if any(
        token in cls_name or token in msg
        for token in ("videounavailable", "videounplayable", "agerestricted", "private")
    ):
        return AppError(
            ErrorCode.VIDEO_UNAVAILABLE,
            "Video is unavailable, private, or restricted.",
            retryable=False,
            details={"video_id": video_id},
        )

    if any(
        token in cls_name or token in msg
        for token in (
            "transcriptsdisabled",
            "notranscriptfound",
            "couldnotretrievetranscript",
            "no transcript",
            "subtitles are disabled",
        )
    ):
        return AppError(
            ErrorCode.CAPTION_UNAVAILABLE,
            "No captions are available for the requested video and languages.",
            retryable=False,
            details={"video_id": video_id},
        )

    return AppError(
        ErrorCode.CAPTION_UNAVAILABLE,
        "Transcript API could not retrieve captions for this video.",
        retryable=False,
        details={"video_id": video_id, "reason": exc.__class__.__name__},
    )


class TranscriptApiAdapter:
    def __init__(
        self,
        settings: Settings,
        api_factory: Callable[[], Any] = YouTubeTranscriptApi,
    ) -> None:
        self._settings = settings
        self._api_factory = api_factory

    @staticmethod
    def _is_native_track(tr: Any) -> bool:
        if not bool(getattr(tr, "is_generated", False)):
            return True
        url = str(getattr(tr, "_url", ""))
        return "variant=timing-optimized" not in url and "tlang=" not in url

    async def list_tracks(self, video_id: str) -> list[CaptionTrack]:
        def _sync_list() -> list[CaptionTrack]:
            api = self._api_factory()
            transcript_list = api.list(video_id)
            tracks: list[CaptionTrack] = []
            for tr in transcript_list:
                is_gen = bool(getattr(tr, "is_generated", False))
                is_native = self._is_native_track(tr)
                if not is_gen:
                    source = TranscriptSource.MANUAL_CAPTION
                elif is_native:
                    source = TranscriptSource.AUTO_CAPTION
                else:
                    source = TranscriptSource.TRANSLATED_CAPTION
                tracks.append(
                    CaptionTrack(
                        language=str(getattr(tr, "language_code", "en")),
                        name=str(getattr(tr, "language", "")),
                        source=source,
                        is_generated=is_gen,
                        is_translatable=bool(getattr(tr, "is_translatable", False)),
                    )
                )
            return tracks

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_sync_list),
                timeout=self._settings.caption_timeout_seconds,
            )
        except Exception as exc:
            raise map_transcript_api_error(exc, video_id) from exc

    async def fetch_transcript(
        self,
        video_id: str,
        languages: list[str] | None = None,
        allow_other_language: bool = False,
    ) -> TranscriptRecord:
        requested_langs = [lang.strip() for lang in (languages or ["en", "id"]) if lang.strip()]
        if not requested_langs:
            requested_langs = ["en", "id"]

        def _sync_fetch() -> TranscriptRecord:
            api = self._api_factory()
            selected_tr = None
            is_translated_track = False
            if hasattr(api, "list"):
                try:
                    t_list = list(api.list(video_id))
                    manual_by_lang = {
                        str(getattr(t, "language_code", "")): t
                        for t in t_list
                        if not bool(getattr(t, "is_generated", False))
                    }
                    native_auto_by_lang = {
                        str(getattr(t, "language_code", "")): t
                        for t in t_list
                        if bool(getattr(t, "is_generated", False)) and self._is_native_track(t)
                    }
                    translated_by_lang = {
                        str(getattr(t, "language_code", "")): t
                        for t in t_list
                        if bool(getattr(t, "is_generated", False)) and not self._is_native_track(t)
                    }

                    for lang in requested_langs:
                        if lang in manual_by_lang:
                            selected_tr = manual_by_lang[lang]
                            break
                    if selected_tr is None:
                        for lang in requested_langs:
                            if lang in native_auto_by_lang:
                                selected_tr = native_auto_by_lang[lang]
                                break
                    if selected_tr is None and allow_other_language:
                        if manual_by_lang:
                            selected_tr = next(iter(manual_by_lang.values()))
                        elif native_auto_by_lang:
                            selected_tr = next(iter(native_auto_by_lang.values()))
                    if selected_tr is None:
                        for lang in requested_langs:
                            if lang in translated_by_lang:
                                selected_tr = translated_by_lang[lang]
                                is_translated_track = True
                                break
                    if selected_tr is None and allow_other_language and t_list:
                        selected_tr = t_list[0]
                        is_translated_track = not self._is_native_track(selected_tr)
                except Exception as list_exc:
                    cls_n = list_exc.__class__.__name__.lower()
                    if "blocked" in cls_n or "unavailable" in cls_n:
                        raise

            if selected_tr is not None:
                fetched = selected_tr.fetch()
                is_gen = bool(getattr(selected_tr, "is_generated", False))
                lang_code = str(getattr(selected_tr, "language_code", requested_langs[0]))
            else:
                fetched = api.fetch(video_id, languages=requested_langs)
                is_gen = bool(getattr(fetched, "is_generated", False))
                lang_code = str(getattr(fetched, "language_code", requested_langs[0]))

            snippets = getattr(fetched, "snippets", fetched)
            raw_segments: list[dict[str, Any]] = []
            for item in snippets:
                if isinstance(item, dict):
                    raw_segments.append(item)
                else:
                    raw_segments.append(
                        {
                            "start": float(getattr(item, "start", 0.0)),
                            "duration": float(getattr(item, "duration", 0.0)),
                            "text": str(getattr(item, "text", "")),
                        }
                    )

            segments = normalize_segments(raw_segments)
            if not segments:
                raise AppError(
                    ErrorCode.CAPTION_UNAVAILABLE,
                    "Caption track was empty after normalization.",
                    details={"video_id": video_id},
                )

            if not is_gen:
                source = TranscriptSource.MANUAL_CAPTION
            elif is_translated_track:
                source = TranscriptSource.TRANSLATED_CAPTION
            else:
                source = TranscriptSource.AUTO_CAPTION

            return TranscriptRecord(
                video_id=video_id,
                language=lang_code,
                source=source,
                is_generated=is_gen,
                segments=segments,
                extractor_version="youtube-transcript-api",
                created_at=time.time(),
            )

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_sync_fetch),
                timeout=self._settings.caption_timeout_seconds,
            )
        except Exception as exc:
            raise map_transcript_api_error(exc, video_id) from exc
