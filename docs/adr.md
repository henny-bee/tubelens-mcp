# Architecture Decision Records (ADRs)

This document captures the foundational architectural decisions for Tubelens MCP.

## ADR-001: Single Application Container Modular Monolith

- **Status:** Accepted
- **Date:** 2026-10-14

### Context

Self-hosted AI tooling frequently suffers from operational sprawl: separate containers for API gateways, Redis brokers, PostgreSQL databases, Celery workers, and dedicated GPU model servers. For personal and small-team deployments on modest VPS hardware (1 vCPU, 2 GiB RAM), multi-container orchestration increases memory overhead, configuration complexity, and failure modes.

### Decision

Package and deploy Tubelens MCP as a single application Docker container running a Python 3.12 modular monolith:
1. One ASGI process exposes the remote MCP Streamable HTTP endpoint (`/mcp`), health probes (`/healthz`, `/readyz`), and authenticated artifact download route (`/artifacts/{artifact_id}`).
2. Background tasks (ASR transcription and opt-in media downloads) are managed by an in-process single-consumer scheduler backed by persistent local storage.
3. Runtime binaries (`ffmpeg`, `deno`, `yt-dlp`) are bundled directly into the container image via a multi-stage Docker build.

### Consequences

- **Positive:** One-command deployment (`docker compose up -d --build`), zero inter-service network serialization overhead, and minimal baseline memory footprint.
- **Negative:** Horizontal scaling across multiple container replicas is not supported in v1 (`uvicorn` must run with `workers=1` so the internal job scheduler does not double-run). Container restarts temporarily interrupt in-flight jobs, which are reconciled on startup.

---

## ADR-002: SQLite WAL Persistent Job Queue and Cache Index

- **Status:** Accepted
- **Date:** 2026-10-14

### Context

Asynchronous speech-to-text jobs and multi-tier caching (metadata, captions, ASR transcripts, and media artifacts) require durability across container restarts, deduplication of concurrent identical requests, and deterministic TTL expiration without introducing external Redis or PostgreSQL dependencies.

### Decision

Use SQLite in Write-Ahead Logging (`WAL`) mode at `/data/state/app.sqlite3` as the single source of truth for job state, cache metadata, artifacts, and security audit events:
1. Enable `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`, `PRAGMA foreign_keys=ON;`, and `PRAGMA busy_timeout=5000;`.
2. Write large payload blobs (normalized transcript JSON, metadata JSON, media artifacts) atomically to `/data/cache` and `/data/artifacts` via temporary files (`*.tmp`) followed by `os.replace`, recording only metadata and file paths in SQLite after the file rename succeeds.
3. Deduplicate jobs using a deterministic `job_key` hash (`video_id`, `kind`, `language`, `model`, `extractor_version`, `options`).
4. Reconcile any jobs left in `running` state at startup to `interrupted`, automatically requeuing idempotent jobs when `attempts < MAX_JOB_ATTEMPTS`.

### Consequences

- **Positive:** Zero external database containers, ACID transactions for job claiming and cache indexing, and straightforward online backup using SQLite's native backup API.
- **Negative:** Requires a POSIX-compliant local host volume for `/data`; network filesystems (such as NFS or SMB without proper locking) are unsupported for SQLite WAL files.

---

## ADR-003: Caption-First and Cache-First Transcript Pipeline

- **Status:** Accepted
- **Date:** 2026-10-14

### Context

Running local CPU speech-to-text (`faster-whisper`) on every transcript request wastes CPU cycles, increases latency from sub-second to minutes, and risks saturating the job queue when creator-supplied or YouTube auto-generated captions already exist.

### Decision

Enforce a strict multi-stage resolution order in `TranscriptResolver`:
1. **Cache lookup:** Check `/data/cache/captions` and `/data/cache/asr` for a valid, non-expired normalized transcript matching the requested language preference.
2. **Primary caption adapter:** Query `youtube-transcript-api` for manual or auto-generated transcripts matching the requested languages.
3. **Fallback caption adapter:** If `youtube-transcript-api` cannot retrieve tracks (and did not fail due to an upstream IP block), fall back to `yt-dlp` subtitle extraction (`subtitles` then `automatic_captions`) and normalize VTT/SRT/JSON3 cues into sorted, deduplicated `{start, end, text}` segments.
4. **Conditional ASR fallback:** Only when no caption tracks exist, `allow_asr=true` is requested by the caller, `WHISPER_ENABLED=true` is set by the operator, and the video duration is within `MAX_ASR_DURATION_SECONDS`, enqueue an asynchronous ASR job and return `status="pending"` with a `job_id`.
5. **Provenance tracking:** Every transcript response explicitly labels `source` (`youtube_manual_caption`, `youtube_auto_caption`, `youtube_translated_caption`, or `whisper_asr`) and `is_generated`.

### Consequences

- **Positive:** Captioned videos resolve in seconds with near-zero CPU load; repeated requests are served from local disk cache in milliseconds; callers always know the provenance of the text.
- **Negative:** When YouTube blocks caption extraction from a datacenter IP (`YOUTUBE_ACCESS_BLOCKED`), the pipeline fails transparently rather than silently attempting an equally blocked audio download.

---

## ADR-004: Isolated Subprocess for CPU INT8 faster-whisper ASR

- **Status:** Accepted
- **Date:** 2026-10-14

### Context

Loading CTranslate2 / `faster-whisper` models directly inside the main asyncio/FastMCP process retains hundreds of megabytes of resident memory (RSS) indefinitely due to native allocator fragmentation, blocks or starves the Python GIL during CPU-bound inference, and prevents clean cancellation if an inference pass hangs or hits an out-of-memory condition.

### Decision

Execute all `faster-whisper` transcription work inside an isolated child process (`python -m youtube_mcp.workers.transcribe`) spawned on demand by the internal job scheduler:
1. The main MCP server process never imports or initializes `faster-whisper` model weights.
2. Audio is extracted and normalized to 16 kHz mono PCM WAV via `ffmpeg` in `/data/tmp/<job_id>/` prior to spawning the worker.
3. The worker runs with `device="cpu"`, `compute_type="int8"`, `cpu_threads=1`, `beam_size=1`, and `vad_filter=True` by default, storing reusable model weights in `/data/models`.
4. The worker starts in its own process group (`start_new_session=True`) so job cancellation or timeout (`ASR_TIMEOUT_SECONDS`) kills the entire process tree cleanly and deletes temporary audio files.

### Consequences

- **Positive:** 100% of ASR model and allocator memory is returned to the OS immediately when a job finishes; the FastMCP HTTP event loop remains responsive to metadata and cached transcript queries during active transcription; OOM kills in the worker do not crash the MCP server.
- **Negative:** Each ASR job incurs a cold-start overhead of loading model weights from `/data/models` into the child process (typically 1-3 seconds on SSD storage).

---

## ADR-005: Authenticated Remote MCP and Trusted HTTPS Boundary

- **Status:** Accepted
- **Date:** 2026-10-14

### Context

Exposing a remote MCP server that can invoke `yt-dlp`, `ffmpeg`, and CPU-intensive ASR without authentication or transport encryption would turn the service into an open proxy, an unauthenticated scraping relay, and an easy denial-of-service target.

### Decision

Enforce a fail-closed authentication and authorization boundary around the ASGI application:
1. **Transport security:** Require `MCP_PUBLIC_BASE_URL` to use `https://` for all non-localhost deployments. Bind the Docker container port to `127.0.0.1:8000` by default so public traffic must pass through a TLS-terminating reverse proxy (e.g., Caddy or Nginx) or private tunnel.
2. **Authentication:** Reject startup if `AUTH_MODE=bearer` and neither a strong `MCP_BEARER_TOKEN` (>= 16 characters, non-placeholder) nor `MCP_TOKENS_JSON` is configured. Verify tokens in constant time (`hmac.compare_digest`). Reject any request passing tokens in URL query parameters (`?token=...`).
3. **Scoped authorization:** Assign every authenticated principal a set of scopes (`read`, `media:download`, `admin:jobs`). Check scopes inside tool handlers regardless of client-side tool visibility.
4. **Untrusted content tagging:** Mark all upstream YouTube text fields (titles, descriptions, captions, comments) with `untrusted_content: true` so downstream AI agents maintain a clear prompt-injection boundary.

### Consequences

- **Positive:** Prevents unauthorized usage, credential leakage in URL access logs, SSRF via arbitrary URLs, and accidental exposure of media download capabilities.
- **Negative:** Operators must configure a bearer token and TLS reverse proxy before connecting remote AI clients; clients that only support unauthenticated HTTP cannot connect.
