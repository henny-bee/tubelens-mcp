#!/usr/bin/env python3
"""Benchmark script for CPU INT8 faster-whisper ASR subprocess execution.

Generates a synthetic 16 kHz mono WAV file if no input file is supplied, invokes
youtube_mcp.workers.subprocess_runner or youtube_mcp.workers.transcribe (falling back
to direct faster-whisper measurement if needed), and writes wall-clock time, CPU time,
and peak RSS into a structured JSON report.
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import os
import platform
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def generate_synthetic_wav(path: Path, duration_sec: float = 5.0, sample_rate: int = 16000) -> Path:
    """Create a 16 kHz 16-bit mono PCM WAV with speech-like amplitude-modulated harmonics."""
    total_samples = max(1, int(duration_sec * sample_rate))
    frames = bytearray(total_samples * 2)
    for i in range(total_samples):
        t = i / sample_rate
        syllable_env = 0.5 * (1.0 + math.sin(2.0 * math.pi * 3.5 * t))
        carrier = (
            0.6 * math.sin(2.0 * math.pi * 180.0 * t)
            + 0.3 * math.sin(2.0 * math.pi * 540.0 * t)
            + 0.1 * math.sin(2.0 * math.pi * 1200.0 * t)
        )
        # Insert brief pauses every 1.2 seconds so VAD has realistic voice/silence boundaries
        active = 1.0 if (t % 1.2) < 0.9 else 0.02
        sample_val = int(max(-1.0, min(1.0, active * syllable_env * carrier)) * 16000)
        struct.pack_into("<h", frames, i * 2, sample_val)

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(bytes(frames))
    return path


def _get_peak_rss_bytes() -> int | None:
    """Return peak resident set size in bytes where supported by the OS."""
    try:
        import resource

        usage_self = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        usage_children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        peak = max(usage_self, usage_children)
        if sys.platform == "darwin":
            return int(peak)
        return int(peak) * 1024
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            handle = kernel32.GetCurrentProcess()
            fn = getattr(kernel32, "K32GetProcessMemoryInfo", None) or getattr(
                ctypes.windll.psapi, "GetProcessMemoryInfo", None
            )
            if fn:
                fn.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
                    wintypes.DWORD,
                ]
                fn.restype = wintypes.BOOL
                if fn(handle, ctypes.byref(pmc), pmc.cb):
                    return int(pmc.PeakWorkingSetSize)
        except Exception:
            return None
    return None


def _invoke_worker(
    audio_path: Path,
    output_json: Path,
    model_name: str,
    compute_type: str,
    cpu_threads: int,
    beam_size: int,
    vad_enabled: bool,
    models_dir: Path,
    language: str | None,
    timeout_sec: int,
) -> dict[str, Any]:
    """Invoke youtube_mcp.workers.subprocess_runner or youtube_mcp.workers.transcribe."""
    try:
        from youtube_mcp.workers import subprocess_runner  # type: ignore

        for fn_name in ("run_asr_subprocess", "run_transcription_subprocess", "run_transcribe_worker"):
            fn = getattr(subprocess_runner, fn_name, None)
            if callable(fn):
                sig = inspect.signature(fn)
                kwargs: dict[str, Any] = {}
                mapping = {
                    "audio_path": audio_path,
                    "wav_path": audio_path,
                    "input_path": audio_path,
                    "output_path": output_json,
                    "output_json": output_json,
                    "model": model_name,
                    "model_name": model_name,
                    "compute_type": compute_type,
                    "cpu_threads": cpu_threads,
                    "beam_size": beam_size,
                    "vad_enabled": vad_enabled,
                    "vad_filter": vad_enabled,
                    "models_dir": models_dir,
                    "download_root": models_dir,
                    "language": language,
                    "timeout": timeout_sec,
                    "timeout_seconds": timeout_sec,
                }
                for param_name in sig.parameters:
                    if param_name in mapping:
                        kwargs[param_name] = mapping[param_name]
                res = fn(**kwargs)
                if inspect.iscoroutine(res):
                    import asyncio

                    res = asyncio.run(res)
                return {"runner": f"subprocess_runner.{fn_name}", "result": res}
    except ImportError:
        pass
    except Exception as exc:
        return {"runner": "subprocess_runner", "error": str(exc)}

    cmd = [
        sys.executable,
        "-m",
        "youtube_mcp.workers.transcribe",
        "--audio",
        str(audio_path),
        "--output",
        str(output_json),
        "--model",
        model_name,
        "--compute-type",
        compute_type,
        "--threads",
        str(cpu_threads),
        "--beam-size",
        str(beam_size),
        "--models-dir",
        str(models_dir),
        "--vad",
        "true" if vad_enabled else "false",
    ]
    if language:
        cmd.extend(["--language", language])

    env = os.environ.copy()
    existing_py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing_py_path}" if existing_py_path else str(SRC_DIR)
    env["OMP_NUM_THREADS"] = str(cpu_threads)
    env["OPENBLAS_NUM_THREADS"] = str(cpu_threads)
    env["MKL_NUM_THREADS"] = str(cpu_threads)

    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
    if proc.returncode == 0 and output_json.exists():
        payload = json.loads(output_json.read_text(encoding="utf-8"))
        return {
            "runner": "python -m youtube_mcp.workers.transcribe",
            "returncode": 0,
            "segments_count": len(payload.get("segments", [])) if isinstance(payload, dict) else 0,
            "payload": payload,
        }

    try:
        from youtube_mcp.workers import transcribe as transcribe_mod  # type: ignore

        for fn_name in ("transcribe_audio", "run_transcription", "transcribe_file"):
            fn = getattr(transcribe_mod, fn_name, None)
            if callable(fn):
                sig = inspect.signature(fn)
                kwargs = {}
                mapping = {
                    "audio_path": audio_path,
                    "wav_path": audio_path,
                    "output_path": output_json,
                    "model": model_name,
                    "model_name": model_name,
                    "compute_type": compute_type,
                    "cpu_threads": cpu_threads,
                    "beam_size": beam_size,
                    "vad_enabled": vad_enabled,
                    "vad_filter": vad_enabled,
                    "models_dir": models_dir,
                    "download_root": models_dir,
                    "language": language,
                }
                for param_name in sig.parameters:
                    if param_name in mapping:
                        kwargs[param_name] = mapping[param_name]
                res = fn(**kwargs)
                return {"runner": f"transcribe.{fn_name}", "result": res}
    except Exception as exc:
        return {
            "runner": "transcribe_fallback",
            "returncode": proc.returncode,
            "stderr": (proc.stderr or "").strip()[:500],
            "error": str(exc),
        }

    return {
        "runner": "python -m youtube_mcp.workers.transcribe",
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "").strip()[:500],
    }


def run_benchmark(
    audio_path: Path | None = None,
    duration_sec: float = 5.0,
    model_name: str = "base",
    compute_type: str = "int8",
    cpu_threads: int = 1,
    beam_size: int = 1,
    vad_enabled: bool = True,
    models_dir: Path | None = None,
    language: str | None = "en",
    timeout_sec: int = 600,
    dry_run: bool = False,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ytmcp_bench_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        effective_models_dir = models_dir or (tmp_dir / "models")
        effective_models_dir.mkdir(parents=True, exist_ok=True)

        if audio_path is None:
            wav_file = generate_synthetic_wav(tmp_dir / "synthetic_16k_mono.wav", duration_sec=duration_sec)
            synthetic = True
        else:
            wav_file = Path(audio_path).resolve()
            synthetic = False

        with wave.open(str(wav_file), "rb") as wf:
            actual_duration = wf.getnframes() / float(wf.getframerate())
            sample_rate = wf.getframerate()
            channels = wf.getnchannels()

        output_json = tmp_dir / "asr_output.json"

        t_wall_start = time.perf_counter()
        t_cpu_start = time.process_time()

        if dry_run:
            worker_info: dict[str, Any] = {
                "runner": "dry_run",
                "status": "skipped_inference",
            }
        else:
            worker_info = _invoke_worker(
                audio_path=wav_file,
                output_json=output_json,
                model_name=model_name,
                compute_type=compute_type,
                cpu_threads=cpu_threads,
                beam_size=beam_size,
                vad_enabled=vad_enabled,
                models_dir=effective_models_dir,
                language=language,
                timeout_sec=timeout_sec,
            )

        wall_clock_seconds = round(time.perf_counter() - t_wall_start, 4)
        cpu_time_seconds = round(time.process_time() - t_cpu_start, 4)
        peak_rss_bytes = _get_peak_rss_bytes()

        return {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python_version": platform.python_version(),
            },
            "config": {
                "model": model_name,
                "compute_type": compute_type,
                "cpu_threads": cpu_threads,
                "beam_size": beam_size,
                "vad_enabled": vad_enabled,
                "dry_run": dry_run,
            },
            "audio": {
                "synthetic": synthetic,
                "duration_seconds": round(actual_duration, 3),
                "sample_rate_hz": sample_rate,
                "channels": channels,
                "size_bytes": wav_file.stat().st_size,
            },
            "metrics": {
                "wall_clock_seconds": wall_clock_seconds,
                "cpu_time_seconds": cpu_time_seconds,
                "realtime_factor": round(wall_clock_seconds / max(actual_duration, 0.001), 4),
                "peak_rss_bytes": peak_rss_bytes,
                "peak_rss_mib": round(peak_rss_bytes / (1024 * 1024), 2) if peak_rss_bytes else None,
            },
            "worker": worker_info,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark Tubelens MCP faster-whisper CPU INT8 subprocess."
    )
    parser.add_argument("--audio-file", type=Path, default=None, help="Optional existing 16kHz WAV file")
    parser.add_argument(
        "--duration-sec",
        type=float,
        default=5.0,
        help="Synthetic WAV duration in seconds when --audio-file is omitted (default: 5.0)",
    )
    parser.add_argument("--model", default="base", choices=["tiny", "base", "small"])
    parser.add_argument("--compute-type", default="int8", choices=["int8", "int8_float32", "float32"])
    parser.add_argument("--cpu-threads", type=int, default=1)
    parser.add_argument("--beam-size", type=int, default=1)
    parser.add_argument("--no-vad", action="store_true", help="Disable VAD filter")
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path(os.environ.get("DATA_DIR", "/data")) / "models",
        help="Directory for cached Whisper model weights",
    )
    parser.add_argument("--language", default="en")
    parser.add_argument("--timeout-sec", type=int, default=600)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate synthetic WAV and record baseline harness metrics without loading model weights",
    )
    parser.add_argument("--output", type=Path, default=None, help="Optional path to write JSON report")
    args = parser.parse_args(argv)

    models_dir = args.models_dir
    try:
        models_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        models_dir = None

    report = run_benchmark(
        audio_path=args.audio_file,
        duration_sec=args.duration_sec,
        model_name=args.model,
        compute_type=args.compute_type,
        cpu_threads=args.cpu_threads,
        beam_size=args.beam_size,
        vad_enabled=not args.no_vad,
        models_dir=models_dir,
        language=args.language,
        timeout_sec=args.timeout_sec,
        dry_run=args.dry_run,
    )

    formatted = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(formatted + "\n", encoding="utf-8")
    print(formatted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
