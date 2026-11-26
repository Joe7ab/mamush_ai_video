"""Single-shot TTS voice test for shot_01 — Mamush AI Film.

Reads the approved Amharic narration for shot_01 from storyboards/storyboard.json,
synthesizes WAV audio via Gemini TTS, validates the result, and records everything
in runs.jsonl. Generates audio for shot_01 ONLY; shots 02–08 are never touched.

Usage (from project root with PYTHONPATH=src):
    python scripts/tts_test_shot01.py [--force]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import struct
import sys
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from jsonschema import Draft202012Validator  # noqa: E402
from tenacity import (  # noqa: E402
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from mamush_film.cache import StageCache, compute_cache_key  # noqa: E402
from mamush_film.config import AppConfig, configure_logging, load_config  # noqa: E402
from mamush_film.stages.script_narration import (  # noqa: E402
    STAGE_NAME,
    WavValidationResult,
    _extract_usage_dict,
    _is_retryable_exception,
    _relative_posix,
    append_run_record,
    validate_wav_file,
)

logger = logging.getLogger("mamush_film.tts_test_shot01")

SHOT_TARGET = "shot_01"


# ---------------------------------------------------------------------------
# TTS API call (with tenacity retries)
# ---------------------------------------------------------------------------


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable_exception),
)
def _call_tts(
    client: genai.Client,
    config: AppConfig,
    text_am: str,
    emotion: str,
) -> object:
    """Generate Amharic narration using Gemini 3.8 Flash TTS."""

    style = (
        "Natural Ethiopian Amharic pronunciation. "
        "Warm, mature, emotionally expressive storytelling. "
        "Gentle and reflective delivery. "
        "Unhurried cinematic pacing. "
        f"Emotional direction: {emotion}."
    )

    return client.models.generate_content(
        model=config.gemini.tts_model,
        contents=[
            {
                "role": "user",
                "parts": [
                    {
                        "text": text_am,
                        "speech_metadata": {
                            "style": style
                        },
                    }
                ],
            }
        ],
        config={
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "voice": config.gemini.tts_voice
                }
            },
        },
    )


# ---------------------------------------------------------------------------
# Audio extraction helpers
# ---------------------------------------------------------------------------


def _extract_and_save_audio(response, output_path: Path) -> dict:
    """Extract Gemini-generated audio and save it as a WAV file."""

    candidates = response.candidates or []

    for candidate in candidates:
        content = candidate.content

        if not content:
            continue

        for part in content.parts or []:
            inline_data = part.inline_data

            if not inline_data or not inline_data.data:
                continue

            audio_bytes = inline_data.data
            mime_type = inline_data.mime_type or ""

            if "audio" not in mime_type.lower():
                continue

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            if "wav" in mime_type.lower():
                output_path.write_bytes(audio_bytes)
            else:
                raise ValueError(
                    f"Unexpected audio MIME type: {mime_type}. "
                    "Expected audio/wav."
                )

            return {
                "path": str(output_path),
                "mime_type": mime_type,
                "bytes": len(audio_bytes),
            }

    raise RuntimeError(
        "Gemini response contained no usable inline audio data."
    )


# ---------------------------------------------------------------------------
# Main test entrypoint
# ---------------------------------------------------------------------------

def run_shot01_tts_test(
    config: AppConfig,
    cache: StageCache,
    *,
    force: bool = False,
) -> tuple[Path, WavValidationResult]:
    """Synthesize, save, and validate Amharic TTS audio for shot_01 only.

    Returns the saved WAV path and a WavValidationResult.
    Raises immediately on any API or validation error.
    """
    # 1. Check API key (do not display the value)
    if not config.gemini.api_key:
        raise ValueError(
            "GEMINI_API_KEY is not set. "
            "Copy .env.example to .env, add your key, and retry."
        )
    logger.info("GEMINI_API_KEY: present ✓")

    # 2. Load and validate storyboard
    schema_path = config.paths.schema_path
    storyboard_path = config.paths.storyboard_path
    with schema_path.open("r", encoding="utf-8") as f:
        schema = json.load(f)
    with storyboard_path.open("r", encoding="utf-8") as f:
        storyboard = json.load(f)

    validator = Draft202012Validator(schema)
    schema_errors = sorted(validator.iter_errors(
        storyboard), key=lambda e: list(e.absolute_path))
    if schema_errors:
        details = "; ".join(
            f"{'.'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in schema_errors
        )
        raise ValueError(
            f"storyboard.json failed schema validation: {details}")

    # 3. Locate shot_01
    shot = next((s for s in storyboard["shots"]
                if s["shot_id"] == SHOT_TARGET), None)
    if shot is None:
        raise ValueError(f"'{SHOT_TARGET}' not found in storyboard.json")

    n = shot["narration"]
    text_am: str = n["text_am"].strip()
    emotion: str = n["emotion"].strip()
    logger.info("shot_01 narration (am): %s", text_am)
    logger.info("shot_01 emotion: %s", emotion)

    # 4. TTS output path and cache key
    config.paths.ensure_directories()
    wav_path = config.paths.audio_dir / f"{SHOT_TARGET}_narration.wav"
    tts_cache_key = compute_cache_key(
        f"{STAGE_NAME}_tts",
        {
            "tts_model": config.gemini.tts_model,
            "tts_voice": config.gemini.tts_voice,
            "language": "am",
            "text_am": text_am,
            "emotion": emotion,
        },
    )

    # 5. Cache hit?
    if not force and wav_path.is_file() and cache.get_metadata(f"{STAGE_NAME}_tts", tts_cache_key):
        logger.info("Cache hit for %s — reusing existing WAV: %s",
                    SHOT_TARGET, wav_path)
        validation = validate_wav_file(wav_path)
        append_run_record(
            config.paths.runs_log_path,
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "stage": STAGE_NAME,
                "operation": "gemini_tts_shot01_test",
                "status": "cache_hit",
                "model": config.gemini.tts_model,
                "parameters": {
                    "shot_id": SHOT_TARGET,
                    "voice": config.gemini.tts_voice,
                    "language_code": "am",
                    "cache_key": tts_cache_key,
                    "sample_rate_hz": validation.sample_rate_hz,
                    "channels": validation.channels,
                    "duration_sec": validation.duration_sec,
                },
                "seed": None,
                "latency_ms": 0.0,
                "usage": None,
                "cost_usd": 0.0,
                "notes": f"Cache hit — reused validated WAV for {SHOT_TARGET}.",
            },
        )
        return wav_path, validation

    # 6. Live Gemini TTS call
    logger.info("TTS model : %s", config.gemini.tts_model)
    logger.info("TTS voice : %s", config.gemini.tts_voice)
    logger.info("Language  : am (Ethiopian Amharic)")
    logger.info("Calling Gemini TTS API for %s …", SHOT_TARGET)

    client = genai.Client(
        api_key=config.gemini.api_key,
        http_options=types.HttpOptions(
            api_version="v1beta"
        ),
    )
    t0 = time.perf_counter()
    try:
        response = _call_tts(client, config, text_am, emotion)
        print("\n========== GEMINI TTS RESPONSE DEBUG ==========")
        print("Response type:", type(response).__name__)

        print("Response fields:", list(response.model_dump(exclude_none=True).keys()))

        candidates = response.candidates or []
        print("Candidate count:", len(candidates))

        for ci, candidate in enumerate(candidates):
            print(f"\nCandidate {ci}")
            print("Finish reason:", candidate.finish_reason)

            content = candidate.content
            if not content:
                print("No content")
                continue

            print("Role:", content.role)
            print("Part count:", len(content.parts or []))

            for pi, part in enumerate(content.parts or []):
                print(f"Part {pi} fields:",
                      list(part.model_dump(exclude_none=True).keys()))

                if part.inline_data:
                    data = part.inline_data.data
                    print("MIME type:", part.inline_data.mime_type)
                    print("Audio bytes:", len(data) if data else 0)
                else:
                    print("No inline audio data")
                    if part.text:
                        print("Text response:", part.text[:500])

        print("================================================\n")
        _extract_and_save_audio(response, wav_path)
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        # Remove partial/corrupt file, if any
        if wav_path.exists():
            wav_path.unlink(missing_ok=True)
        append_run_record(
            config.paths.runs_log_path,
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "stage": STAGE_NAME,
                "operation": "gemini_tts_shot01_test",
                "status": "error",
                "model": config.gemini.tts_model,
                "parameters": {
                    "shot_id": SHOT_TARGET,
                    "voice": config.gemini.tts_voice,
                    "language_code": "am",
                },
                "seed": None,
                "latency_ms": latency_ms,
                "usage": None,
                "cost_usd": None,
                "notes": f"Gemini TTS API call or audio extraction failed: {exc}",
            },
        )
        raise  # exact error propagated to caller — nothing fabricated

    latency_ms = round((time.perf_counter() - t0) * 1000, 2)

    # 7. Validate WAV (raises ValueError on any format/silence/size issue)
    try:
        validation = validate_wav_file(wav_path)
    except ValueError as exc:
        wav_path.unlink(missing_ok=True)
        append_run_record(
            config.paths.runs_log_path,
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "stage": STAGE_NAME,
                "operation": "gemini_tts_shot01_test",
                "status": "error",
                "model": config.gemini.tts_model,
                "parameters": {
                    "shot_id": SHOT_TARGET,
                    "voice": config.gemini.tts_voice,
                    "language_code": "am",
                },
                "seed": None,
                "latency_ms": latency_ms,
                "usage": _extract_usage_dict(response),
                "cost_usd": None,
                "notes": f"WAV validation failed — file deleted: {exc}",
            },
        )
        raise

    # 8. Update storyboard.json only for shot_01
    shot["narration"]["audio_path"] = _relative_posix(
        wav_path, config.paths.root_dir)
    shot["narration"]["cache_key"] = tts_cache_key
    shot["stage_status"]["narration"] = "generated"
    tmp_path = storyboard_path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(storyboard, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(storyboard_path)

    # 9. Persist TTS cache entry
    cache.put_metadata(
        f"{STAGE_NAME}_tts",
        tts_cache_key,
        {
            "audio_path": shot["narration"]["audio_path"],
            "model": config.gemini.tts_model,
            "voice": config.gemini.tts_voice,
            "sample_rate_hz": validation.sample_rate_hz,
            "channels": validation.channels,
            "duration_sec": validation.duration_sec,
        },
    )

    # 10. Record full run metadata
    append_run_record(
        config.paths.runs_log_path,
        {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "stage": STAGE_NAME,
            "operation": "gemini_tts_shot01_test",
            "status": "success",
            "model": config.gemini.tts_model,
            "parameters": {
                "shot_id": SHOT_TARGET,
                "voice": config.gemini.tts_voice,
                "language_code": "am",
                "prompt_style": "natural_amharic_warm_mature_cinematic",
                "sample_rate_hz": validation.sample_rate_hz,
                "channels": validation.channels,
                "sample_width_bytes": validation.sample_width_bytes,
                "frame_count": validation.frame_count,
                "duration_sec": validation.duration_sec,
                "peak_amplitude": validation.peak_amplitude,
                "cache_key": tts_cache_key,
            },
            "seed": None,
            "latency_ms": latency_ms,
            "usage": _extract_usage_dict(response),
            "cost_usd": None,
            "notes": (
                f"Synthesized and validated Amharic TTS WAV for {SHOT_TARGET}. "
                f"Duration={validation.duration_sec:.2f}s, "
                f"sample_rate={validation.sample_rate_hz}Hz, "
                f"peak={validation.peak_amplitude}."
            ),
        },
    )
    return wav_path, validation


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Single-shot TTS test for shot_01 — Mamush AI Film.",
    )
    p.add_argument("--force", action="store_true",
                   help="Bypass cache; re-synthesize even if WAV exists.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(project_root=PROJECT_ROOT)
    configure_logging(config.logging, write_file=True)
    cache = StageCache(config.cache)

    try:
        wav_path, validation = run_shot01_tts_test(
            config, cache, force=args.force)
    except ValueError as exc:
        # Configuration/key/validation error — exact message, no fabrication
        logger.error("TTS test failed: %s", exc)
        return 1
    except Exception as exc:
        # API error — exact message
        logger.error("TTS test failed with API error: %s", exc)
        return 1

    logger.info("=" * 60)
    logger.info("TTS voice test PASSED for %s", SHOT_TARGET)
    logger.info("  Audio file   : %s", wav_path)
    logger.info("  Sample rate  : %d Hz", validation.sample_rate_hz)
    logger.info("  Channels     : %d", validation.channels)
    logger.info("  Bit depth    : %d-bit", validation.sample_width_bytes * 8)
    logger.info("  Frames       : %d", validation.frame_count)
    logger.info("  Duration     : %.2f s", validation.duration_sec)
    logger.info("  Peak amplitude: %d / %d", validation.peak_amplitude,
                (2 ** (validation.sample_width_bytes * 8 - 1)) - 1)
    logger.info("  Runs log     : %s", config.paths.runs_log_path)
    logger.info("=" * 60)
    logger.info("Playback instructions:")
    logger.info("  Windows   : Start-Process '%s'", wav_path)
    logger.info("  ffplay    : ffplay \"%s\"", wav_path)
    logger.info("  ffmpeg    : ffmpeg -i \"%s\" -f null -", wav_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
