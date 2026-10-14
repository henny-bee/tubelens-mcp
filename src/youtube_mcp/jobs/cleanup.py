from __future__ import annotations

import shutil
from pathlib import Path

from youtube_mcp.config import Settings
from youtube_mcp.jobs.persistence import JobRepository
from youtube_mcp.storage.artifacts import ArtifactRepository
from youtube_mcp.storage.cache import CacheRepository


class CleanupManager:
    def __init__(
        self,
        settings: Settings,
        cache_repo: CacheRepository,
        artifact_repo: ArtifactRepository,
        job_repo: JobRepository,
    ) -> None:
        self._settings = settings
        self._cache_repo = cache_repo
        self._artifact_repo = artifact_repo
        self._job_repo = job_repo

    def clean_job_workspace(self, job_id: str) -> None:
        workspace = (self._settings.tmp_dir / f"job_{job_id}").resolve()
        if workspace.parent == self._settings.tmp_dir.resolve() and workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)

    def clean_all_tmp(self) -> int:
        removed = 0
        tmp_root = self._settings.tmp_dir
        if not tmp_root.exists():
            return 0
        for child in tmp_root.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        return removed

    def run_maintenance(self, *, clean_tmp: bool = False) -> dict[str, int]:
        stats = {
            "expired_cache": self._cache_repo.purge_expired(),
            "expired_artifacts": self._artifact_repo.purge_expired(),
            "expired_jobs": self._job_repo.purge_expired(),
            "tmp_entries": self.clean_all_tmp() if clean_tmp else 0,
        }
        return stats
