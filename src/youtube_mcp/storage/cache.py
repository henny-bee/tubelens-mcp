from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from youtube_mcp.config import Settings
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.storage.db import Database

CacheKind = Literal["metadata", "captions", "asr"]


def compute_cache_key(
    *,
    provider: str,
    video_id: str,
    language: str = "",
    source_policy: str = "default",
    version: str = "1.0",
    extra: str = "",
) -> str:
    raw = f"{provider}|{video_id}|{language}|{source_policy}|{version}|{extra}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class CacheRepository:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings

    def _dir_for_kind(self, kind: CacheKind) -> Path:
        if kind == "metadata":
            return self._settings.metadata_cache_dir
        if kind == "captions":
            return self._settings.captions_cache_dir
        return self._settings.asr_cache_dir

    def _ttl_for_kind(self, kind: CacheKind) -> int:
        if kind == "metadata":
            return self._settings.metadata_cache_ttl_seconds
        if kind == "captions":
            return self._settings.caption_cache_ttl_seconds
        return self._settings.asr_cache_ttl_seconds

    def total_storage_bytes(self) -> int:
        row = self._db.fetch_one(
            """
            SELECT
                COALESCE((SELECT SUM(bytes) FROM cache_entries), 0) +
                COALESCE((SELECT SUM(size_bytes) FROM artifacts), 0) AS total_bytes
            """
        )
        return int(row["total_bytes"]) if row else 0

    def enforce_storage_budget(self, incoming_bytes: int = 0) -> None:
        used = self.total_storage_bytes()
        if used + incoming_bytes > self._settings.max_storage_bytes:
            raise AppError(
                ErrorCode.STORAGE_FULL,
                "Operational storage quota exceeded.",
                retryable=False,
                details={
                    "used_bytes": used,
                    "incoming_bytes": incoming_bytes,
                    "max_storage_bytes": self._settings.max_storage_bytes,
                },
            )

    def get(self, key: str) -> dict[str, Any] | None:
        now = time.time()
        row = self._db.fetch_one(
            "SELECT * FROM cache_entries WHERE key = ?",
            (key,),
        )
        if row is None:
            return None

        target_path = Path(row["path"])
        if float(row["expires_at"]) <= now or not target_path.is_file():
            self.delete(key)
            return None

        try:
            raw_bytes = target_path.read_bytes()
            return json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            self.delete(key)
            return None

    def get_by_video(
        self,
        *,
        video_id: str,
        kind: CacheKind | None = None,
        languages: list[str] | None = None,
    ) -> dict[str, Any] | None:
        now = time.time()
        params: list[Any] = [video_id, now]
        sql = "SELECT key, language FROM cache_entries WHERE video_id = ? AND expires_at > ?"
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY created_at DESC"
        rows = self._db.fetch_all(sql, tuple(params))
        if not rows:
            return None

        if languages:
            for source_pref in ("youtube_manual_caption", "youtube_auto_caption", "whisper_asr", "youtube_translated_caption"):
                for lang in languages:
                    for row in rows:
                        if row["language"] == lang:
                            payload = self.get(row["key"])
                            if payload is not None and payload.get("source") == source_pref:
                                return payload
            for lang in languages:
                for row in rows:
                    if row["language"] == lang:
                        payload = self.get(row["key"])
                        if payload is not None:
                            return payload
        for row in rows:
            payload = self.get(row["key"])
            if payload is not None:
                return payload
        return None

    def put(
        self,
        *,
        key: str,
        kind: CacheKind,
        video_id: str,
        payload: dict[str, Any],
        language: str = "",
        source: str = "",
        extractor_version: str = "1.0",
        ttl_seconds: int | None = None,
    ) -> Path:
        target_dir = self._dir_for_kind(kind)
        target_dir.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.enforce_storage_budget(len(serialized))

        content_hash = hashlib.sha256(serialized).hexdigest()
        final_path = (target_dir / f"{key}.json").resolve()
        if not str(final_path).startswith(str(target_dir.resolve())):
            raise AppError(ErrorCode.INTERNAL_ERROR, "Invalid cache file path.")

        tmp_path = target_dir / f".{key}.{uuid.uuid4().hex[:8]}.tmp"
        tmp_path.write_bytes(serialized)
        os.replace(tmp_path, final_path)

        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self._ttl_for_kind(kind)
        expires_at = now + ttl

        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO cache_entries (
                    key, kind, video_id, language, source, path,
                    content_hash, bytes, created_at, expires_at, extractor_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    kind = excluded.kind,
                    video_id = excluded.video_id,
                    language = excluded.language,
                    source = excluded.source,
                    path = excluded.path,
                    content_hash = excluded.content_hash,
                    bytes = excluded.bytes,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    extractor_version = excluded.extractor_version
                """,
                (
                    key,
                    kind,
                    video_id,
                    language,
                    source,
                    str(final_path),
                    content_hash,
                    len(serialized),
                    now,
                    expires_at,
                    extractor_version,
                ),
            )
        return final_path

    def delete(self, key: str) -> None:
        row = self._db.fetch_one("SELECT path FROM cache_entries WHERE key = ?", (key,))
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
        if row:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError:
                pass

    def purge_expired(self, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        rows = self._db.fetch_all(
            "SELECT key, path FROM cache_entries WHERE expires_at <= ?",
            (ts,),
        )
        if not rows:
            return 0
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM cache_entries WHERE expires_at <= ?", (ts,))
        for row in rows:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError:
                pass
        return len(rows)
