from __future__ import annotations

from youtube_mcp.config import Settings
from youtube_mcp.schemas.common import AppError, CursorPaginationMeta, ErrorCode
from youtube_mcp.schemas.video import (
    ChannelResponse,
    CommentsResponse,
    FormatsResponse,
    PlaylistResponse,
    SearchResponse,
    VideoInfo,
    VideoInfoResponse,
)
from youtube_mcp.services.url_validator import (
    parse_channel_ref,
    parse_playlist_id,
    parse_video_id,
    validate_search_query,
)
from youtube_mcp.services.yt_dlp_adapter import (
    YtDlpAdapter,
    decode_cursor,
    encode_cursor,
)
from youtube_mcp.storage.cache import CacheRepository, compute_cache_key


class YouTubeService:
    def __init__(
        self,
        *,
        settings: Settings,
        cache_repo: CacheRepository,
        yt_dlp: YtDlpAdapter,
    ) -> None:
        self._settings = settings
        self._cache_repo = cache_repo
        self._yt_dlp = yt_dlp

    async def get_video_info(
        self, url_or_id: str, *, refresh: bool = False
    ) -> VideoInfoResponse:
        target = parse_video_id(url_or_id)
        cache_key = compute_cache_key(provider="yt_dlp_metadata", video_id=target.video_id)

        if not refresh:
            cached = self._cache_repo.get(cache_key)
            if cached is not None:
                return VideoInfoResponse(
                    status="ready",
                    video=VideoInfo(**cached),
                    cached=True,
                )

        info = await self._yt_dlp.extract_video_info(target.video_id, target.canonical_url)
        self._cache_repo.put(
            key=cache_key,
            kind="metadata",
            video_id=target.video_id,
            payload=info.model_dump(),
            extractor_version=info.extractor_version,
        )
        return VideoInfoResponse(
            status="ready",
            video=info,
            cached=False,
        )

    async def get_formats(self, url_or_id: str) -> FormatsResponse:
        target = parse_video_id(url_or_id)
        formats = await self._yt_dlp.extract_formats(target.video_id, target.canonical_url)
        return FormatsResponse(
            status="ready" if formats else "partial",
            video_id=target.video_id,
            formats=formats,
            cached=False,
        )

    async def get_comments(self, url_or_id: str, *, limit: int = 20) -> CommentsResponse:
        if not self._settings.enable_comment_extraction:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Comment extraction is disabled by operator policy (ENABLE_COMMENT_EXTRACTION=false).",
            )
        target = parse_video_id(url_or_id)
        safe_limit = max(1, min(limit, 100))
        comments, coverage, reason = await self._yt_dlp.extract_comments(
            target.video_id, target.canonical_url, limit=safe_limit
        )
        return CommentsResponse(
            status="ready" if coverage == "complete" else "partial",
            video_id=target.video_id,
            coverage=coverage,  # type: ignore[arg-type]
            comments=comments,
            reason=reason,
        )

    async def search_videos(self, query: str, *, limit: int = 10) -> SearchResponse:
        if not self._settings.enable_search:
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Video search is disabled by operator policy (ENABLE_SEARCH=false).",
            )
        cleaned_query = validate_search_query(query)
        safe_limit = max(1, min(limit, 20))
        items = await self._yt_dlp.search_videos(cleaned_query, limit=safe_limit)
        return SearchResponse(
            status="ready" if items else "partial",
            query=cleaned_query,
            items=items,
            limit=safe_limit,
        )

    async def list_playlist(
        self,
        playlist_url: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> PlaylistResponse:
        target = parse_playlist_id(playlist_url)
        offset = decode_cursor(cursor)
        safe_limit = max(1, min(limit, 50))
        title, items, has_more = await self._yt_dlp.list_collection(
            target.canonical_url, offset=offset, limit=safe_limit
        )
        next_cursor = encode_cursor(offset + len(items)) if has_more and items else None
        return PlaylistResponse(
            status="ready",
            playlist_id=target.playlist_id,
            title=title,
            items=items,
            pagination=CursorPaginationMeta(
                cursor=cursor,
                next_cursor=next_cursor,
                limit=safe_limit,
                has_more=has_more,
            ),
        )

    async def list_channel(
        self,
        channel_url: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> ChannelResponse:
        target = parse_channel_ref(channel_url)
        offset = decode_cursor(cursor)
        safe_limit = max(1, min(limit, 50))
        title, items, has_more = await self._yt_dlp.list_collection(
            target.canonical_url, offset=offset, limit=safe_limit
        )
        next_cursor = encode_cursor(offset + len(items)) if has_more and items else None
        return ChannelResponse(
            status="ready",
            channel_ref=target.channel_ref,
            channel_name=title,
            items=items,
            pagination=CursorPaginationMeta(
                cursor=cursor,
                next_cursor=next_cursor,
                limit=safe_limit,
                has_more=has_more,
            ),
        )
