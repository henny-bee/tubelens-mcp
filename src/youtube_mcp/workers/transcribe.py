from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from youtube_mcp.services.transcript_normalizer import normalize_segments

ALLOWED_MODELS = {"tiny", "base", "small"}
ALLOWED_COMPUTE_TYPES = {"int8", "int8_float32", "float32"}


def run_transcription(
    *,
    audio_path: Path,
    output_path: Path,
    models_dir: Path,
    model_name: str = "base",
    compute_type: str = "int8",
    cpu_threads: int = 1,
    beam_size: int = 1,
    vad_filter: bool = True,
    language: str | None = None,
) -> dict[str, Any]:
    if model_name not in ALLOWED_MODELS:
        raise ValueError(f"Unsupported Whisper model: {model_name}")
    if compute_type not in ALLOWED_COMPUTE_TYPES:
        raise ValueError(f"Unsupported compute type: {compute_type}")
    if not audio_path.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    os.environ["OMP_NUM_THREADS"] = str(cpu_threads)
    os.environ["OPENBLAS_NUM_THREADS"] = str(cpu_threads)
    os.environ["MKL_NUM_THREADS"] = str(cpu_threads)

    from faster_whisper import WhisperModel

    models_dir.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(
        model_name,
        device="cpu",
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        num_workers=1,
        download_root=str(models_dir),
    )

    segments_iter, info = model.transcribe(
        str(audio_path),
        language=language or None,
        beam_size=beam_size,
        vad_filter=vad_filter,
        word_timestamps=False,
    )

    raw_segments: list[dict[str, Any]] = []
    for seg in segments_iter:
        raw_segments.append(
            {
                "start": float(seg.start),
                "end": float(seg.end),
                "text": str(seg.text),
            }
        )

    normalized = [s.model_dump() for s in normalize_segments(raw_segments)]
    detected_lang = getattr(info, "language", None) or language or "en"

    payload: dict[str, Any] = {
        "language": str(detected_lang),
        "model": model_name,
        "compute_type": compute_type,
        "segments": normalized,
        "created_at": time.time(),
    }

    tmp_output = output_path.with_suffix(".tmp")
    tmp_output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_output, output_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated faster-whisper CPU INT8 worker")
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--models-dir", required=True, type=Path)
    parser.add_argument("--model", default="base", choices=sorted(ALLOWED_MODELS))
    parser.add_argument("--compute-type", default="int8", choices=sorted(ALLOWED_COMPUTE_TYPES))
    parser.add_argument("--threads", default=1, type=int)
    parser.add_argument("--beam-size", default=1, type=int)
    parser.add_argument("--vad", default="true", choices=["true", "false"])
    parser.add_argument("--language", default="")
    args = parser.parse_args(argv)

    run_transcription(
        audio_path=args.audio,
        output_path=args.output,
        models_dir=args.models_dir,
        model_name=args.model,
        compute_type=args.compute_type,
        cpu_threads=max(1, min(args.threads, 8)),
        beam_size=max(1, min(args.beam_size, 5)),
        vad_filter=(args.vad == "true"),
        language=args.language.strip() or None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
