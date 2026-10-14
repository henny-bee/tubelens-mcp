from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

from youtube_mcp.config import Settings
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.job import ArtifactMeta
from youtube_mcp.storage.cache import CacheRepository
from youtube_mcp.storage.db import Database

ALLOWED_ARTIFACT_MIMES = frozenset(
    {
        "audio/mpeg",
        "audio/mp4",
        "audio/wav",
        "audio/x-wav",
        "audio/webm",
        "video/mp4",
        "video/webm",
    }
)

ARTIFACT_ID_PATTERN = re.compile(r"^art_[a-f0-9]{16,32}$")


class ArtifactRepository:
    def __init__(self, db: Database, settings: Settings, cache_repo: CacheRepository) -> None:
        self._db = db
        self._settings = settings
        self._cache_repo = cache_repo

    def register_file(
        self,
        *,
        job_id: str,
        source_file: Path,
        mime: str,
        extension: str,
        ttl_seconds: int | None = None,
    ) -> ArtifactMeta:
        if mime not in ALLOWED_ARTIFACT_MIMES:
            raise AppError(
                ErrorCode.FORBIDDEN,
                f"Output MIME '{mime}' is not in the artifact allowlist.",
            )
        if not source_file.is_file() or source_file.is_symlink():
            raise AppError(ErrorCode.INTERNAL_ERROR, "Invalid artifact source file.")

        size_bytes = source_file.stat().st_size
        self._cache_repo.enforce_storage_budget(size_bytes)

        artifact_id = f"art_{uuid.uuid4().hex[:20]}"
        safe_ext = re.sub(r"[^a-zA-Z0-9]", "", extension)[:8] or "bin"
        artifacts_root = self._settings.artifacts_dir.resolve()
        artifacts_root.mkdir(parents=True, exist_ok=True)

        dest_path = (artifacts_root / f"{artifact_id}.{safe_ext}").resolve()
        if dest_path.parent != artifacts_root:
            raise AppError(ErrorCode.FORBIDDEN, "Artifact path traversal rejected.")

        os.replace(source_file, dest_path)

        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self._settings.artifact_ttl_seconds
        expires_at = now + ttl

        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (id, job_id, path, mime, size_bytes, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (artifact_id, job_id, str(dest_path), mime, size_bytes, now, expires_at),
            )

        return ArtifactMeta(
            id=artifact_id,
            job_id=job_id,
            path=str(dest_path),
            mime=mime,
            size_bytes=size_bytes,
            created_at=now,
            expires_at=expires_at,
        )

    def get(self, *, artifact_id: str | None = None, job_id: str | None = None) -> ArtifactMeta:
        if artifact_id:
            if not ARTIFACT_ID_PATTERN.match(artifact_id):
                raise AppError(ErrorCode.INVALID_ID, "Malformed artifact_id.")
            row = self._db.fetch_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        elif job_id:
            row = self._db.fetch_one(
                "SELECT * FROM artifacts WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
                (job_id,),
            )
        else:
            raise AppError(ErrorCode.INVALID_ID, "Either artifact_id or job_id must be provided.")

        if row is None:
            raise AppError(ErrorCode.JOB_NOT_FOUND, "Artifact not found or has expired.")

        now = time.time()
        file_path = Path(row["path"]).resolve()
        artifacts_root = self._settings.artifacts_dir.resolve()

        if (
            float(row["expires_at"]) <= now
            or not file_path.is_file()
            or file_path.is_symlink()
            or file_path.parent != artifacts_root
        ):
            self.delete(str(row["id"]))
            raise AppError(ErrorCode.JOB_NOT_FOUND, "Artifact has expired and was removed.")

        return ArtifactMeta(
            id=str(row["id"]),
            job_id=str(row["job_id"]),
            path=str(file_path),
            mime=str(row["mime"]),
            size_bytes=int(row["size_bytes"]),
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
        )

    def delete(self, artifact_id: str) -> None:
        row = self._db.fetch_one("SELECT path FROM artifacts WHERE id = ?", (artifact_id,))
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
        if row:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError:
                pass

    def purge_expired(self, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        rows = self._db.fetch_all(
            "SELECT id, path FROM artifacts WHERE expires_at <= ?",
            (ts,),
        )
        if not rows:
            return 0
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM artifacts WHERE expires_at <= ?", (ts,))
        for row in rows:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError:
                pass
        return len(rows)
