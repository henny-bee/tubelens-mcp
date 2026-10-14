from __future__ import annotations

import shutil
import sys

import uvicorn

from youtube_mcp.config import Settings
from youtube_mcp.mcp_server import build_runtime, create_asgi_app


def run_startup_self_check(settings: Settings) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for binary in ("yt-dlp", "deno", "ffmpeg"):
        found = shutil.which(binary)
        statuses[binary] = found or "missing"
    settings.ensure_directories()
    return statuses


def main() -> int:
    try:
        settings = Settings()
    except Exception as exc:
        sys.stderr.write(f"Configuration validation error: {exc}\n")
        return 1

    run_startup_self_check(settings)
    runtime = build_runtime(settings)
    app = create_asgi_app(runtime)

    uvicorn.run(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        workers=1,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
