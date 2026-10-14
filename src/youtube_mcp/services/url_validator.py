from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from youtube_mcp.schemas.common import AppError, ErrorCode

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
PLAYLIST_ID_RE = re.compile(r"^(PL|UU|LL|FL|OLAK5uy_|RD)[A-Za-z0-9_-]{10,64}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
CHANNEL_HANDLE_RE = re.compile(r"^@[A-Za-z0-9_.-]{3,40}$")
CHANNEL_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]{3,60}$")

ALLOWED_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)

MAX_INPUT_LEN = 512
MAX_QUERY_LEN = 200


@dataclass(frozen=True)
class CanonicalVideoTarget:
    video_id: str
    canonical_url: str


@dataclass(frozen=True)
class CanonicalPlaylistTarget:
    playlist_id: str
    canonical_url: str


@dataclass(frozen=True)
class CanonicalChannelTarget:
    channel_ref: str
    canonical_url: str


def _reject_unsafe_url(raw: str) -> None:
    if not raw or not isinstance(raw, str):
        raise AppError(ErrorCode.INVALID_URL, "Input URL or identifier cannot be empty.")
    stripped = raw.strip()
    if len(stripped) > MAX_INPUT_LEN:
        raise AppError(ErrorCode.INVALID_URL, "Input exceeds maximum allowed length.")
    if any(ch in stripped for ch in ("\x00", "\r", "\n", "\t", " ")):
        raise AppError(ErrorCode.INVALID_URL, "Input contains illegal control or whitespace characters.")
    if stripped.startswith("-"):
        raise AppError(ErrorCode.INVALID_ID, "Identifier cannot start with a dash.")


def _parse_and_validate_https_youtube_url(raw: str) -> tuple[str, str, dict[str, list[str]]]:
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise AppError(
            ErrorCode.INVALID_URL,
            f"Unsupported URL scheme '{scheme or 'none'}'; only https:// YouTube URLs are permitted.",
        )
    if parsed.username or parsed.password:
        raise AppError(ErrorCode.INVALID_URL, "Embedded credentials in URLs are forbidden.")
    if parsed.port not in (None, 443):
        raise AppError(ErrorCode.INVALID_URL, "Non-standard ports in URLs are forbidden.")

    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise AppError(ErrorCode.INVALID_URL, "URL is missing a hostname.")

    try:
        ip = ipaddress.ip_address(hostname)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise AppError(ErrorCode.INVALID_URL, "Private, loopback, or internal IP addresses are forbidden.")
        raise AppError(ErrorCode.INVALID_URL, "Direct IP literal URLs are not allowed.")
    except ValueError:
        pass

    if hostname in ("localhost", "broadcasthost") or hostname.endswith((".local", ".internal", ".lan")):
        raise AppError(ErrorCode.INVALID_URL, "Local or internal hostnames are forbidden.")

    if hostname not in ALLOWED_YOUTUBE_HOSTS:
        raise AppError(
            ErrorCode.INVALID_URL,
            f"Host '{hostname}' is not in the YouTube allowlist.",
        )

    decoded_path = unquote(parsed.path or "")
    if ".." in decoded_path or "\\" in decoded_path or "@" in decoded_path.split("/")[0]:
        raise AppError(ErrorCode.INVALID_URL, "Path traversal or ambiguous path segments are forbidden.")

    query = parse_qs(parsed.query, keep_blank_values=False)
    return hostname, parsed.path or "", query


def parse_video_id(url_or_id: str) -> CanonicalVideoTarget:
    _reject_unsafe_url(url_or_id)
    candidate = url_or_id.strip()

    if "://" not in candidate and "/" not in candidate and "." not in candidate:
        if not VIDEO_ID_RE.match(candidate):
            raise AppError(
                ErrorCode.INVALID_ID,
                "Invalid YouTube video ID; expected an 11-character video ID.",
            )
        return CanonicalVideoTarget(
            video_id=candidate,
            canonical_url=f"https://www.youtube.com/watch?v={candidate}",
        )

    hostname, path, query = _parse_and_validate_https_youtube_url(candidate)
    video_id: str | None = None

    if hostname == "youtu.be":
        parts = [p for p in path.split("/") if p]
        if len(parts) == 1:
            video_id = parts[0]
    else:
        parts = [p for p in path.split("/") if p]
        if path == "/watch":
            v_list = query.get("v", [])
            if len(v_list) == 1:
                video_id = v_list[0]
        elif len(parts) == 2 and parts[0] in ("shorts", "embed", "live", "v"):
            video_id = parts[1]

    if not video_id or not VIDEO_ID_RE.match(video_id):
        raise AppError(
            ErrorCode.INVALID_ID,
            "Could not extract a valid 11-character YouTube video ID from URL.",
        )

    return CanonicalVideoTarget(
        video_id=video_id,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def parse_playlist_id(playlist_url_or_id: str) -> CanonicalPlaylistTarget:
    _reject_unsafe_url(playlist_url_or_id)
    candidate = playlist_url_or_id.strip()

    if "://" not in candidate and "/" not in candidate and "." not in candidate:
        if not PLAYLIST_ID_RE.match(candidate):
            raise AppError(ErrorCode.INVALID_ID, "Invalid YouTube playlist ID.")
        return CanonicalPlaylistTarget(
            playlist_id=candidate,
            canonical_url=f"https://www.youtube.com/playlist?list={candidate}",
        )

    hostname, path, query = _parse_and_validate_https_youtube_url(candidate)
    if hostname == "youtu.be" or path not in ("/playlist", "/watch"):
        raise AppError(ErrorCode.INVALID_URL, "Playlist URL must use /playlist?list= or /watch?list=.")

    list_vals = query.get("list", [])
    if len(list_vals) != 1 or not PLAYLIST_ID_RE.match(list_vals[0]):
        raise AppError(ErrorCode.INVALID_ID, "Missing or invalid YouTube playlist ID in URL.")

    playlist_id = list_vals[0]
    return CanonicalPlaylistTarget(
        playlist_id=playlist_id,
        canonical_url=f"https://www.youtube.com/playlist?list={playlist_id}",
    )


def parse_channel_ref(channel_url_or_ref: str) -> CanonicalChannelTarget:
    _reject_unsafe_url(channel_url_or_ref)
    candidate = channel_url_or_ref.strip()

    if "://" not in candidate and "/" not in candidate:
        if CHANNEL_ID_RE.match(candidate):
            return CanonicalChannelTarget(
                channel_ref=candidate,
                canonical_url=f"https://www.youtube.com/channel/{candidate}/videos",
            )
        if CHANNEL_HANDLE_RE.match(candidate):
            return CanonicalChannelTarget(
                channel_ref=candidate,
                canonical_url=f"https://www.youtube.com/{candidate}/videos",
            )
        raise AppError(
            ErrorCode.INVALID_ID,
            "Invalid YouTube channel identifier; expected a UC... channel ID, @handle, or https://www.youtube.com channel URL.",
        )

    hostname, path, _ = _parse_and_validate_https_youtube_url(candidate)
    if hostname == "youtu.be":
        raise AppError(ErrorCode.INVALID_URL, "youtu.be does not host channel pages.")

    parts = [p for p in path.split("/") if p]
    if not parts:
        raise AppError(ErrorCode.INVALID_URL, "Channel URL path is empty.")

    if parts[0].startswith("@") and CHANNEL_HANDLE_RE.match(parts[0]):
        handle = parts[0]
        return CanonicalChannelTarget(
            channel_ref=handle,
            canonical_url=f"https://www.youtube.com/{handle}/videos",
        )

    if len(parts) >= 2 and parts[0] == "channel" and CHANNEL_ID_RE.match(parts[1]):
        cid = parts[1]
        return CanonicalChannelTarget(
            channel_ref=cid,
            canonical_url=f"https://www.youtube.com/channel/{cid}/videos",
        )

    if len(parts) >= 2 and parts[0] in ("c", "user") and CHANNEL_SLUG_RE.match(parts[1]):
        kind, slug = parts[0], parts[1]
        return CanonicalChannelTarget(
            channel_ref=f"{kind}:{slug}",
            canonical_url=f"https://www.youtube.com/{kind}/{slug}/videos",
        )

    raise AppError(ErrorCode.INVALID_URL, "Unsupported or malformed YouTube channel URL.")


def validate_search_query(query: str) -> str:
    if not query or not isinstance(query, str):
        raise AppError(ErrorCode.INVALID_ID, "Search query cannot be empty.")
    cleaned = " ".join(query.strip().split())
    if not cleaned:
        raise AppError(ErrorCode.INVALID_ID, "Search query cannot be whitespace only.")
    if len(cleaned) > MAX_QUERY_LEN:
        raise AppError(
            ErrorCode.INVALID_ID,
            f"Search query exceeds maximum length of {MAX_QUERY_LEN} characters.",
        )
    if cleaned.startswith("-") or "://" in cleaned or "\x00" in cleaned:
        raise AppError(ErrorCode.INVALID_URL, "Search query must not be a URL or CLI flag.")
    return cleaned
