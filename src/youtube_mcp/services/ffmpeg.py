from __future__ import annotations

import asyncio
import wave
from pathlib import Path
from typing import Awaitable, Callable

from youtube_mcp.config import Settings
from youtube_mcp.schemas.common import AppError, ErrorCode

FfmpegRunnerFn = Callable[[list[str], float], Awaitable[tuple[int, bytes, bytes]]]


async def default_ffmpeg_runner(argv: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AppError(
            ErrorCode.INTERNAL_ERROR,
            "FFmpeg binary is not installed in the runtime environment.",
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise AppError(
            ErrorCode.UPSTREAM_TIMEOUT,
            "FFmpeg audio conversion timed out.",
            retryable=True,
        ) from exc

    return proc.returncode or 0, stdout, stderr


class FfmpegService:
    def __init__(
        self,
        settings: Settings,
        runner: FfmpegRunnerFn = default_ffmpeg_runner,
    ) -> None:
        self._settings = settings
        self._runner = runner

    async def convert_to_16k_mono_wav(self, input_path: Path, output_wav: Path) -> float:
        if not input_path.is_file():
            raise AppError(ErrorCode.INTERNAL_ERROR, "Input audio file does not exist.")
        if input_path.stat().st_size > self._settings.max_audio_work_bytes:
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                "Input audio exceeds maximum work bytes limit.",
            )

        argv = [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-acodec",
            "pcm_s16le",
            str(output_wav),
        ]
        rc, _, _ = await self._runner(argv, 300.0)
        if rc != 0 or not output_wav.is_file():
            raise AppError(
                ErrorCode.INTERNAL_ERROR,
                "FFmpeg failed to normalize audio into 16 kHz mono WAV.",
            )

        if output_wav.stat().st_size > self._settings.max_audio_work_bytes:
            output_wav.unlink(missing_ok=True)
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                "Normalized WAV exceeds maximum audio work bytes budget.",
            )

        duration_sec = self.inspect_wav_duration(output_wav)
        if duration_sec > self._settings.max_asr_duration_seconds:
            output_wav.unlink(missing_ok=True)
            raise AppError(
                ErrorCode.RESOURCE_LIMIT,
                f"Decoded audio duration ({int(duration_sec)}s) exceeds maximum limit ({self._settings.max_asr_duration_seconds}s).",
            )
        return duration_sec

    @staticmethod
    def inspect_wav_duration(wav_path: Path) -> float:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return 0.0
            return float(frames) / float(rate)
