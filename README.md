# Tubelens MCP

A self-hosted Model Context Protocol (MCP) server for YouTube video metadata, timestamped transcripts, transcript search, playlists, channels, media formats, comments, and opt-in media downloads.

Runs as a single Docker container over MCP Streamable HTTP (`/mcp`) with SQLite caching and an optional CPU `faster-whisper` (`int8`) fallback when captions are unavailable.

## Features

- **Caption-First & Cache-First:** Fetches official manual or auto-generated captions via `youtube-transcript-api` and `yt-dlp`, caching results in SQLite + JSON on `/data`.
- **CPU Whisper ASR Fallback:** When captions are missing, queues an isolated `faster-whisper` (`int8`) child process that releases all memory once transcription finishes.
- **Single Container:** Bundles Python, `yt-dlp`, `deno`, and `ffmpeg` with SQLite WAL persistence. No Redis, PostgreSQL, or GPU required.
- **Authenticated Remote MCP:** Streamable HTTP endpoint at `/mcp` protected by Bearer token authentication, scoped permissions (`read`, `media:download`, `admin:jobs`), and rate limits.

## Quickstart

1. **Create your `.env` file:**
   ```bash
   cp .env.example .env
   ```
   Generate a secret token (at least 16 characters) and set `MCP_BEARER_TOKEN` in `.env`:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. **Start the server:**
   ```bash
   docker compose up -d --build
   ```

3. **Check service health:**
   ```bash
   curl -fsS http://127.0.0.1:8000/healthz
   ```

## MCP Client Configuration

Point your MCP client to `/mcp` using Streamable HTTP and a `Bearer` token header (place behind a TLS reverse proxy such as Caddy or Nginx when exposing remotely):

```json
{
  "mcpServers": {
    "youtube": {
      "type": "streamable-http",
      "url": "https://youtube.example.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_BEARER_TOKEN"
      }
    }
  }
}
```

## Available Tools

| Tool | Scope | Parameters | Description |
|---|---|---|---|
| `youtube_get_video_info` | `read` | `url_or_id`, `refresh=false` | Retrieve video metadata (title, channel, duration, views, thumbnail, availability). |
| `youtube_list_captions` | `read` | `url_or_id` | List available manual and auto-generated caption tracks. |
| `youtube_get_transcript` | `read` | `url_or_id`, `languages=["id","en"]`, `allow_asr=true`, `allow_other_language=false`, `offset=0`, `limit=100` | Fetch paginated `{start, end, text}` segments, or enqueue an ASR job if captions are unavailable. |
| `youtube_get_transcript_window` | `read` | `video_id`, `start_sec`, `end_sec`, `max_chars=12000`, `languages=["id","en"]` | Retrieve transcript segments within a `[start_sec, end_sec]` time window. |
| `youtube_search_transcript` | `read` | `video_id`, `query`, `limit=20`, `languages=["id","en"]` | Search within a video's transcript and return matching segments with timestamps. |
| `youtube_search_videos` | `read` | `query`, `limit=10` | Search YouTube videos by keyword (`limit <= 20`). |
| `youtube_list_playlist` | `read` | `playlist_url`, `cursor=null`, `limit=20` | List videos in a playlist with cursor pagination (`limit <= 50`). |
| `youtube_list_channel` | `read` | `channel_url`, `cursor=null`, `limit=20` | List recent uploads from a channel (`@handle`, `UC...`, or URL, `limit <= 50`). |
| `youtube_get_formats` | `read` | `url_or_id` | List available audio and video stream formats, codecs, and sizes. |
| `youtube_get_comments` | `read` | `url_or_id`, `limit=20` | Fetch top video comments (`limit <= 100`). |
| `youtube_download_media` | `media:download` | `url_or_id`, `kind="audio"`, `format="m4a"`, `confirm_authorized=false` | Schedule an async media download job (requires `ENABLE_MEDIA_DOWNLOAD=true` and `confirm_authorized=true`). |
| `youtube_get_artifact` | `media:download` | `job_id=null`, `artifact_id=null` | Get metadata and authenticated `/artifacts/{artifact_id}` download URL for a completed media job. |
| `youtube_get_job_status` | `read` | `job_id` | Poll status and progress of an ASR or media download job. |
| `youtube_cancel_job` | `admin:jobs` | `job_id` | Cancel a queued or running job and clean up temporary files. |

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `MCP_HOST` | `0.0.0.0` | Server bind address. |
| `MCP_PORT` | `8000` | Server HTTP port. |
| `MCP_PUBLIC_BASE_URL` | `https://youtube.example.com` | Public base URL used for artifact download links. |
| `AUTH_MODE` | `bearer` | Authentication mode (`bearer` or `trusted_ingress`). |
| `MCP_BEARER_TOKEN` | *(required)* | Secret token (>= 16 chars) for `AUTH_MODE=bearer`. |
| `MCP_DEFAULT_SCOPES` | `["read"]` | Scopes granted to `MCP_BEARER_TOKEN`. |
| `MCP_TOKENS_JSON` | `""` | Optional JSON map of `token -> {"principal_id": "...", "scopes": [...]}`. |
| `DATA_DIR` | `/data` | Persistent storage path for SQLite, cache, models, and artifacts. |
| `WHISPER_ENABLED` | `true` | Enable `faster-whisper` ASR fallback when captions are missing. |
| `WHISPER_MODEL` | `base` | Whisper model size (`tiny`, `base`, or `small`). |
| `WHISPER_COMPUTE_TYPE` | `int8` | Quantization type (`int8`, `int8_float32`, `float32`). |
| `WHISPER_CPU_THREADS` | `1` | CPU threads per ASR worker subprocess. |
| `MAX_QUEUE_SIZE` | `10` | Maximum concurrent queued + running background jobs. |
| `MAX_ASR_DURATION_SECONDS` | `2700` | Maximum video duration eligible for ASR or media download (45 minutes). |
| `MAX_STORAGE_BYTES` | `5368709120` | Maximum storage quota for caches and artifacts (5 GiB). |
| `ENABLE_MEDIA_DOWNLOAD` | `false` | Enable `youtube_download_media` tool. |
| `ENABLE_COMMENT_EXTRACTION` | `true` | Enable `youtube_get_comments` tool. |
| `ENABLE_SEARCH` | `true` | Enable `youtube_search_videos` tool. |

## Legal Notice

Tubelens MCP is an independent self-hosted tool and is not affiliated with or endorsed by YouTube or Google. Users are responsible for complying with the [YouTube Terms of Service](https://www.youtube.com/t/terms) and applicable copyright laws, and must only process or download content they are authorized to access.

## License

MIT
