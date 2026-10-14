from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from youtube_mcp.config import Settings, redact_text
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.transcript import (
    CaptionTrack,
    TranscriptRecord,
    TranscriptSource,
)
from youtube_mcp.schemas.video import (
    CommentEntry,
    MediaFormat,
    PlaylistItem,
    VideoInfo,
)
from youtube_mcp.services.transcript_normalizer import parse_subtitle_content

MAX_JSON_STDOUT_BYTES = 15 * 1024 * 1024
MAX_DESCRIPTION_CHARS = 4000

SubprocessExecFn = Callable[[list[str], float], Awaitable[tuple[int, bytes, bytes]]]


def encode_cursor(offset: int) -> str:
    payload = json.dumps({"offset": max(0, int(offset))}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
        offset = int(data.get("offset", 0))
        if offset < 0 or offset > 5000:
            raise ValueError("Cursor offset out of range")
        return offset
    except Exception as exc:
        raise AppError(ErrorCode.INVALID_ID, "Invalid pagination cursor.") from exc


def map_yt_dlp_error(stderr_text: str, target_id: str = "") -> AppError:
    lower = stderr_text.lower()
    details = {"target": target_id} if target_id else {}

    if any(
        marker in lower
        for marker in (
            "sign in to confirm you're not a bot",
            "confirm you’re not a bot",
            "http error 429",
            "too many requests",
            "http error 403",
            "blocked",
            "captcha",
        )
    ):
        return AppError(
            ErrorCode.YOUTUBE_ACCESS_BLOCKED,
            "YouTube blocked access from this server.",
            retryable=False,
            details=details,
        )

    if any(
        marker in lower
        for marker in (
            "video unavailable",
            "private video",
            "this video has been removed",
            "premieres in",
            "members-only",
            "sign in to confirm your age",
            "not available in your country",
        )
    ):
        return AppError(
            ErrorCode.VIDEO_UNAVAILABLE,
            "Requested YouTube resource is unavailable or restricted.",
            retryable=False,
            details=details,
        )

    if "file is larger than max-filesize" in lower or "max-filesize" in lower:
        return AppError(
            ErrorCode.RESOURCE_LIMIT,
            "Media file exceeds maximum allowed size budget.",
            retryable=False,
            details=details,
        )

    return AppError(
        ErrorCode.VIDEO_UNAVAILABLE,
        "yt-dlp could not extract the requested YouTube resource.",
        retryable=False,
        details=details,
    )


async def default_run_yt_dlp(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "yt-dlp binary is not installed in the runtime environment.",
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise AppError(
            ErrorCode.UPSTREAM_TIMEOUT,
            f"yt-dlp timed out after {timeout} seconds.",
            retryable=True,
        ) from exc

    if len(stdout) > MAX_JSON_STDOUT_BYTES:
        raise AppError(
            ErrorCode.RESOURCE_LIMIT,
            "yt-dlp output exceeded maximum metadata size limit.",
        )

    return proc.returncode or 0, stdout, stderr


class YtDlpAdapter:
    def __init__(
        self,
        settings: Settings,
        runner: SubprocessExecFn = default_run_yt_dlp,
    ) -> None:
        self._settings = settings
        self._runner = runner

    def _base_args(self, *, single_video: bool = True) -> list[str]:
        args = [
            "yt-dlp",
            "--no-config",
            "--no-warnings",
            "--no-progress",
            "--socket-timeout",
            "15",
        ]
        if single_video:
            args.append("--no-playlist")
        return args

    async def _extract_json(
        self,
        extra_args: list[str],
        target_url: str,
        *,
        single_video: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        argv = [*self._base_args(single_video=single_video), "--dump-single-json", *extra_args, "--", target_url]
        effective_timeout = timeout or float(self._settings.metadata_timeout_seconds)
        rc, stdout, stderr = await self._runner(argv, effective_timeout)
        stderr_clean = redact_text(stderr.decode("utf-8", errors="replace"))
        if rc != 0 or not stdout.strip():
            raise map_yt_dlp_error(stderr_clean, target_url)
        try:
            data = json.loads(stdout.decode("utf-8", errors="replace"))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            return data
        except Exception as exc:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "Failed to parse JSON metadata from yt-dlp.",
            ) from exc

    async def extract_video_info(self, video_id: str, canonical_url: str) -> VideoInfo:
        raw = await self._extract_json(["--skip-download"], canonical_url, single_video=True)
        return self._parse_video_info(raw, video_id, canonical_url)

    def _parse_video_info(self, raw: dict[str, Any], video_id: str, canonical_url: str) -> VideoInfo:
        desc = str(raw.get("description") or "")
        truncated = False
        if len(desc) > MAX_DESCRIPTION_CHARS:
            desc = desc[:MAX_DESCRIPTION_CHARS]
            truncated = True

        extractor_ver = str((raw.get("_version") or {}).get("version") or "yt-dlp")
        duration = raw.get("duration")
        view_count = raw.get("view_count")

        return VideoInfo(
            video_id=video_id,
            canonical_url=canonical_url,
            title=str(raw.get("title") or video_id),
            description=desc,
            description_truncated=truncated,
            channel_name=raw.get("uploader") or raw.get("channel"),
            channel_id=raw.get("channel_id") or raw.get("uploader_id"),
            channel_url=raw.get("channel_url") or raw.get("uploader_url"),
            upload_date=raw.get("upload_date"),
            duration_seconds=int(duration) if duration is not None else None,
            view_count=int(view_count) if view_count is not None else None,
            thumbnail_url=raw.get("thumbnail"),
            is_live=bool(raw.get("is_live", False)),
            availability=str(raw.get("availability") or "public"),
            extractor_version=extractor_ver,
            untrusted_content=True,
        )

    async def list_caption_tracks(self, video_id: str, canonical_url: str) -> list[CaptionTrack]:
        raw = await self._extract_json(
            ["--skip-download"],
            canonical_url,
            single_video=True,
            timeout=float(self._settings.caption_timeout_seconds),
        )
        tracks: list[CaptionTrack] = []
        subtitles = raw.get("subtitles") or {}
        auto_caps = raw.get("automatic_captions") or {}

        for lang, items in subtitles.items():
            name = items[0].get("name") if isinstance(items, list) and items else None
            tracks.append(
                CaptionTrack(
                    language=str(lang),
                    name=str(name) if name else None,
                    source=TranscriptSource.MANUAL_CAPTION,
                    is_generated=False,
                )
            )
        for lang, items in auto_caps.items():
            name = items[0].get("name") if isinstance(items, list) and items else None
            tracks.append(
                CaptionTrack(
                    language=str(lang),
                    name=str(name) if name else None,
                    source=TranscriptSource.AUTO_CAPTION,
                    is_generated=True,
                )
            )
        return tracks

    async def fetch_caption_transcript(
        self,
        video_id: str,
        canonical_url: str,
        languages: list[str] | None = None,
        allow_other_language: bool = False,
    ) -> TranscriptRecord:
        requested = [l.strip() for l in (languages or ["id", "en"]) if l.strip()]
        if not requested:
            requested = ["id", "en"]

        raw = await self._extract_json(
            ["--skip-download"],
            canonical_url,
            single_video=True,
            timeout=float(self._settings.caption_timeout_seconds),
        )
        subtitles: dict[str, Any] = raw.get("subtitles") or {}
        auto_caps: dict[str, Any] = raw.get("automatic_captions") or {}

        chosen_lang: str | None = None
        chosen_items: list[dict[str, Any]] | None = None
        is_generated = False

        for lang in requested:
            if lang in subtitles and subtitles[lang]:
                chosen_lang = lang
                chosen_items = subtitles[lang]
                is_generated = False
                break

        if chosen_items is None:
            for lang in requested:
                if lang in auto_caps and auto_caps[lang]:
                    chosen_lang = lang
                    chosen_items = auto_caps[lang]
                    is_generated = True
                    break

        if chosen_items is None and allow_other_language:
            for lang, items in subtitles.items():
                if items:
                    chosen_lang = lang
                    chosen_items = items
                    is_generated = False
                    break
            if chosen_items is None:
                for lang, items in auto_caps.items():
                    if items:
                        chosen_lang = lang
                        chosen_items = items
                        is_generated = True
                        break

        if not chosen_items or not chosen_lang:
            raise AppError(
                ErrorCode.CAPTION_UNAVAILABLE,
                "No matching manual or automatic captions found via yt-dlp.",
                details={"video_id": video_id, "requested_languages": requested},
            )

        inline_data: str | None = None
        chosen_ext = "vtt"
        for fmt_pref in ("vtt", "json3", "srv1", "ttml"):
            for item in chosen_items:
                if item.get("ext") == fmt_pref and item.get("data"):
                    inline_data = str(item["data"])
                    chosen_ext = fmt_pref
                    break
            if inline_data:
                break

        if inline_data is None:
            sub_dir = self._settings.tmp_dir / f"subs_{video_id}_{ int(time.time() * 1000) }"
            sub_dir.mkdir(parents=True, exist_ok=True)
            try:
                sub_flag = "--write-auto-subs" if is_generated else "--write-subs"
                argv = [
                    *self._base_args(single_video=True),
                    "--skip-download",
                    sub_flag,
                    "--sub-langs",
                    chosen_lang,
                    "--sub-format",
                    "vtt/best",
                    "-o",
                    str(sub_dir / "%(id)s.%(ext)s"),
                    "--",
                    canonical_url,
                ]
                rc, _, stderr = await self._runner(argv, float(self._settings.caption_timeout_seconds))
                if rc != 0:
                    raise map_yt_dlp_error(stderr.decode("utf-8", errors="replace"), video_id)
                sub_files = list(sub_dir.glob(f"{video_id}*"))
                if not sub_files:
                    raise AppError(
                        ErrorCode.CAPTION_UNAVAILABLE,
                        "Subtitle file was not produced by yt-dlp.",
                        details={"video_id": video_id},
                    )
                inline_data = sub_files[0].read_text(encoding="utf-8", errors="replace")
                chosen_ext = sub_files[0].suffix.lstrip(".") or "vtt"
            finally:
                for child in sub_dir.glob("*"):
                    child.unlink(missing_ok=True)
                try:
                    sub_dir.rmdir()
                except OSError:
                    pass

        segments = parse_subtitle_content(inline_data, fmt=chosen_ext)
        if not segments:
            raise AppError(
                ErrorCode.CAPTION_UNAVAILABLE,
                "Parsed subtitle track contained no usable segments.",
                details={"video_id": video_id},
            )

        return TranscriptRecord(
            video_id=video_id,
            language=chosen_lang,
            source=(
                TranscriptSource.AUTO_CAPTION
                if is_generated
                else TranscriptSource.MANUAL_CAPTION
            ),
            is_generated=is_generated,
            segments=segments,
            extractor_version="yt-dlp",
            created_at=time.time(),
        )

    async def extract_formats(self, video_id: str, canonical_url: str) -> list[MediaFormat]:
        raw = await self._extract_json(["--skip-download"], canonical_url, single_video=True)
        formats_raw = raw.get("formats") or []
        results: list[MediaFormat] = []
        for f in formats_raw[:60]:
            fid = str(f.get("format_id") or "")
            ext = str(f.get("ext") or "")
            if not fid or not ext:
                continue
            size = f.get("filesize") or f.get("filesize_approx")
            results.append(
                MediaFormat(
                    format_id=fid,
                    ext=ext,
                    resolution=f.get("resolution"),
                    fps=float(f["fps"]) if f.get("fps") is not None else None,
                    vcodec=f.get("vcodec"),
                    acodec=f.get("acodec"),
                    abr_kbps=float(f["abr"]) if f.get("abr") is not None else None,
                    tbr_kbps=float(f["tbr"]) if f.get("tbr") is not None else None,
                    filesize_bytes=int(size) if size is not None else None,
                    note=f.get("format_note"),
                )
            )
        return results

    async def extract_comments(
        self, video_id: str, canonical_url: str, limit: int = 20
    ) -> tuple[list[CommentEntry], str, str | None]:
        safe_limit = max(1, min(limit, 100))
        extra = [
            "--skip-download",
            "--write-comments",
            "--extractor-args",
            f"youtube:max_comments={safe_limit},all,{safe_limit},0",
        ]
        raw = await self._extract_json(extra, canonical_url, single_video=True)
        raw_comments = raw.get("comments")
        if raw_comments is None:
            return [], "empty", "Comments are disabled or unavailable for this video."

        entries: list[CommentEntry] = []
        for idx, c in enumerate(raw_comments[:safe_limit]):
            text = str(c.get("text") or "").strip()
            if not text:
                continue
            entries.append(
                CommentEntry(
                    comment_id=str(c.get("id") or f"c_{idx}"),
                    author=c.get("author"),
                    text=text[:1000],
                    like_count=int(c["like_count"]) if c.get("like_count") is not None else None,
                    published_time_text=c.get("_time_text"),
                    is_pinned=bool(c.get("is_pinned", False)),
                    untrusted_content=True,
                )
            )
        coverage = "partial" if len(raw_comments) >= safe_limit else "complete"
        if not entries:
            coverage = "empty"
        return entries, coverage, None

    async def search_videos(self, query: str, limit: int = 10) -> list[PlaylistItem]:
        safe_limit = max(1, min(limit, 20))
        search_spec = f"ytsearch{safe_limit}:{query}"
        raw = await self._extract_json(
            ["--flat-playlist", "--skip-download"],
            search_spec,
            single_video=False,
        )
        return self._parse_entries(raw.get("entries") or [], start_index=0)

    async def list_collection(
        self,
        canonical_url: str,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[str | None, list[PlaylistItem], bool]:
        safe_limit = max(1, min(limit, 50))
        start_item = offset + 1
        end_item = offset + safe_limit + 1
        raw = await self._extract_json(
            [
                "--flat-playlist",
                "--skip-download",
                "--playlist-start",
                str(start_item),
                "--playlist-end",
                str(end_item),
            ],
            canonical_url,
            single_video=False,
        )
        entries = list(raw.get("entries") or [])
        has_more = len(entries) > safe_limit
        sliced = entries[:safe_limit]
        title = raw.get("title") or raw.get("uploader") or raw.get("channel")
        return (
            str(title) if title else None,
            self._parse_entries(sliced, start_index=offset),
            has_more,
        )

    def _parse_entries(
        self, entries: list[dict[str, Any]], start_index: int = 0
    ) -> list[PlaylistItem]:
        items: list[PlaylistItem] = []
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            vid = str(entry.get("id") or "")
            if len(vid) != 11:
                continue
            dur = entry.get("duration")
            items.append(
                PlaylistItem(
                    video_id=vid,
                    canonical_url=f"https://www.youtube.com/watch?v={vid}",
                    title=str(entry.get("title") or vid),
                    duration_seconds=int(dur) if dur is not None else None,
                    channel_name=entry.get("uploader") or entry.get("channel"),
                    index=start_index + idx,
                )
            )
        return items

    async def download_audio_for_asr(
        self, video_id: str, canonical_url: str, work_dir: Path
    ) -> Path:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_template = str(work_dir / f"{video_id}.%(ext)s")
        max_bytes = self._settings.max_audio_work_bytes
        argv = [
            *self._base_args(single_video=True),
            "-f",
            "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio",
            "--max-filesize",
            str(max_bytes),
            "-o",
            out_template,
            "--",
            canonical_url,
        ]
        rc, _, stderr = await self._runner(
            argv, float(self._settings.media_download_timeout_seconds)
        )
        if rc != 0:
            raise map_yt_dlp_error(stderr.decode("utf-8", errors="replace"), video_id)

        candidates = [
            p for p in work_dir.iterdir() if p.is_file() and p.name.startswith(f"{video_id}.")
        ]
        if not candidates:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "Audio download completed without producing an output file.",
                details={"video_id": video_id},
            )
        audio_path = candidates[0]
        if audio_path.stat().st_size > max_bytes:
            audio_path.unlink(missing_ok=True)
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                "Downloaded audio exceeds maximum allowed work bytes.",
                details={"video_id": video_id},
            )
        return audio_path

    async def download_authorized_media(
        self,
        video_id: str,
        canonical_url: str,
        *,
        kind: str,
        format_preset: str,
        work_dir: Path,
    ) -> tuple[Path, str, str]:
        work_dir.mkdir(parents=True, exist_ok=True)
        out_template = str(work_dir / f"{video_id}_media.%(ext)s")
        max_bytes = self._settings.max_audio_work_bytes

        if kind == "audio":
            fmt_selector = "bestaudio[ext=m4a]/bestaudio"
            mime = "audio/mp4" if format_preset == "m4a" else "audio/webm"
        else:
            fmt_selector = "best[height<=720][ext=mp4]/best[ext=mp4]"
            mime = "video/mp4"

        argv = [
            *self._base_args(single_video=True),
            "-f",
            fmt_selector,
            "--max-filesize",
            str(max_bytes),
            "-o",
            out_template,
            "--",
            canonical_url,
        ]
        rc, _, stderr = await self._runner(
            argv, float(self._settings.media_download_timeout_seconds)
        )
        if rc != 0:
            raise map_yt_dlp_error(stderr.decode("utf-8", errors="replace"), video_id)

        candidates = [
            p for p in work_dir.iterdir() if p.is_file() and p.name.startswith(f"{video_id}_media.")
        ]
        if not candidates:
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "Media download completed without producing a file.",
            )
        path = candidates[0]
        ext = path.suffix.lstrip(".").lower() or ("m4a" if kind == "audio" else "mp4")
        mime_map = {
            "mp3": "audio/mpeg",
            "m4a": "audio/mp4",
            "wav": "audio/wav",
            "webm": "audio/webm" if kind == "audio" else "video/webm",
            "mp4": "video/mp4",
        }
        return path, mime_map.get(ext, mime), ext
