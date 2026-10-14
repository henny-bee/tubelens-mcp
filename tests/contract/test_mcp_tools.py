from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from youtube_mcp.config import Settings
from youtube_mcp.mcp_server import build_runtime
from youtube_mcp.security.auth import Principal, set_current_principal
from youtube_mcp.services.yt_dlp_adapter import YtDlpAdapter
from tests.integration.test_pipeline_and_asr import (
    FakeAsrRunner,
    FakeFfmpegService,
    FakeTranscriptApiAdapter,
    make_fake_yt_dlp_runner,
)

EXPECTED_TOOLS = {
    "youtube_get_video_info",
    "youtube_get_transcript",
    "youtube_get_transcript_window",
    "youtube_search_transcript",
    "youtube_list_captions",
    "youtube_get_job_status",
    "youtube_cancel_job",
    "youtube_search_videos",
    "youtube_list_playlist",
    "youtube_list_channel",
    "youtube_get_formats",
    "youtube_get_comments",
    "youtube_download_media",
    "youtube_get_artifact",
}


class TestMcpToolContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.yt_state: dict[str, Any] = {"calls": 0, "duration": 180}
        self.settings = Settings(
            MCP_BEARER_TOKEN="test_secret_token_1234567890",
            MCP_DEFAULT_SCOPES=["read", "media:download", "admin:jobs"],
            DATA_DIR=self.root,
            ENABLE_MEDIA_DOWNLOAD=False,
        )
        self.runtime = build_runtime(
            self.settings,
            yt_dlp=YtDlpAdapter(self.settings, runner=make_fake_yt_dlp_runner(self.yt_state)),
            transcript_api=FakeTranscriptApiAdapter(self.settings),
            ffmpeg=FakeFfmpegService(self.settings),
            asr_runner=FakeAsrRunner(self.settings),
        )
        set_current_principal(
            Principal(
                principal_id="p_contract",
                scopes=frozenset({"read", "media:download", "admin:jobs"}),
            )
        )

    async def asyncTearDown(self) -> None:
        self.runtime.db.close()
        self._tmp.cleanup()

    async def test_all_14_tools_registered(self) -> None:
        self.assertEqual(set(self.runtime.tools.keys()), EXPECTED_TOOLS)

    async def test_p0_and_p1_tool_contracts(self) -> None:
        captions = await self.runtime.tools["youtube_list_captions"]("dQw4w9WgXcQ")
        self.assertEqual(captions["status"], "ready")
        self.assertEqual(len(captions["tracks"]), 1)

        win = await self.runtime.tools["youtube_get_transcript_window"](
            "dQw4w9WgXcQ", start_sec=0.0, end_sec=3.0
        )
        self.assertEqual(win["status"], "ready")
        self.assertEqual(len(win["segments"]), 2)

        srch_tr = await self.runtime.tools["youtube_search_transcript"](
            "dQw4w9WgXcQ", query="manual"
        )
        self.assertEqual(srch_tr["status"], "ready")
        self.assertEqual(len(srch_tr["matches"]), 1)

        srch_vid = await self.runtime.tools["youtube_search_videos"]("mcp server", limit=5)
        self.assertEqual(srch_vid["status"], "ready")
        self.assertEqual(len(srch_vid["items"]), 1)

        pl = await self.runtime.tools["youtube_list_playlist"](
            "https://www.youtube.com/playlist?list=PL1234567890abcdefghij", limit=10
        )
        self.assertEqual(pl["status"], "ready")
        self.assertEqual(len(pl["items"]), 2)

        ch = await self.runtime.tools["youtube_list_channel"]("@SampleChannel", limit=10)
        self.assertEqual(ch["status"], "ready")
        self.assertEqual(len(ch["items"]), 2)

        fmts = await self.runtime.tools["youtube_get_formats"]("dQw4w9WgXcQ")
        self.assertEqual(fmts["status"], "ready")
        self.assertEqual(fmts["formats"][0]["format_id"], "140")

        comments = await self.runtime.tools["youtube_get_comments"]("dQw4w9WgXcQ", limit=10)
        self.assertIn(comments["status"], ("ready", "partial"))
        self.assertEqual(len(comments["comments"]), 1)
        self.assertTrue(comments["comments"][0]["untrusted_content"])

    async def test_t11_media_download_disabled_by_default_and_authorized_flow(self) -> None:
        denied = await self.runtime.tools["youtube_download_media"](
            "dQw4w9WgXcQ", kind="audio", format="m4a", confirm_authorized=True
        )
        self.assertEqual(denied["status"], "error")
        self.assertEqual(denied["code"], "FORBIDDEN")

        self.settings.enable_media_download = True
        no_confirm = await self.runtime.tools["youtube_download_media"](
            "dQw4w9WgXcQ", kind="audio", format="m4a", confirm_authorized=False
        )
        self.assertEqual(no_confirm["status"], "error")
        self.assertEqual(no_confirm["code"], "FORBIDDEN")

        scheduled = await self.runtime.tools["youtube_download_media"](
            "dQw4w9WgXcQ", kind="audio", format="m4a", confirm_authorized=True
        )
        self.assertEqual(scheduled["status"], "pending")
        job_id = scheduled["job_id"]

        claimed = self.runtime.job_repo.claim_next_queued()
        self.assertIsNotNone(claimed)
        done = await self.runtime.scheduler.execute_claimed_job(claimed)
        self.assertEqual(done.status.value, "succeeded")

        art = await self.runtime.tools["youtube_get_artifact"](job_id=job_id)
        self.assertEqual(art["status"], "ready")
        self.assertTrue(art["download_url"].endswith(f"/artifacts/{art['artifact_id']}"))


if __name__ == "__main__":
    unittest.main()
