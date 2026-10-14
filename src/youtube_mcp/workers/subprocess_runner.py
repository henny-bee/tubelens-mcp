from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from youtube_mcp.config import Settings, redact_text
from youtube_mcp.schemas.common import AppError, ErrorCode
from youtube_mcp.schemas.transcript import (
    TranscriptRecord,
    TranscriptSegment,
    TranscriptSource,
)


def _terminate_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name != "nt" and proc.pid:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class SubprocessAsrRunner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._active_procs: dict[str, asyncio.subprocess.Process] = {}
        self._cancelled_jobs: set[str] = set()

    def cancel(self, job_id: str) -> bool:
        self._cancelled_jobs.add(job_id)
        proc = self._active_procs.get(job_id)
        if proc is not None and proc.returncode is None:
            _terminate_process_tree(proc)
            return True
        return False

    async def run(
        self,
        *,
        job_id: str,
        video_id: str,
        audio_wav_path: Path,
        output_json_path: Path,
        language: str | None = None,
    ) -> TranscriptRecord:
        if job_id in self._cancelled_jobs:
            raise AppError(ErrorCode.JOB_CANCELLED, "ASR job was cancelled before execution.")

        argv = [
            sys.executable,
            "-m",
            "youtube_mcp.workers.transcribe",
            "--audio",
            str(audio_wav_path),
            "--output",
            str(output_json_path),
            "--models-dir",
            str(self._settings.models_dir),
            "--model",
            self._settings.whisper_model,
            "--compute-type",
            self._settings.whisper_compute_type,
            "--threads",
            str(self._settings.whisper_cpu_threads),
            "--beam-size",
            str(self._settings.whisper_beam_size),
            "--vad",
            "true" if self._settings.whisper_vad_enabled else "false",
            "--language",
            language or "",
        ]

        env = os.environ.copy()
        threads_str = str(self._settings.whisper_cpu_threads)
        env["OMP_NUM_THREADS"] = threads_str
        env["OPENBLAS_NUM_THREADS"] = threads_str
        env["MKL_NUM_THREADS"] = threads_str

        popen_kwargs: dict[str, object] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **popen_kwargs,  # type: ignore[arg-type]
        )
        self._active_procs[job_id] = proc

        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=float(self._settings.asr_timeout_seconds),
            )
        except asyncio.TimeoutError as exc:
            _terminate_process_tree(proc)
            await proc.wait()
            raise AppError(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"ASR worker exceeded timeout of {self._settings.asr_timeout_seconds} seconds.",
                retryable=False,
                details={"job_id": job_id, "video_id": video_id},
            ) from exc
        finally:
            self._active_procs.pop(job_id, None)

        if job_id in self._cancelled_jobs:
            self._cancelled_jobs.discard(job_id)
            raise AppError(
                ErrorCode.JOB_CANCELLED,
                "ASR job was cancelled while running.",
                details={"job_id": job_id, "video_id": video_id},
            )

        rc = proc.returncode or 0
        stderr_clean = redact_text(stderr.decode("utf-8", errors="replace"))

        if rc in (-9, 137) or "memoryerror" in stderr_clean.lower() or "bad_alloc" in stderr_clean.lower():
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                "ASR worker subprocess terminated due to memory/resource limits (OOM).",
                retryable=False,
                details={"job_id": job_id, "video_id": video_id, "exit_code": rc},
            )

        if rc != 0 or not output_json_path.is_file():
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "ASR worker subprocess failed.",
                retryable=False,
                details={"job_id": job_id, "video_id": video_id, "exit_code": rc},
            )

        data = json.loads(output_json_path.read_text(encoding="utf-8"))
        segments = [TranscriptSegment(**s) for s in data.get("segments", [])]
        if not segments:
            raise AppError(
                ErrorCode.CAPTION_UNAVAILABLE,
                "Whisper ASR completed but detected no speech segments.",
                details={"job_id": job_id, "video_id": video_id},
            )

        return TranscriptRecord(
            video_id=video_id,
            language=str(data.get("language") or language or "en"),
            source=TranscriptSource.WHISPER_ASR,
            is_generated=True,
            segments=segments,
            extractor_version=f"faster-whisper-{self._settings.whisper_model}-{self._settings.whisper_compute_type}",
            created_at=float(data.get("created_at") or time.time()),
        )
