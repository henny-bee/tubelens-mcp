from __future__ import annotations

import unittest
from pathlib import Path

from youtube_mcp.services.transcript_normalizer import (
    normalize_segments,
    paginate_segments,
    parse_subtitle_content,
    search_segments,
    window_segments,
)


class TestTranscriptNormalizer(unittest.TestCase):
    def test_parse_vtt_and_dedupe(self) -> None:
        vtt_path = Path(__file__).resolve().parents[1] / "fixtures" / "sample_captions.vtt"
        vtt_text = vtt_path.read_text(encoding="utf-8")
        segments = parse_subtitle_content(vtt_text, fmt="vtt")

        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0].text, "Welcome to the CPU optimization guide.")
        self.assertEqual(segments[0].start, 0.0)
        self.assertEqual(segments[0].end, 2.5)
        self.assertEqual(segments[1].text, "Today we configure & benchmark faster-whisper INT8.")

    def test_parse_json3_subtitles(self) -> None:
        json3 = '{"events":[{"tStartMs":1000,"dDurationMs":2000,"segs":[{"utf8":"Hello "},{"utf8":"world"}]}]}'
        segments = parse_subtitle_content(json3, fmt="json3")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start, 1.0)
        self.assertEqual(segments[0].end, 3.0)
        self.assertEqual(segments[0].text, "Hello world")

    def test_pagination_and_char_cap(self) -> None:
        raw = [
            {"start": float(i * 2), "end": float(i * 2 + 1.5), "text": f"Segment number {i} content"}
            for i in range(10)
        ]
        segments = normalize_segments(raw)

        page1, meta1 = paginate_segments(segments, offset=0, limit=4, max_chars=12000)
        self.assertEqual(len(page1), 4)
        self.assertTrue(meta1.has_more)
        self.assertEqual(meta1.total, 10)

        page_char_capped, meta_capped = paginate_segments(segments, offset=0, limit=10, max_chars=55)
        self.assertEqual(len(page_char_capped), 2)
        self.assertTrue(meta_capped.has_more)

    def test_window_and_search(self) -> None:
        raw = [
            {"start": 0.0, "end": 5.0, "text": "Introduction to video"},
            {"start": 10.0, "end": 15.0, "text": "Deep dive into CPU INT8 quantization"},
            {"start": 20.0, "end": 25.0, "text": "Conclusion and benchmarks"},
        ]
        segments = normalize_segments(raw)

        win, truncated = window_segments(segments, start_sec=8.0, end_sec=16.0, max_chars=1000)
        self.assertFalse(truncated)
        self.assertEqual(len(win), 1)
        self.assertIn("CPU INT8", win[0].text)

        matches = search_segments(segments, query="int8", limit=5)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].segment_index, 1)


if __name__ == "__main__":
    unittest.main()
