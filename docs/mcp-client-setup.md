# MCP Client Setup Guide

This guide explains how to connect remote AI clients to Tubelens MCP over Streamable HTTP, configure multi-scope tokens, terminate TLS with a reverse proxy, and troubleshoot common connection issues.

## 1. Endpoint Overview

- **Transport protocol:** MCP Streamable HTTP
- **MCP endpoint path:** `/mcp` (e.g., `https://youtube.example.com/mcp`)
- **Health probes:** `/healthz` (unauthenticated liveness) and `/readyz` (authenticated readiness)
- **Artifact download route:** `/artifacts/{artifact_id}` (authenticated HTTP GET, when `ENABLE_MEDIA_DOWNLOAD=true`)

## 2. Authentication Modes and Scopes

### Bearer Token Authentication (`AUTH_MODE=bearer`)

Every HTTP request to `/mcp`, `/readyz`, or `/artifacts/{artifact_id}` must include an `Authorization` header:

```http
Authorization: Bearer <your-secret-token>
```

Never pass tokens in URL query parameters (`?token=...` or `?access_token=...`); the server explicitly rejects query-string credentials with `401 Unauthorized` to prevent secret leakage in proxy logs.

### Multi-Scope Token Configuration (`MCP_TOKENS_JSON`)

By default, `MCP_BEARER_TOKEN` grants the scopes listed in `MCP_DEFAULT_SCOPES` (default: `["read"]`). To provision separate tokens for read-only AI agents, media download clients, and operators, set `MCP_TOKENS_JSON` in `.env`:

```dotenv
MCP_TOKENS_JSON={"tok_reader_9f8a7b6c5d4e3f2a1b0c":{"principal_id":"research_agent","scopes":["read"]},"tok_media_1a2b3c4d5e6f7a8b9c0d":{"principal_id":"media_worker","scopes":["read","media:download"]},"tok_admin_0f1e2d3c4b5a69788796":{"principal_id":"operator_cli","scopes":["read","media:download","admin:jobs"]}}
```

Available scopes:

| Scope | Permitted Actions |
|---|---|
| `read` | All read-only metadata, caption, transcript, search, playlist, channel, format, comment, job status, and artifact metadata tools, plus enqueueing ASR fallback when `allow_asr=true`. |
| `media:download` | Required (along with `ENABLE_MEDIA_DOWNLOAD=true` and `confirm_authorized=true`) to call `youtube_download_media` and fetch `/artifacts/{artifact_id}`. |
| `admin:jobs` | Allows cancelling jobs created by any principal via `youtube_cancel_job` (without `admin:jobs`, a principal may only cancel its own jobs). |

### Trusted Ingress Authentication (`AUTH_MODE=trusted_ingress`)

If an upstream authenticating proxy or identity-aware gateway validates client credentials before forwarding to `127.0.0.1:8000`, you can set `AUTH_MODE=trusted_ingress`. The proxy must strip external `X-Authenticated-*` headers and inject:

```http
X-Authenticated-Principal: user_or_agent_id
X-Authenticated-Scopes: read
```

Only enable `trusted_ingress` when port 8000 is strictly bound to `127.0.0.1` or an isolated firewall zone reachable exclusively by the proxy.

## 3. Client Configuration Examples

### Generic MCP Streamable HTTP Client JSON

For MCP clients that support `streamable-http` or `http` server definitions with custom headers:

```json
{
  "mcpServers": {
    "youtube-intelligence": {
      "type": "streamable-http",
      "url": "https://youtube.example.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_SECRET_BEARER_TOKEN"
      }
    }
  }
}
```

### Python FastMCP Client Example

```python
import asyncio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


async def main() -> None:
    transport = StreamableHttpTransport(
        url="https://youtube.example.com/mcp",
        headers={"Authorization": "Bearer YOUR_SECRET_BEARER_TOKEN"},
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        print("Available tools:", [t.name for t in tools])

        info = await client.call_tool(
            "youtube_get_video_info",
            {"url_or_id": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        print("Video info:", info)


if __name__ == "__main__":
    asyncio.run(main())
```

### Compatibility Note for Hosted AI Clients

Some hosted web AI clients require OAuth 2.1 dynamic discovery instead of static `Authorization: Bearer` headers. Verify that your target MCP client supports custom `Authorization` headers over Streamable HTTP, or place an OAuth-compatible gateway in front of the service using `AUTH_MODE=trusted_ingress`.

## 4. Reverse Proxy and TLS Termination

The Docker Compose configuration binds port 8000 to `127.0.0.1:8000`. Use a reverse proxy on the host to terminate HTTPS.

### Option A: Caddy (`Caddyfile`)

Caddy automatically provisions and renews Let's Encrypt TLS certificates:

```caddyfile
youtube.example.com {
    encode zstd gzip

    handle /mcp* {
        reverse_proxy 127.0.0.1:8000 {
            flush_interval -1
            transport http {
                read_timeout 120s
                write_timeout 120s
            }
        }
    }

    handle /artifacts/* {
        reverse_proxy 127.0.0.1:8000
    }

    handle /readyz {
        reverse_proxy 127.0.0.1:8000
    }

    handle /healthz {
        reverse_proxy 127.0.0.1:8000
    }

    respond 404
}
```

### Option B: Nginx (`/etc/nginx/sites-available/youtube-mcp.conf`)

Disable proxy buffering on `/mcp` so Streamable HTTP responses flush immediately:

```nginx
server {
    listen 443 ssl http2;
    server_name youtube.example.com;

    ssl_certificate     /etc/letsencrypt/live/youtube.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/youtube.example.com/privkey.pem;

    location /mcp {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 120s;
    }

    location /artifacts/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location = /healthz {
        proxy_pass http://127.0.0.1:8000/healthz;
    }

    location = /readyz {
        proxy_pass http://127.0.0.1:8000/readyz;
    }
}
```

## 5. Troubleshooting

| Symptom | Cause | Resolution |
|---|---|---|
| `401 Unauthorized` with `Missing or malformed Bearer Authorization header` | Client did not send `Authorization: Bearer <token>` or passed `?token=` in query string | Move the token into the `Authorization` request header. |
| `403 Forbidden` with `lacks required scope 'media:download'` | Principal token only has `read` scope | Configure `MCP_TOKENS_JSON` with `"scopes": ["read", "media:download"]` for that token. |
| `UNSUPPORTED_OPERATION` when calling `youtube_download_media` | `ENABLE_MEDIA_DOWNLOAD` is `false` (default) | Set `ENABLE_MEDIA_DOWNLOAD=true` in `.env` and restart the container. |
| Client hangs or times out on `/mcp` | Reverse proxy is buffering streaming HTTP responses | Set `proxy_buffering off;` in Nginx or `flush_interval -1` in Caddy. |
| `youtube_get_transcript` returns `status: "pending"` | Video has no captions and an ASR job was queued | Poll `youtube_get_job_status(job_id)` until `job_state == "succeeded"`, then call `youtube_get_transcript` again. |
| `RESOURCE_LIMIT` on ASR request | Job queue is full (`MAX_QUEUE_SIZE=10`) or video exceeds `MAX_ASR_DURATION_SECONDS` (2700s) | Wait for active jobs to complete or adjust limits in `.env` if host hardware allows. |
