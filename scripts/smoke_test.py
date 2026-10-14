#!/usr/bin/env python3
"""Runtime dependency and HTTP endpoint smoke test for Tubelens MCP."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _run_version_cmd(cmd: list[str]) -> dict[str, Any]:
    binary = cmd[0]
    resolved = shutil.which(binary)
    if not resolved:
        return {"ok": False, "binary": binary, "error": f"{binary} not found in PATH"}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        first_line = (proc.stdout or proc.stderr or "").strip().splitlines()
        version_str = first_line[0] if first_line else "unknown"
        return {
            "ok": proc.returncode == 0,
            "binary": resolved,
            "version": version_str,
            "returncode": proc.returncode,
        }
    except Exception as exc:
        return {"ok": False, "binary": resolved, "error": str(exc)}


def check_python_runtime() -> dict[str, Any]:
    ver = sys.version_info
    ok = (ver.major, ver.minor) >= (3, 11)
    return {
        "ok": ok,
        "version": f"{ver.major}.{ver.minor}.{ver.micro}",
        "executable": sys.executable,
    }


def check_sqlite_wal() -> dict[str, Any]:
    try:
        with tempfile.TemporaryDirectory(prefix="ytmcp_smoke_") as tmp:
            db_file = Path(tmp) / "smoke.sqlite3"
            conn = sqlite3.connect(str(db_file))
            try:
                mode = conn.execute("PRAGMA journal_mode=WAL;").fetchone()[0]
                conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);")
                conn.execute("INSERT INTO t (val) VALUES ('ok');")
                conn.commit()
                integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
            finally:
                conn.close()
        return {
            "ok": str(mode).lower() == "wal" and integrity == "ok",
            "sqlite_version": sqlite3.sqlite_version,
            "journal_mode": mode,
            "integrity": integrity,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def check_youtube_mcp_imports() -> dict[str, Any]:
    modules = [
        "youtube_mcp",
        "youtube_mcp.config",
        "youtube_mcp.schemas.common",
        "youtube_mcp.schemas.video",
        "youtube_mcp.schemas.transcript",
        "youtube_mcp.schemas.job",
        "youtube_mcp.storage.db",
        "youtube_mcp.storage.migrations",
        "youtube_mcp.storage.cache",
        "youtube_mcp.storage.artifacts",
        "youtube_mcp.security.auth",
        "youtube_mcp.security.authorization",
        "youtube_mcp.security.rate_limits",
        "youtube_mcp.security.audit",
    ]
    optional_modules = [
        "youtube_mcp.services.url_validator",
        "youtube_mcp.services.transcript_normalizer",
        "youtube_mcp.workers.transcribe",
        "youtube_mcp.workers.subprocess_runner",
        "youtube_mcp.mcp_server",
    ]
    loaded: list[str] = []
    failed: dict[str, str] = {}
    for mod in modules:
        try:
            importlib.import_module(mod)
            loaded.append(mod)
        except Exception as exc:
            failed[mod] = str(exc)

    optional_loaded: list[str] = []
    for mod in optional_modules:
        try:
            importlib.import_module(mod)
            optional_loaded.append(mod)
        except Exception:
            pass

    return {
        "ok": len(failed) == 0,
        "loaded_core": loaded,
        "loaded_optional": optional_loaded,
        "failed": failed,
    }


def probe_http_endpoints(base_url: str, bearer_token: str | None) -> dict[str, Any]:
    base = base_url.rstrip("/")
    results: dict[str, Any] = {}

    health_req = urllib.request.Request(f"{base}/healthz", method="GET")
    try:
        with urllib.request.urlopen(health_req, timeout=10) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            results["healthz"] = {"ok": resp.status == 200, "status": resp.status, "body": body[:200]}
    except Exception as exc:
        results["healthz"] = {"ok": False, "error": str(exc)}

    unauth_req = urllib.request.Request(f"{base}/mcp", method="POST", data=b"{}")
    unauth_req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(unauth_req, timeout=10) as resp:
            results["mcp_unauth_rejected"] = {"ok": False, "status": resp.status}
    except urllib.error.HTTPError as err:
        results["mcp_unauth_rejected"] = {
            "ok": err.code in (401, 403),
            "status": err.code,
        }
    except Exception as exc:
        results["mcp_unauth_rejected"] = {"ok": False, "error": str(exc)}

    if bearer_token:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "1.0.0"},
                },
            }
        ).encode("utf-8")
        mcp_req = urllib.request.Request(f"{base}/mcp", method="POST", data=payload)
        mcp_req.add_header("Authorization", f"Bearer {bearer_token}")
        mcp_req.add_header("Content-Type", "application/json")
        mcp_req.add_header("Accept", "application/json, text/event-stream")
        try:
            with urllib.request.urlopen(mcp_req, timeout=10) as resp:
                results["mcp_authenticated"] = {
                    "ok": 200 <= resp.status < 300,
                    "status": resp.status,
                }
        except urllib.error.HTTPError as err:
            results["mcp_authenticated"] = {
                "ok": err.code not in (401, 403),
                "status": err.code,
            }
        except Exception as exc:
            results["mcp_authenticated"] = {"ok": False, "error": str(exc)}

    overall = all(item.get("ok", False) for item in results.values())
    results["ok"] = overall
    return results


def run_smoke_checks(
    url: str | None = None,
    token: str | None = None,
    allow_missing_binaries: bool = False,
) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "python": check_python_runtime(),
        "sqlite_wal": check_sqlite_wal(),
        "youtube_mcp_imports": check_youtube_mcp_imports(),
        "yt_dlp": _run_version_cmd(["yt-dlp", "--version"]),
        "deno": _run_version_cmd(["deno", "--version"]),
        "ffmpeg": _run_version_cmd(["ffmpeg", "-version"]),
    }
    if url:
        checks["http_probes"] = probe_http_endpoints(url, token)

    required_keys = ["python", "sqlite_wal", "youtube_mcp_imports"]
    if not allow_missing_binaries:
        required_keys.extend(["yt_dlp", "deno", "ffmpeg"])
    if url:
        required_keys.append("http_probes")

    overall_ok = all(checks[k].get("ok", False) for k in required_keys)
    return {"ok": overall_ok, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check Tubelens MCP runtime dependencies and optional HTTP endpoints."
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("SMOKE_TEST_URL", ""),
        help="Optional running server base URL (e.g. http://127.0.0.1:8000)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("MCP_BEARER_TOKEN", ""),
        help="Optional bearer token for authenticated /mcp probe",
    )
    parser.add_argument(
        "--allow-missing-binaries",
        action="store_true",
        help="Do not fail exit code if external CLI binaries (deno/ffmpeg/yt-dlp) are absent on dev host",
    )
    args = parser.parse_args(argv)

    report = run_smoke_checks(
        url=args.url.strip() or None,
        token=args.token.strip() or None,
        allow_missing_binaries=args.allow_missing_binaries,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
