from __future__ import annotations

import time
import uuid

from youtube_mcp.storage.db import Database


class AuditLogger:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record(
        self,
        *,
        principal_id: str,
        action: str,
        object_id: str,
        status: str,
    ) -> str:
        event_id = f"aud_{uuid.uuid4().hex[:16]}"
        now = time.time()
        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (id, principal_id, action, object_id, status, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (event_id, principal_id, action, object_id, status, now),
            )
        return event_id
