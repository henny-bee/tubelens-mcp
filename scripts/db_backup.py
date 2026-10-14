#!/usr/bin/env python3
"""Safe online SQLite WAL backup and integrity verification utility."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


def backup_sqlite_database(
    source_path: Path,
    dest_path: Path,
    checkpoint: bool = True,
) -> dict[str, Any]:
    """Perform an online SQLite backup using PRAGMA wal_checkpoint(TRUNCATE) and Connection.backup()."""
    src = Path(source_path).resolve()
    dst = Path(dest_path).resolve()

    if not src.is_file():
        raise FileNotFoundError(f"Source SQLite database does not exist: {src}")
    if src == dst:
        raise ValueError("Source and destination paths must be different.")

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp_dst = dst.with_suffix(dst.suffix + ".tmp")
    if tmp_dst.exists():
        tmp_dst.unlink()

    started_at = time.time()
    checkpoint_result: tuple[Any, ...] | None = None

    src_conn = sqlite3.connect(str(src), timeout=30.0)
    try:
        src_conn.execute("PRAGMA busy_timeout=10000;")
        if checkpoint:
            row = src_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchone()
            checkpoint_result = tuple(row) if row is not None else None

        dst_conn = sqlite3.connect(str(tmp_dst), timeout=30.0)
        try:
            src_conn.backup(dst_conn, pages=256, sleep=0.05)
            dst_conn.execute("PRAGMA journal_mode=DELETE;")
            dst_conn.commit()
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    verify_conn = sqlite3.connect(str(tmp_dst), timeout=10.0)
    try:
        integrity_rows = verify_conn.execute("PRAGMA integrity_check;").fetchall()
        integrity_status = [r[0] for r in integrity_rows]
    finally:
        verify_conn.close()

    if integrity_status != ["ok"]:
        tmp_dst.unlink(missing_ok=True)
        raise RuntimeError(f"Backup integrity check failed: {integrity_status}")

    os.replace(tmp_dst, dst)
    elapsed_ms = round((time.time() - started_at) * 1000.0, 2)

    return {
        "ok": True,
        "source": str(src),
        "destination": str(dst),
        "size_bytes": dst.stat().st_size,
        "wal_checkpoint": list(checkpoint_result) if checkpoint_result is not None else None,
        "integrity_check": integrity_status[0],
        "elapsed_ms": elapsed_ms,
    }


def main(argv: list[str] | None = None) -> int:
    default_data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    parser = argparse.ArgumentParser(
        description="Safely back up the Tubelens MCP SQLite WAL database and verify integrity."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=default_data_dir / "state" / "app.sqlite3",
        help="Path to live SQLite database (default: /data/state/app.sqlite3)",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="Destination path for verified SQLite backup file",
    )
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="Skip PRAGMA wal_checkpoint(TRUNCATE) before copying pages",
    )
    args = parser.parse_args(argv)

    try:
        result = backup_sqlite_database(
            source_path=args.source,
            dest_path=args.dest,
            checkpoint=not args.no_checkpoint,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        err_payload = {
            "ok": False,
            "source": str(args.source),
            "destination": str(args.dest),
            "error": str(exc),
        }
        print(json.dumps(err_payload, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
