from __future__ import annotations

import html
import json
import re
from typing import Any

from youtube_mcp.schemas.common import PaginationMeta
from youtube_mcp.schemas.transcript import (
    TranscriptSearchMatch,
    TranscriptSegment,
)

TAG_RE = re.compile(r"<[^>]+>")
VTT_TIMESTAMP_RE = re.compile(
    r"^(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})"
)
WHITESPACE_RE = re.compile(r"\s+")


def clean_caption_text(raw: str) -> str:
    if not raw:
        return ""
    unescaped = html.unescape(raw)
    without_tags = TAG_RE.sub(" ", unescaped)
    cleaned = WHITESPACE_RE.sub(" ", without_tags).strip()
    return cleaned


def normalize_segments(raw_segments: list[dict[str, Any]]) -> list[TranscriptSegment]:
    cleaned_items: list[tuple[ float, float, str, bool ]] = []
    for item in raw_segments:
        text = clean_caption_text(str(item.get("text", "")))
        if not text:
            continue
        start = max(0.0, round(float(item.get("start", 0.0)), 3))
        if "end" in item and item["end"] is not None:
            raw_end = round(float(item["end"]), 3)
        else:
            duration = max(0.0, float(item.get("duration", 0.0)))
            raw_end = round(start + duration, 3)
        zero_len = raw_end <= start
        end = round(start + 0.5, 3) if zero_len else raw_end
        cleaned_items.append((start, end, text, zero_len))

    cleaned_items.sort(key=lambda s: (s[0], s[1]))

    deduped: list[TranscriptSegment] = []
    for start, end, text, zero_len in cleaned_items:
        if deduped:
            prev = deduped[-1]
            if prev.text == text and zero_len and start <= prev.end + 0.05:
                continue
            if prev.text == text and abs(start - prev.start) < 0.2:
                if end > prev.end and not zero_len:
                    deduped[-1] = TranscriptSegment(start=prev.start, end=end, text=prev.text)
                continue
            if prev.text == text and start <= prev.end + 0.05:
                deduped[-1] = TranscriptSegment(
                    start=prev.start, end=max(prev.end, end), text=prev.text
                )
                continue
        deduped.append(TranscriptSegment(start=start, end=end, text=text))
    return deduped


def _parse_time_parts(hours: str | None, minutes: str, seconds: str, millis: str) -> float:
    h = int(hours) if hours else 0
    return h * 3600.0 + int(minutes) * 60.0 + int(seconds) + int(millis) / 1000.0


def parse_subtitle_content(content: str, fmt: str = "vtt") -> list[TranscriptSegment]:
    if not content or not content.strip():
        return []

    stripped = content.strip()
    if fmt == "json3" or stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            events = data.get("events", [])
            raw_list: list[dict[str, Any]] = []
            for ev in events:
                segs = ev.get("segs")
                if not segs:
                    continue
                text = "".join(str(s.get("utf8", "")) for s in segs)
                start_ms = float(ev.get("tStartMs", 0.0))
                dur_ms = float(ev.get("dDurationMs", 1000.0))
                raw_list.append(
                    {
                        "start": start_ms / 1000.0,
                        "end": (start_ms + dur_ms) / 1000.0,
                        "text": text,
                    }
                )
            return normalize_segments(raw_list)
        except Exception:
            pass

    lines = stripped.splitlines()
    raw_segments: list[dict[str, Any]] = []
    current_start: float | None = None
    current_end: float | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_start, current_end, current_lines
        if current_start is not None and current_end is not None and current_lines:
            raw_segments.append(
                {
                    "start": current_start,
                    "end": current_end,
                    "text": " ".join(current_lines),
                }
            )
        current_start = None
        current_end = None
        current_lines = []

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if line.startswith(("WEBVTT", "NOTE", "STYLE", "Kind:", "Language:")):
            continue
        if line.isdigit() and current_start is None:
            continue

        match = VTT_TIMESTAMP_RE.match(line)
        if match:
            flush()
            h1, m1, s1, ms1, h2, m2, s2, ms2 = match.groups()
            current_start = _parse_time_parts(h1, m1, s1, ms1)
            current_end = _parse_time_parts(h2, m2, s2, ms2)
        elif current_start is not None:
            current_lines.append(line)

    flush()
    return normalize_segments(raw_segments)


def paginate_segments(
    segments: list[TranscriptSegment],
    *,
    offset: int = 0,
    limit: int = 500,
    max_chars: int = 12_000,
) -> tuple[list[TranscriptSegment], PaginationMeta]:
    safe_offset = max(0, offset)
    safe_limit = max(1, min(limit, 500))
    total = len(segments)

    if safe_offset >= total:
        return [], PaginationMeta(offset=safe_offset, limit=safe_limit, total=total, has_more=False)

    sliced: list[TranscriptSegment] = []
    char_count = 0
    idx = safe_offset
    while idx < total and len(sliced) < safe_limit:
        seg = segments[idx]
        seg_len = len(seg.text)
        if sliced and char_count + seg_len > max_chars:
            break
        if not sliced and seg_len > max_chars:
            sliced.append(
                TranscriptSegment(start=seg.start, end=seg.end, text=seg.text[:max_chars])
            )
            idx += 1
            break
        sliced.append(seg)
        char_count += seg_len
        idx += 1

    has_more = idx < total
    return sliced, PaginationMeta(
        offset=safe_offset,
        limit=len(sliced) or safe_limit,
        total=total,
        has_more=has_more,
    )


def window_segments(
    segments: list[TranscriptSegment],
    *,
    start_sec: float,
    end_sec: float,
    max_chars: int = 12_000,
) -> tuple[list[TranscriptSegment], bool]:
    selected: list[TranscriptSegment] = []
    char_count = 0
    truncated = False

    for seg in segments:
        if seg.end < start_sec:
            continue
        if seg.start > end_sec:
            break
        if selected and char_count + len(seg.text) > max_chars:
            truncated = True
            break
        if not selected and len(seg.text) > max_chars:
            selected.append(
                TranscriptSegment(start=seg.start, end=seg.end, text=seg.text[:max_chars])
            )
            truncated = True
            break
        selected.append(seg)
        char_count += len(seg.text)

    return selected, truncated


def search_segments(
    segments: list[TranscriptSegment],
    *,
    query: str,
    limit: int = 20,
) -> list[TranscriptSearchMatch]:
    needle = query.strip().lower()
    if not needle:
        return []
    safe_limit = max(1, min(limit, 100))
    matches: list[TranscriptSearchMatch] = []
    for idx, seg in enumerate(segments):
        if needle in seg.text.lower():
            matches.append(
                TranscriptSearchMatch(
                    start=seg.start,
                    end=seg.end,
                    text=seg.text,
                    segment_index=idx,
                )
            )
            if len(matches) >= safe_limit:
                break
    return matches
