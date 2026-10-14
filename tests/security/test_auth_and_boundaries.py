from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import httpx

from youtube_mcp.config import Settings, redact_text
from youtube_mcp.mcp_server import build_runtime, create_asgi_app
from youtube_mcp.services.yt_dlp_adapter import YtDlpAdapter
from tests.integration.test_pipeline_and_asr import (
    FakeAsrRunner,
    FakeFfmpegService,
    FakeTranscriptApiAdapter,
    make_fake_yt_dlp_runner,
)


class TestSecurityAndHttpTransport(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.yt_state: dict[str, Any] = {"calls": 0, "duration": 180}
        self.token = "super_secret_bearer_token_9876543210"
        self.readonly_token = "readonly_secret_bearer_token_111111"
        self.settings = Settings(
            MCP_BEARER_TOKEN=self.token,
            MCP_DEFAULT_SCOPES=["read", "media:download", "admin:jobs"],
            MCP_TOKENS_JSON=json.dumps(
                {
                    self.readonly_token: {
                        "principal_id": "p_readonly",
                        "scopes": ["read"],
                    }
                }
            ),
            DATA_DIR=self.root,
            ENABLE_MEDIA_DOWNLOAD=True,
            RATE_LIMIT_REQUESTS_PER_MINUTE=5,
        )
        self.runtime = build_runtime(
            self.settings,
            yt_dlp=YtDlpAdapter(self.settings, runner=make_fake_yt_dlp_runner(self.yt_state)),
            transcript_api=FakeTranscriptApiAdapter(self.settings),
            ffmpeg=FakeFfmpegService(self.settings),
            asr_runner=FakeAsrRunner(self.settings),
        )
        self.app = create_asgi_app(self.runtime)

    async def asyncTearDown(self) -> None:
        await self.runtime.scheduler.stop()
        self.runtime.db.close()
        self._tmp.cleanup()

    def test_placeholder_secret_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Settings(MCP_BEARER_TOKEN="REPLACE_WITH_RANDOM_SECRET")
        with self.assertRaises(ValueError):
            Settings(MCP_BEARER_TOKEN="short")

    def test_log_secret_redaction(self) -> None:
        raw = f"Authorization: Bearer {self.token} and MCP_BEARER_TOKEN={self.token}"
        cleaned = redact_text(raw, extra_secrets=[self.token])
        self.assertNotIn(self.token, cleaned)
        self.assertIn("[REDACTED]", cleaned)

    async def test_t10_ac01_ac02_ac15_http_auth_and_mcp_e2e(self) -> None:
        async with self.runtime.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8000"
            ) as client:
                health = await client.get("/healthz")
                self.assertEqual(health.status_code, 200)

                unauth = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual(unauth.status_code, 401)

                bad_tok = await client.post(
                    "/mcp",
                    headers={"Authorization": "Bearer wrong_token"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                self.assertEqual(bad_tok.status_code, 401)

                qs_tok = await client.post(
                    f"/mcp?token={self.token}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                self.assertEqual(qs_tok.status_code, 401)

                ready = await client.get(
                    "/readyz",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
                self.assertEqual(ready.status_code, 200)
                self.assertEqual(ready.json()["status"], "ready")

                mcp_headers = {
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/json, text/event-stream",
                }
                init_resp = await client.post(
                    "/mcp",
                    headers=mcp_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "e2e-test-client", "version": "1.0.0"},
                        },
                    },
                )
                self.assertEqual(init_resp.status_code, 200)

                tools_resp = await client.post(
                    "/mcp",
                    headers=mcp_headers,
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                )
                self.assertEqual(tools_resp.status_code, 200)
                tools_data = tools_resp.json()
                tool_names = {t["name"] for t in tools_data["result"]["tools"]}
                self.assertIn("youtube_get_video_info", tool_names)
                self.assertIn("youtube_get_transcript", tool_names)

                call_resp = await client.post(
                    "/mcp",
                    headers=mcp_headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "youtube_get_video_info",
                            "arguments": {"url_or_id": "dQw4w9WgXcQ"},
                        },
                    },
                )
                self.assertEqual(call_resp.status_code, 200)
                call_json = call_resp.json()
                self.assertFalse(call_json["result"].get("isError", False))

    async def test_artifact_route_authorization_and_traversal_guard(self) -> None:
        src = self.settings.tmp_dir / "clip.m4a"
        src.write_bytes(b"authorized_media_payload")
        meta = self.runtime.artifact_repo.register_file(
            job_id="job_sec",
            source_file=src,
            mime="audio/mp4",
            extension="m4a",
        )

        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as client:
            unauth = await client.get(f"/artifacts/{meta.id}")
            self.assertEqual(unauth.status_code, 401)

            forbidden = await client.get(
                f"/artifacts/{meta.id}",
                headers={"Authorization": f"Bearer {self.readonly_token}"},
            )
            self.assertEqual(forbidden.status_code, 403)

            traversal = await client.get(
                "/artifacts/..%2Fstate%2Fapp.sqlite3",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            self.assertIn(traversal.status_code, (400, 404))

            ok = await client.get(
                f"/artifacts/{meta.id}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.content, b"authorized_media_payload")


if __name__ == "__main__":
    unittest.main()
