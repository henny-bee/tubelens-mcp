from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._connect()

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=10.0,
                check_same_thread=False,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute("PRAGMA busy_timeout=5000;")
            self._conn = conn
        return self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE;")
            try:
                yield conn
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            conn = self._connect()
            cur = conn.execute(query, params)
            return cur.fetchone()

    def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            conn = self._connect()
            cur = conn.execute(query, params)
            return list(cur.fetchall())

    def check_health(self) -> bool:
        try:
            row = self.fetch_one("SELECT 1 AS ok;")
            return bool(row and row["ok"] == 1)
        except Exception:
            return False

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
