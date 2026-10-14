from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from youtube_mcp.config import Settings
from youtube_mcp.jobs.persistence import JobRepository, compute_job_key
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.job import JobKind, JobStatus
from youtube_mcp.storage.artifacts import ArtifactRepository
from youtube_mcp.storage.cache import CacheRepository, compute_cache_key
from youtube_mcp.storage.db import Database
from youtube_mcp.storage.migrations import apply_migrations


def make_test_settings(tmp_dir: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "MCP_BEARER_TOKEN": "test_secret_token_1234567890",
        "DATA_DIR": tmp_dir,
        "MAX_QUEUE_SIZE": 2,
        "MAX_STORAGE_BYTES": 10_485_760,
    }
    defaults.update(overrides)
    settings = Settings(**defaults)  # type: ignore[arg-type]
    settings.ensure_directories()
    return settings


class TestCacheAndJobs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.settings = make_test_settings(self.root)
        self.db = Database(self.settings.db_path)
        apply_migrations(self.db)
        self.cache = CacheRepository(self.db, self.settings)
        self.artifacts = ArtifactRepository(self.db, self.settings, self.cache)
        self.jobs = JobRepository(self.db, self.settings)

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def test_cache_put_get_and_ttl_expiry(self) -> None:
        key = compute_cache_key(provider="captions", video_id="dQw4w9WgXcQ", language="en")
        self.cache.put(
            key=key,
            kind="captions",
            video_id="dQw4w9WgXcQ",
            payload={"hello": "world"},
            language="en",
            ttl_seconds=60,
        )
        loaded = self.cache.get(key)
        self.assertEqual(loaded, {"hello": "world"})

        expired_key = compute_cache_key(provider="captions", video_id="dQw4w9WgXcQ", language="id")
        self.cache.put(
            key=expired_key,
            kind="captions",
            video_id="dQw4w9WgXcQ",
            payload={"expired": True},
            language="id",
            ttl_seconds=-10,
        )
        self.assertIsNone(self.cache.get(expired_key))

    def test_storage_budget_enforced(self) -> None:
        small_settings = make_test_settings(self.root, MAX_STORAGE_BYTES=10_485_760)
        small_settings.max_storage_bytes = 50
        small_cache = CacheRepository(self.db, small_settings)
        with self.assertRaises(AppError) as ctx:
            small_cache.put(
                key="k1",
                kind="metadata",
                video_id="dQw4w9WgXcQ",
                payload={"data": "x" * 200},
            )
        self.assertEqual(ctx.exception.code, ErrorCode.STORAGE_FULL)

    def test_job_deduplication_and_queue_overflow(self) -> None:
        k1 = compute_job_key(video_id="dQw4w9WgXcQ", kind=JobKind.ASR_TRANSCRIBE, language="en")
        j1, created1 = self.jobs.enqueue_or_get(
            principal_id="p1",
            kind=JobKind.ASR_TRANSCRIBE,
            video_id="dQw4w9WgXcQ",
            job_key=k1,
        )
        self.assertTrue(created1)

        j1_dup, created_dup = self.jobs.enqueue_or_get(
            principal_id="p1",
            kind=JobKind.ASR_TRANSCRIBE,
            video_id="dQw4w9WgXcQ",
            job_key=k1,
        )
        self.assertFalse(created_dup)
        self.assertEqual(j1.id, j1_dup.id)

        k2 = compute_job_key(video_id="1234567890a", kind=JobKind.ASR_TRANSCRIBE, language="en")
        _, created2 = self.jobs.enqueue_or_get(
            principal_id="p1",
            kind=JobKind.ASR_TRANSCRIBE,
            video_id="1234567890a",
            job_key=k2,
        )
        self.assertTrue(created2)

        k3 = compute_job_key(video_id="1234567890b", kind=JobKind.ASR_TRANSCRIBE, language="en")
        with self.assertRaises(AppError) as ctx:
            self.jobs.enqueue_or_get(
                principal_id="p1",
                kind=JobKind.ASR_TRANSCRIBE,
                video_id="1234567890b",
                job_key=k3,
            )
        self.assertEqual(ctx.exception.code, ErrorCode.RESOURCE_LIMIT)

    def test_restart_persistence_and_reconciliation(self) -> None:
        k1 = compute_job_key(video_id="dQw4w9WgXcQ", kind=JobKind.ASR_TRANSCRIBE, language="en")
        job, _ = self.jobs.enqueue_or_get(
            principal_id="p1",
            kind=JobKind.ASR_TRANSCRIBE,
            video_id="dQw4w9WgXcQ",
            job_key=k1,
        )
        claimed = self.jobs.claim_next_queued()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, JobStatus.RUNNING)

        self.db.close()
        reopened_db = Database(self.settings.db_path)
        reopened_jobs = JobRepository(reopened_db, self.settings)
        try:
            stats = reopened_jobs.reconcile_on_startup(auto_requeue_idempotent=True)
            self.assertEqual(stats["requeued"], 1)
            reconciled = reopened_jobs.get(job.id)
            self.assertEqual(reconciled.status, JobStatus.QUEUED)
        finally:
            reopened_db.close()

    def test_artifact_ttl_expiry(self) -> None:
        src_file = self.settings.tmp_dir / "sample.m4a"
        src_file.write_bytes(b"fake_audio_bytes")
        meta = self.artifacts.register_file(
            job_id="job_123",
            source_file=src_file,
            mime="audio/mp4",
            extension="m4a",
            ttl_seconds=1,
        )
        fetched = self.artifacts.get(artifact_id=meta.id)
        self.assertEqual(fetched.id, meta.id)

        self.artifacts.purge_expired(now=time.time() + 10.0)
        with self.assertRaises(AppError) as ctx:
            self.artifacts.get(artifact_id=meta.id)
        self.assertEqual(ctx.exception.code, ErrorCode.JOB_NOT_FOUND)
        self.assertFalse(Path(meta.path).exists())

    def test_online_sqlite_backup_and_integrity(self) -> None:
        from scripts.db_backup import backup_sqlite_database

        key = compute_cache_key(provider="captions", video_id="dQw4w9WgXcQ", language="en")
        self.cache.put(
            key=key,
            kind="captions",
            video_id="dQw4w9WgXcQ",
            payload={"backup": "verified"},
            language="en",
        )
        backup_file = self.root / "backups" / "app_backup.sqlite3"
        report = backup_sqlite_database(self.settings.db_path, backup_file)
        self.assertTrue(report["ok"])
        self.assertEqual(report["integrity_check"], "ok")
        self.assertTrue(backup_file.is_file())


if __name__ == "__main__":
    unittest.main()
