from __future__ import annotations

from youtube_mcp.storage.db import Database

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    job_key TEXT NOT NULL UNIQUE,
    video_id TEXT NOT NULL,
    options_json TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0,
    error_code TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    result_ref TEXT,
    expires_at REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_video_id ON jobs(video_id);

CREATE TABLE IF NOT EXISTS cache_entries (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    video_id TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    extractor_version TEXT NOT NULL DEFAULT '1.0'
);

CREATE INDEX IF NOT EXISTS idx_cache_video_kind ON cache_entries(video_id, kind, language);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache_entries(expires_at);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    path TEXT NOT NULL,
    mime TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_job_id ON artifacts(job_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_expires ON artifacts(expires_at);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    action TEXT NOT NULL,
    object_id TEXT NOT NULL,
    status TEXT NOT NULL,
    timestamp REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_events(timestamp);
"""


def apply_migrations(db: Database) -> None:
    import time

    with db.transaction() as conn:
        for statement in SCHEMA_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                conn.execute(stmt)
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (1, ?)",
            (time.time(),),
        )
