# Operations Runbook

This runbook covers deployment, verification, backup and restore, upgrades, secret rotation, storage maintenance, and incident response for Tubelens MCP.

## 1. Deployment Prerequisites

- **Host OS:** Linux x86_64 (recommended) or arm64 with Docker Engine 24+ and Docker Compose v2.
- **Hardware resources:**
  - **Balanced profile (default):** 1.0 vCPU, 2 GiB RAM, 10 GiB free SSD storage (`/data` volume uses up to 5 GiB for cache/artifacts plus ~150-500 MiB for Whisper model weights).
  - **Minimum profile:** 0.5 vCPU, 1 GiB RAM (`WHISPER_MODEL=tiny`, experimental).
  - **Quality profile:** 2.0 vCPU, 4 GiB RAM (`WHISPER_MODEL=small`, `WHISPER_CPU_THREADS=2`).
- **Network & TLS:** A domain name and TLS reverse proxy (Caddy, Nginx, Traefik) or private VPN/tunnel (Tailscale, WireGuard, Cloudflare Tunnel) forwarding HTTPS traffic to `127.0.0.1:8000`. Never expose port 8000 over plain HTTP on a public interface.
- **Filesystem:** `/data` must reside on a local POSIX filesystem supporting SQLite WAL file locking (do not mount `/data/state` over NFS or SMB).

## 2. Environment Configuration (`.env`)

1. Copy the template configuration file:
   ```bash
   cp .env.example .env
   chmod 600 .env
   ```

2. Generate a strong bearer secret (at least 32 random bytes):
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

3. Edit `.env` and set the required production parameters:
   ```dotenv
   MCP_HOST=0.0.0.0
   MCP_PORT=8000
   MCP_PUBLIC_BASE_URL=https://youtube.example.com
   AUTH_MODE=bearer
   MCP_BEARER_TOKEN=<paste-generated-secret-here>
   DATA_DIR=/data
   ```

4. Ensure `./data` exists and is writable by container UID/GID `10001:10001`:
   ```bash
   mkdir -p ./data
   sudo chown -R 10001:10001 ./data
   ```

## 3. Starting the Service

Build and start the single application container:

```bash
docker compose up -d --build
```

Inspect container status and startup logs:

```bash
docker compose ps
docker compose logs --tail=100 youtube-mcp
```

Note that structured logs redact bearer tokens, cookies, and authorization headers automatically.

## 4. Health and Readiness Verification

1. **Liveness check (`/healthz`):** Unauthenticated endpoint verifying the ASGI process is alive:
   ```bash
   curl -fsS http://127.0.0.1:8000/healthz
   ```

2. **Readiness check (`/readyz`):** Authenticated endpoint verifying SQLite WAL health, writable `/data` directories, and runtime binaries (`yt-dlp`, `deno`, `ffmpeg`):
   ```bash
   curl -fsS -H "Authorization: Bearer $MCP_BEARER_TOKEN" http://127.0.0.1:8000/readyz
   ```

3. **Container smoke test script:** Run the bundled verification script inside the running container:
   ```bash
   docker compose exec youtube-mcp python scripts/smoke_test.py \
     --url http://127.0.0.1:8000 \
     --token "$MCP_BEARER_TOKEN"
   ```

## 5. SQLite WAL Backup and Restore Procedure

### Online Backup

Use `scripts/db_backup.py` to perform a safe online backup. The script issues `PRAGMA wal_checkpoint(TRUNCATE)`, copies `/data/state/app.sqlite3` using Python's `sqlite3.Connection.backup()` API, and runs `PRAGMA integrity_check` on the resulting backup file:

```bash
docker compose exec youtube-mcp python scripts/db_backup.py \
  --source /data/state/app.sqlite3 \
  --dest /data/state/backup-$(date +%Y%m%d-%H%M%S).sqlite3
```

You can then copy the verified `.sqlite3` snapshot off-host from `./data/state/`.

### Restore Procedure

1. Stop the container so no process holds open file descriptors on the WAL or SHM files:
   ```bash
   docker compose down
   ```
2. Remove any stale WAL/SHM files and replace `app.sqlite3` with the verified backup:
   ```bash
   rm -f ./data/state/app.sqlite3-wal ./data/state/app.sqlite3-shm
   cp /path/to/backup-YYYYMMDD-HHMMSS.sqlite3 ./data/state/app.sqlite3
   chown 10001:10001 ./data/state/app.sqlite3
   ```
3. Start the service and verify readiness:
   ```bash
   docker compose up -d
   docker compose exec youtube-mcp python scripts/smoke_test.py
   ```

## 6. Upgrade and Rollback Runbook

### Upgrading

Because YouTube frequently updates its player JavaScript challenges, `yt-dlp`, `yt-dlp-ejs`, and `deno` versions are pinned in `requirements.lock` and `Dockerfile`.

1. Take an online SQLite backup before upgrading:
   ```bash
   docker compose exec youtube-mcp python scripts/db_backup.py \
     --source /data/state/app.sqlite3 \
     --dest /data/state/pre-upgrade.sqlite3
   ```
2. Tag the currently working image for instant rollback:
   ```bash
   docker tag tubelens-mcp:local tubelens-mcp:rollback
   ```
3. Pull the latest repository changes and rebuild:
   ```bash
   git pull
   docker compose up -d --build
   ```
4. Run the smoke test to confirm runtime dependencies and MCP response health:
   ```bash
   docker compose exec youtube-mcp python scripts/smoke_test.py \
     --url http://127.0.0.1:8000 \
     --token "$MCP_BEARER_TOKEN"
   ```

### Rolling Back

If an upgrade introduces an upstream regression:

1. Check out the previous git tag or commit, or restore the `tubelens-mcp:rollback` image tag:
   ```bash
   docker tag tubelens-mcp:rollback tubelens-mcp:local
   docker compose up -d --no-build
   ```
2. If database schema changes occurred, restore `./data/state/pre-upgrade.sqlite3` using the Restore Procedure in Section 5.

## 7. Token Rotation

### Zero-Downtime Rotation with `MCP_TOKENS_JSON`

To rotate tokens without breaking active clients immediately:

1. Add both the new token and old token to `MCP_TOKENS_JSON` in `.env`:
   ```dotenv
   MCP_TOKENS_JSON={"new_secret_token_value":{"principal_id":"agent_prod","scopes":["read"]},"old_secret_token_value":{"principal_id":"agent_legacy","scopes":["read"]}}
   ```
2. Recreate the container to reload `.env`:
   ```bash
   docker compose up -d
   ```
3. Update all remote MCP clients to use `new_secret_token_value`.
4. Remove `old_secret_token_value` from `.env` and run `docker compose up -d` again.

### Immediate Single-Token Rotation

Generate a new secret, replace `MCP_BEARER_TOKEN` in `.env`, and run `docker compose up -d`. All existing sessions using the old token will immediately receive `401 Unauthorized`.

## 8. Disk Quota and TTL Maintenance

The server automatically enforces storage hygiene on startup and every `CLEANUP_INTERVAL_SECONDS` (default 300s):

- **Temporary workspaces (`/data/tmp`):** Deleted immediately upon job completion, failure, or cancellation. Stale directories are purged on startup.
- **Expiration TTLs:**
  - Metadata cache (`METADATA_CACHE_TTL_SECONDS`): 6 hours (21,600s)
  - Caption cache (`CAPTION_CACHE_TTL_SECONDS`): 7 days (604,800s)
  - ASR transcript cache (`ASR_CACHE_TTL_SECONDS`): 30 days (2,592,000s)
  - Media artifacts (`ARTIFACT_TTL_SECONDS`): 24 hours (86,400s)
- **Storage quota (`MAX_STORAGE_BYTES`):** Default 5 GiB (`5368709120` bytes) across cache and artifacts. When total tracked bytes exceed `MAX_STORAGE_BYTES`, the cleanup routine evicts expired entries first and then least-recently-created entries until usage drops below the cap. If storage cannot be reclaimed, new downloads fail safely with `STORAGE_FULL`.

To inspect disk usage manually:
```bash
du -sh ./data/* ./data/cache/*
```

## 9. Incident Response

### Suspected Token Compromise or Abuse

1. Immediately rotate `MCP_BEARER_TOKEN` and `MCP_TOKENS_JSON` in `.env`.
2. Disable optional high-cost features in `.env`:
   ```dotenv
   ENABLE_MEDIA_DOWNLOAD=false
   WHISPER_ENABLED=false
   ```
3. Restart the container (`docker compose up -d`) to terminate any running subprocess workers and invalidate old tokens.
4. Query the SQLite `audit_events` and `jobs` tables to inspect activity by `principal_id`:
   ```bash
   sqlite3 ./data/state/app.sqlite3 \
     "SELECT datetime(timestamp, 'unixepoch'), principal_id, action, object_id, status FROM audit_events ORDER BY timestamp DESC LIMIT 50;"
   ```

### Upstream YouTube IP Block (`YOUTUBE_ACCESS_BLOCKED` / HTTP 403 / 429)

1. Verify whether requests return `YOUTUBE_ACCESS_BLOCKED` in logs.
2. Reduce `RATE_LIMIT_REQUESTS_PER_MINUTE` and increase `METADATA_CACHE_TTL_SECONDS` to minimize outbound requests.
3. Do not attempt to bypass DRM, CAPTCHA, or anti-bot protections. Ensure `yt-dlp` and `deno` are updated to the latest tested release.

### Runaway CPU or Memory Pressure (OOM)

1. Cancel any active ASR job via the `youtube_cancel_job` MCP tool (requires `admin:jobs` scope or matching principal) or restart the container (`docker compose restart youtube-mcp`).
2. If `WHISPER_MODEL=base` triggers OOM under the 2 GiB limit on your host architecture, either switch `WHISPER_MODEL=tiny` in `.env` or raise `mem_limit: 4g` in `docker-compose.yml`.
