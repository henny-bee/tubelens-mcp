FROM denoland/deno:bin-2.3.1 AS deno_bin

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno_bin /deno /usr/local/bin/deno

RUN groupadd --gid 10001 appuser && \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser && \
    mkdir -p /data/state /data/cache/metadata /data/cache/captions /data/cache/asr /data/models /data/artifacts /data/tmp && \
    chown -R appuser:appuser /data

WORKDIR /app
COPY pyproject.toml requirements.lock README.md ./
COPY src/ ./src/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.lock && \
    pip install --no-cache-dir --no-deps . && \
    deno --version && \
    ffmpeg -version && \
    yt-dlp --version

USER appuser
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "youtube_mcp"]
