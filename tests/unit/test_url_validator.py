from __future__ import annotations

import unittest

from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.services.url_validator import (
    parse_channel_ref,
    parse_playlist_id,
    parse_video_id,
    validate_search_query,
)


class TestUrlValidator(unittest.TestCase):
    def test_valid_video_ids_and_urls(self) -> None:
        cases = [
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ?si=abcdef",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
        ]
        for raw in cases:
            target = parse_video_id(raw)
            self.assertEqual(target.video_id, "dQw4w9WgXcQ")
            self.assertEqual(
                target.canonical_url,
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            )

    def test_rejects_ssrf_and_malicious_urls(self) -> None:
        malicious = [
            "http://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "file:///etc/passwd",
            "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
            "https://127.0.0.1/watch?v=dQw4w9WgXcQ",
            "https://localhost/watch?v=dQw4w9WgXcQ",
            "https://169.254.169.254/latest/meta-data/",
            "https://192.168.1.1/watch?v=dQw4w9WgXcQ",
            "https://[::1]/watch?v=dQw4w9WgXcQ",
            "https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ",
            "https://evil-youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/../../etc/passwd",
            "-o /tmp/pwn",
        ]
        for raw in malicious:
            with self.assertRaises(AppError) as ctx:
                parse_video_id(raw)
            self.assertIn(
                ctx.exception.code,
                (ErrorCode.INVALID_URL, ErrorCode.INVALID_ID),
                f"Failed to reject {raw}",
            )

    def test_valid_and_invalid_playlists(self) -> None:
        pid = "PL1234567890abcdefghij"
        t1 = parse_playlist_id(pid)
        self.assertEqual(t1.playlist_id, pid)
        t2 = parse_playlist_id(f"https://www.youtube.com/playlist?list={pid}")
        self.assertEqual(t2.playlist_id, pid)

        with self.assertRaises(AppError):
            parse_playlist_id("https://evil.example.com/playlist?list=PL1234567890")
        with self.assertRaises(AppError):
            parse_playlist_id("invalid_playlist")

    def test_valid_and_invalid_channels(self) -> None:
        t1 = parse_channel_ref("@OpenDevelopers")
        self.assertEqual(t1.channel_ref, "@OpenDevelopers")
        self.assertEqual(t1.canonical_url, "https://www.youtube.com/@OpenDevelopers/videos")

        cid = "UC" + "a" * 22
        t2 = parse_channel_ref(f"https://www.youtube.com/channel/{cid}")
        self.assertEqual(t2.channel_ref, cid)

        with self.assertRaises(AppError):
            parse_channel_ref("https://youtu.be/@OpenDevelopers")

    def test_search_query_validation(self) -> None:
        self.assertEqual(validate_search_query("  python   mcp  "), "python mcp")
        with self.assertRaises(AppError):
            validate_search_query("--exec rm")
        with self.assertRaises(AppError):
            validate_search_query("https://evil.com")


if __name__ == "__main__":
    unittest.main()
