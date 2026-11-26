"""Stage 1: Amharic screenplay/narration generation, SRT subtitles, and optional TTS via Gemini API."""

from __future__ import annotations

import argparse
import json
import logging
import re
import struct
import time
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from mamush_film.cache import StageCache, compute_cache_key
from mamush_film.config import AppConfig, configure_logging, load_config

logger = logging.getLogger("mamush_film.stages.script_narration")

STAGE_NAME = "script_narration"


class NarrationSegmentSchema(BaseModel):
    """Structured output schema for a single shot's narration segment."""

    shot_id: str = Field(description="Existing shot_id from storyboard.json (e.g. shot_01).")
    text_am: str = Field(description="Natural, emotional Ethiopian Amharic third-person narration.")
    text_en: str = Field(description="Accurate English subtitle preserving emotional meaning.")
    emotion: str = Field(description="Concise tone/delivery cue for the narrator.")
    start_sec: float = Field(description="Segment start timestamp in seconds within the shot window.")
    end_sec: float = Field(description="Segment end timestamp in seconds within the shot window.")


class NarrationPlanSchema(BaseModel):
    """Structured output schema returned by Gemini for the full 8-shot narration."""

    segments: list[NarrationSegmentSchema]


@dataclass(frozen=True)
class WavValidationResult:
    """Validated metadata and non-silence metrics for a generated WAV file."""

    path: Path
    channels: int
    sample_width_bytes: int
    sample_rate_hz: int
    frame_count: int
    duration_sec: float
    peak_amplitude: int


@dataclass(frozen=True)
class Stage1Result:
    """Summary of Stage 1 outputs and execution status."""

    mode: Literal["gemini", "export", "tts"]
    storyboard_path: Path
    narration_text_path: Path
    bilingual_script_path: Path
    english_srt_path: Path
    amharic_srt_path: Path
    audio_paths: list[Path]
    tts_status: str
    runs_log_path: Path
    wav_validations: dict[str, WavValidationResult] | None = None


def _is_retryable_exception(exc: BaseException) -> bool:
    """Retry transient network/timeout/rate-limit/server errors, not auth or schema errors."""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        return code in {408, 429, 500, 502, 503, 504}
    return False


def _format_srt_timestamp(seconds: float) -> str:
    """Format non-negative seconds as SRT timestamp HH:MM:SS,mmm."""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def build_srt(segments: list[dict[str, Any]], text_field: Literal["text_en", "text_am"]) -> str:
    """Render timed narration segments into standard SubRip (.srt) format."""
    blocks: list[str] = []
    for index, seg in enumerate(segments, start=1):
        start_ts = _format_srt_timestamp(float(seg["start_sec"]))
        end_ts = _format_srt_timestamp(float(seg["end_sec"]))
        text = str(seg[text_field]).strip()
        blocks.append(f"{index}\n{start_ts} --> {end_ts}\n{text}")
    return "\n\n".join(blocks) + "\n"


def build_amharic_narration_text(storyboard: dict[str, Any]) -> str:
    """Render a clean Amharic narration script grouped by shot."""
    project = storyboard["project"]
    lines = [
        f"{project['title']} ({project['english_title']})",
        "=" * 48,
        "",
    ]
    for shot in storyboard["shots"]:
        narration = shot["narration"]
        start_s = float(narration["start_sec"])
        end_s = float(narration["end_sec"])
        lines.append(
            f"[{shot['shot_id']} | {start_s:05.2f}s - {end_s:05.2f}s | {narration['emotion']}]"
        )
        lines.append(narration["text_am"].strip())
        lines.append("")
    return "\n".join(lines)


def build_bilingual_markdown(storyboard: dict[str, Any]) -> str:
    """Render a bilingual Markdown script with timings for review."""
    project = storyboard["project"]
    lines = [
        f"# {project['title']} — {project['english_title']}",
        "",
        f"**Logline:** {project['logline']}",
        "",
        "| Shot | Act | Window (s) | Emotion | Amharic Narration | English Subtitle |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for shot in storyboard["shots"]:
        n = shot["narration"]
        window = f"{float(n['start_sec']):.1f}–{float(n['end_sec']):.1f}"
        am = n["text_am"].strip().replace("|", "\\|")
        en = n["text_en"].strip().replace("|", "\\|")
        lines.append(
            f"| `{shot['shot_id']}` | {shot['act']} | {window} | `{n['emotion']}` | {am} | {en} |"
        )
    lines.append("")
    return "\n".join(lines)


def append_run_record(runs_log_path: Path, record: dict[str, Any]) -> None:
    """Append a single JSON line describing a pipeline execution to runs.jsonl."""
    runs_log_path.parent.mkdir(parents=True, exist_ok=True)
    with runs_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _extract_usage_dict(response: Any) -> dict[str, int | None] | None:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    return {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "candidates_tokens": getattr(usage, "candidates_token_count", None),
        "thoughts_tokens": getattr(usage, "thoughts_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }


def _relative_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _load_and_validate_storyboard(storyboard_path: Path, schema_path: Path) -> dict[str, Any]:
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    with storyboard_path.open("r", encoding="utf-8") as handle:
        storyboard = json.load(handle)

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(storyboard), key=lambda err: list(err.absolute_path))
    if errors:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
            for err in errors
        )
        raise ValueError(f"Storyboard failed schema validation: {details}")
    return storyboard


def _write_storyboard(storyboard_path: Path, storyboard: dict[str, Any], schema_path: Path) -> None:
    with schema_path.open("r", encoding="utf-8") as handle:
        schema = json.load(handle)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(storyboard), key=lambda err: list(err.absolute_path))
    if errors:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
            for err in errors
        )
        raise ValueError(f"Updated storyboard failed schema validation: {details}")

    tmp_path = storyboard_path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(storyboard, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    tmp_path.replace(storyboard_path)


def _build_gemini_prompt(storyboard: dict[str, Any]) -> str:
    """Build the creative direction prompt for Gemini from storyboard.json."""
    project = storyboard["project"]
    shot_windows: list[dict[str, Any]] = []
    cursor = 0.0
    for shot in storyboard["shots"]:
        dur = float(shot["duration_sec"])
        shot_windows.append(
            {
                "shot_id": shot["shot_id"],
                "order": shot["order"],
                "act": shot["act"],
                "scene_title": shot["scene_title"],
                "setting": shot["setting"],
                "window_start_sec": round(cursor, 2),
                "window_end_sec": round(cursor + dur, 2),
                "duration_sec": dur,
            }
        )
        cursor += dur

    return (
        f"You are writing the emotional third-person Amharic voice-over narration and "
        f"English subtitles for the {cursor:.1f}-second Ethiopian countryside short film "
        f"'{project['title']}' ({project['english_title']}).\n\n"
        "Story direction:\n"
        "- A warm, mature narrator tells the story of Mamush, a rural Ethiopian mother who "
        "sacrifices daily to help her young son attend school.\n"
        "- The boy faces difficult rural paths, mud, rain, and other obstacles. His mother "
        "supports and encourages him to continue his education.\n"
        "- As he grows older, challenges continue, yet her love remains his strength.\n"
        "- The ending must be emotional and inspiring: a mother's love helps a child "
        "overcome obstacles and build a better future.\n\n"
        "Strict constraints:\n"
        "- Write natural, idiomatic Ethiopian Amharic (no literal machine translation).\n"
        "- Keep speech unhurried (~95-115 Amharic words total across all 8 shots) with "
        "at least 0.4s breathing margin inside each shot window.\n"
        "- Return exactly one segment per shot_id in the exact order provided.\n"
        "- Each segment's start_sec and end_sec must stay strictly within its shot's "
        "[window_start_sec, window_end_sec] and never overlap.\n"
        "- English subtitles must accurately preserve emotional meaning.\n\n"
        f"Shots:\n{json.dumps(shot_windows, indent=2, ensure_ascii=False)}"
    )


def _validate_and_apply_segments(
    storyboard: dict[str, Any],
    segments: list[dict[str, Any]],
    cache_key: str,
    status_label: Literal["generated", "cached"],
) -> None:
    """Validate segment alignment against existing shots and update storyboard in-place."""
    shots = storyboard["shots"]
    if len(segments) != len(shots):
        raise ValueError(f"Expected {len(shots)} narration segments, got {len(segments)}.")

    by_id = {seg["shot_id"]: seg for seg in segments}
    cursor = 0.0
    prev_end = 0.0
    for shot in shots:
        shot_id = shot["shot_id"]
        if shot_id not in by_id:
            raise ValueError(f"Missing narration segment for shot_id '{shot_id}'.")
        seg = by_id[shot_id]
        dur = float(shot["duration_sec"])
        win_start = cursor
        win_end = cursor + dur
        cursor = win_end

        start_sec = round(float(seg["start_sec"]), 2)
        end_sec = round(float(seg["end_sec"]), 2)
        if start_sec >= end_sec:
            raise ValueError(f"{shot_id}: start_sec ({start_sec}) must be < end_sec ({end_sec}).")
        if start_sec < win_start - 1e-6 or end_sec > win_end + 1e-6:
            raise ValueError(
                f"{shot_id}: narration [{start_sec}, {end_sec}] outside shot window [{win_start}, {win_end}]."
            )
        if start_sec < prev_end - 1e-6:
            raise ValueError(f"{shot_id}: narration overlaps previous segment end ({prev_end}).")
        prev_end = end_sec

        shot["narration"]["text_am"] = str(seg["text_am"]).strip()
        shot["narration"]["text_en"] = str(seg["text_en"]).strip()
        shot["narration"]["emotion"] = str(seg["emotion"]).strip()
        shot["narration"]["start_sec"] = start_sec
        shot["narration"]["end_sec"] = end_sec
        shot["narration"]["cache_key"] = cache_key
        shot["stage_status"]["narration"] = status_label


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable_exception),
)
def _call_gemini_script(
    client: genai.Client,
    config: AppConfig,
    prompt: str,
) -> Any:
    gen_config = types.GenerateContentConfig(
        temperature=config.gemini.temperature,
        seed=config.gemini.seed,
        response_mime_type="application/json",
        response_schema=NarrationPlanSchema,
    )
    return client.models.generate_content(
        model=config.gemini.script_model,
        contents=prompt,
        config=gen_config,
    )


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception(_is_retryable_exception),
)
def _call_gemini_tts(
    client: genai.Client,
    config: AppConfig,
    text_am: str,
    emotion: str,
) -> Any:
    """Generate Amharic narration using the verified Gemini 3.8 TTS schema."""

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
                            "style": style,
                        },
                    }
                ],
            }
        ],
        config={
            "response_modalities": ["AUDIO"],
            "speech_config": {
                "voice_config": {
                    "voice": config.gemini.tts_voice,
                }
            },
        },
    )


def validate_wav_file(wav_path: Path) -> WavValidationResult:
    """Validate WAV container headers, audio format, positive duration, and non-zero samples."""
    if not wav_path.is_file() or wav_path.stat().st_size == 0:
        raise ValueError(f"WAV file is missing or empty: {wav_path}")

    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            sample_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
            frames = wav_file.readframes(frame_count)
    except wave.Error as exc:
        raise ValueError(f"Invalid WAV container at {wav_path}: {exc}") from exc

    if channels < 1:
        raise ValueError(f"WAV must have >= 1 channel, got {channels}")
    if sample_width not in {1, 2, 3, 4}:
        raise ValueError(f"Unsupported WAV sample width: {sample_width} bytes")
    if sample_rate < 8000:
        raise ValueError(f"WAV sample rate too low: {sample_rate} Hz")
    if frame_count <= 0 or not frames:
        raise ValueError(f"WAV contains zero audio frames: {wav_path}")

    duration_sec = round(frame_count / float(sample_rate), 4)
    if duration_sec <= 0.0:
        raise ValueError(f"WAV duration must be > 0s, got {duration_sec}")

    peak_amplitude = 0
    if sample_width == 2:
        sample_count = len(frames) // 2
        samples = struct.unpack(f"<{sample_count}h", frames[: sample_count * 2])
        peak_amplitude = max((abs(sample) for sample in samples), default=0)
    elif sample_width == 1:
        peak_amplitude = max((abs(b - 128) for b in frames), default=0)
    else:
        step = sample_width
        for idx in range(0, len(frames) - step + 1, step):
            val = abs(int.from_bytes(frames[idx : idx + step], "little", signed=True))
            if val > peak_amplitude:
                peak_amplitude = val

    if peak_amplitude <= 0:
        raise ValueError(f"WAV contains only digital silence (all zero samples): {wav_path}")

    return WavValidationResult(
        path=wav_path,
        channels=channels,
        sample_width_bytes=sample_width,
        sample_rate_hz=sample_rate,
        frame_count=frame_count,
        duration_sec=duration_sec,
        peak_amplitude=peak_amplitude,
    )


def _save_tts_audio(response: Any, output_wav_path: Path) -> None:
    """Extract audio bytes from Gemini TTS response and save as WAV."""
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            inline_data = getattr(part, "inline_data", None)
            if inline_data is None:
                continue
            raw_bytes = getattr(inline_data, "data", None)
            mime_type = str(getattr(inline_data, "mime_type", "") or "").lower()
            if not raw_bytes:
                continue

            output_wav_path.parent.mkdir(parents=True, exist_ok=True)
            if "wav" in mime_type:
                output_wav_path.write_bytes(raw_bytes)
                return

            # Gemini TTS typically returns linear PCM (e.g. audio/L16;codec=pcm;rate=24000).
            rate_match = re.search(r"rate=(\d+)", mime_type)
            sample_rate = int(rate_match.group(1)) if rate_match else 24000
            if "pcm" in mime_type or "l16" in mime_type or not mime_type:
                with wave.open(str(output_wav_path), "wb") as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(sample_rate)
                    wav_file.writeframes(raw_bytes)
                return

            raise RuntimeError(f"Unsupported TTS audio mime_type '{mime_type}'.")

    raise RuntimeError("Gemini TTS response did not contain audio inline_data.")


def _synthesize_shot_tts(
    config: AppConfig,
    cache: StageCache,
    client: genai.Client,
    shot: dict[str, Any],
    *,
    force: bool = False,
) -> tuple[Path, WavValidationResult]:
    """Synthesize or reuse one shot's Amharic narration WAV."""

    shot_id = shot["shot_id"]
    narration = shot["narration"]

    text_am = str(narration["text_am"]).strip()
    emotion = str(narration["emotion"]).strip()

    if not text_am:
        raise ValueError(f"{shot_id}: Amharic narration is empty.")

    if not emotion:
        raise ValueError(f"{shot_id}: narration emotion is empty.")

    stage = f"{STAGE_NAME}_tts"

    cache_key = compute_cache_key(
        stage,
        {
            "tts_model": config.gemini.tts_model,
            "tts_voice": config.gemini.tts_voice,
            "language": "am",
            "text_am": text_am,
            "emotion": emotion,
            "style": "natural_amharic_warm_mature_cinematic",
        },
    )

    output_path = config.paths.audio_dir / f"{shot_id}_narration.wav"

    # ---------------------------------------------------------
    # Cache lookup
    # ---------------------------------------------------------

    cached = None if force else cache.get_metadata(stage, cache_key)

    if cached is not None:
        cached_path_value = cached.get("audio_path")

        if cached_path_value:
            cached_path = config.paths.root_dir / cached_path_value

            if cached_path.is_file():
                try:
                    validation = validate_wav_file(cached_path)

                    # Reuse only a valid WAV file.
                    narration["audio_path"] = _relative_posix(
                        cached_path,
                        config.paths.root_dir,
                    )
                    narration["cache_key"] = cache_key
                    shot["stage_status"]["narration"] = "generated"

                    append_run_record(
                        config.paths.runs_log_path,
                        {
                            "timestamp_utc": datetime.now(
                                timezone.utc
                            ).isoformat(),
                            "stage": STAGE_NAME,
                            "operation": "gemini_tts_shot",
                            "status": "cache_hit",
                            "model": config.gemini.tts_model,
                            "parameters": {
                                "shot_id": shot_id,
                                "voice": config.gemini.tts_voice,
                                "language": "am",
                                "cache_key": cache_key,
                                "duration_sec": validation.duration_sec,
                                "sample_rate_hz": validation.sample_rate_hz,
                            },
                            "seed": None,
                            "latency_ms": 0.0,
                            "usage": None,
                            "cost_usd": 0.0,
                            "notes": (
                                f"Reused validated cached TTS WAV for {shot_id}."
                            ),
                        },
                    )

                    logger.info(
                        "TTS cache hit: %s — %.2fs",
                        shot_id,
                        validation.duration_sec,
                    )

                    return cached_path, validation

                except ValueError:
                    logger.warning(
                        "Cached WAV for %s failed validation; regenerating.",
                        shot_id,
                    )

    # ---------------------------------------------------------
    # Gemini TTS API call
    # ---------------------------------------------------------

    logger.info(
        "Generating TTS for %s | model=%s | voice=%s",
        shot_id,
        config.gemini.tts_model,
        config.gemini.tts_voice,
    )

    started = time.perf_counter()

    try:
        response = _call_gemini_tts(
            client,
            config,
            text_am,
            emotion,
        )

        _save_tts_audio(response, output_path)

        validation = validate_wav_file(output_path)

    except Exception as exc:
        latency_ms = round(
            (time.perf_counter() - started) * 1000,
            2,
        )

        output_path.unlink(missing_ok=True)

        append_run_record(
            config.paths.runs_log_path,
            {
                "timestamp_utc": datetime.now(
                    timezone.utc
                ).isoformat(),
                "stage": STAGE_NAME,
                "operation": "gemini_tts_shot",
                "status": "error",
                "model": config.gemini.tts_model,
                "parameters": {
                    "shot_id": shot_id,
                    "voice": config.gemini.tts_voice,
                    "language": "am",
                    "emotion": emotion,
                    "cache_key": cache_key,
                },
                "seed": None,
                "latency_ms": latency_ms,
                "usage": None,
                "cost_usd": None,
                "notes": f"TTS generation or WAV validation failed: {exc}",
            },
        )

        raise

    latency_ms = round(
        (time.perf_counter() - started) * 1000,
        2,
    )

    # ---------------------------------------------------------
    # Update this shot only
    # ---------------------------------------------------------

    narration["audio_path"] = _relative_posix(
        output_path,
        config.paths.root_dir,
    )
    narration["cache_key"] = cache_key
    shot["stage_status"]["narration"] = "generated"

    # ---------------------------------------------------------
    # Persist cache metadata
    # ---------------------------------------------------------

    cache.put_metadata(
        stage,
        cache_key,
        {
            "audio_path": narration["audio_path"],
            "model": config.gemini.tts_model,
            "voice": config.gemini.tts_voice,
            "language": "am",
            "sample_rate_hz": validation.sample_rate_hz,
            "channels": validation.channels,
            "duration_sec": validation.duration_sec,
        },
    )

    # ---------------------------------------------------------
    # Record model call
    # ---------------------------------------------------------

    append_run_record(
        config.paths.runs_log_path,
        {
            "timestamp_utc": datetime.now(
                timezone.utc
            ).isoformat(),
            "stage": STAGE_NAME,
            "operation": "gemini_tts_shot",
            "status": "success",
            "model": config.gemini.tts_model,
            "parameters": {
                "shot_id": shot_id,
                "voice": config.gemini.tts_voice,
                "language": "am",
                "emotion": emotion,
                "prompt_style": "natural_amharic_warm_mature_cinematic",
                "sample_rate_hz": validation.sample_rate_hz,
                "channels": validation.channels,
                "sample_width_bytes": validation.sample_width_bytes,
                "frame_count": validation.frame_count,
                "duration_sec": validation.duration_sec,
                "peak_amplitude": validation.peak_amplitude,
                "cache_key": cache_key,
            },
            "seed": None,
            "latency_ms": latency_ms,
            "usage": _extract_usage_dict(response),
            "cost_usd": None,
            "notes": (
                f"Generated and validated Amharic TTS for {shot_id}. "
                f"Duration={validation.duration_sec:.2f}s."
            ),
        },
    )

    logger.info(
        "TTS generated: %s | %.2fs | %d Hz",
        shot_id,
        validation.duration_sec,
        validation.sample_rate_hz,
    )

    return output_path, validation


def run_tts_for_shots(
    config: AppConfig,
    cache: StageCache,
    *,
    shot_ids: list[str] | None = None,
    force: bool = False,
) -> Stage1Result:
    """Run TTS only from approved storyboard narration.

    Does not call Gemini script generation and does not modify
    narration text, English subtitles, emotion, or timing.
    """

    config.paths.ensure_directories()

    storyboard_path = config.paths.storyboard_path
    schema_path = config.paths.schema_path

    storyboard = _load_and_validate_storyboard(
        storyboard_path,
        schema_path,
    )

    # Select shots without changing their narration content.
    all_shots = storyboard["shots"]

    if shot_ids:
        requested_ids = set(shot_ids)
        known_ids = {shot["shot_id"] for shot in all_shots}
        unknown_ids = requested_ids - known_ids

        if unknown_ids:
            raise ValueError(
                f"Unknown shot IDs: {', '.join(sorted(unknown_ids))}"
            )

        selected_shots = [
            shot for shot in all_shots
            if shot["shot_id"] in requested_ids
        ]
    else:
        selected_shots = all_shots

    if not selected_shots:
        raise ValueError("No shots selected for TTS.")

    if not config.gemini.api_key:
        raise ValueError(
            "GEMINI_API_KEY is not configured."
        )

    # Use the same API version as the successful shot_01 test.
    client = genai.Client(
        api_key=config.gemini.api_key,
        http_options=types.HttpOptions(
            api_version="v1beta",
        ),
    )

    audio_paths: list[Path] = []
    validations: dict[str, WavValidationResult] = {}

    for shot in selected_shots:
        shot_id = shot["shot_id"]

        logger.info("Processing TTS: %s", shot_id)

        wav_path, validation = _synthesize_shot_tts(
            config,
            cache,
            client,
            shot,
            force=force,
        )

        audio_paths.append(wav_path)
        validations[shot_id] = validation

        # Save after every completed shot.
        # If a later shot fails, completed shots remain recorded.
        _write_storyboard(
            storyboard_path,
            storyboard,
            schema_path,
        )

    # Export text and subtitle files from the existing storyboard.
    # This does NOT generate or rewrite narration content.
    (
        narration_text_path,
        bilingual_script_path,
        english_srt_path,
        amharic_srt_path,
    ) = export_narration_artifacts(config, storyboard)

    _write_storyboard(
        storyboard_path,
        storyboard,
        schema_path,
    )

    return Stage1Result(
        mode="tts",
        storyboard_path=storyboard_path,
        narration_text_path=narration_text_path,
        bilingual_script_path=bilingual_script_path,
        english_srt_path=english_srt_path,
        amharic_srt_path=amharic_srt_path,
        audio_paths=audio_paths,
        tts_status="generated",
        runs_log_path=config.paths.runs_log_path,
        wav_validations=validations or None,
    )


def export_narration_artifacts(config: AppConfig, storyboard: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    """Write Amharic narration text, bilingual Markdown, and SRT subtitles to output dir."""
    out_dir = config.paths.narration_output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    narration_text_path = out_dir / "narration_am.txt"
    bilingual_script_path = out_dir / "narration_bilingual.md"
    english_srt_path = out_dir / "subtitles_en.srt"
    amharic_srt_path = out_dir / "subtitles_am.srt"

    segments = [shot["narration"] for shot in storyboard["shots"]]

    narration_text_path.write_text(build_amharic_narration_text(storyboard), encoding="utf-8")
    bilingual_script_path.write_text(build_bilingual_markdown(storyboard), encoding="utf-8")
    english_srt_path.write_text(build_srt(segments, "text_en"), encoding="utf-8")
    amharic_srt_path.write_text(build_srt(segments, "text_am"), encoding="utf-8")

    storyboard["assembly"]["subtitle_path"] = _relative_posix(english_srt_path, config.paths.root_dir)
    storyboard["assembly"]["narration_script_path"] = _relative_posix(
        narration_text_path, config.paths.root_dir
    )

    return narration_text_path, bilingual_script_path, english_srt_path, amharic_srt_path


def run_script_narration_stage(
    config: AppConfig,
    cache: StageCache,
    *,
    mode: Literal["gemini", "export"] = "gemini",
    force: bool = False,
    skip_tts: bool = False,
    client: genai.Client | None = None,
) -> Stage1Result:
    """Execute Stage 1: generate or export narration & subtitles and optionally synthesize audio."""
    config.paths.ensure_directories()
    storyboard_path = config.paths.storyboard_path
    schema_path = config.paths.schema_path
    storyboard = _load_and_validate_storyboard(storyboard_path, schema_path)

    if mode == "export":
        t0 = time.perf_counter()
        narration_text_path, bilingual_script_path, english_srt_path, amharic_srt_path = (
            export_narration_artifacts(config, storyboard)
        )
        _write_storyboard(storyboard_path, storyboard, schema_path)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        append_run_record(
            config.paths.runs_log_path,
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "stage": STAGE_NAME,
                "operation": "export_from_storyboard",
                "status": "success",
                "model": None,
                "parameters": {"language": storyboard["project"]["language"]},
                "seed": None,
                "latency_ms": latency_ms,
                "usage": None,
                "cost_usd": None,
                "notes": "Exported Amharic narration and SRT subtitles from storyboard.json without API calls.",
            },
        )
        logger.info("Exported narration artifacts from storyboard.json in %.2f ms", latency_ms)
        return Stage1Result(
            mode="export",
            storyboard_path=storyboard_path,
            narration_text_path=narration_text_path,
            bilingual_script_path=bilingual_script_path,
            english_srt_path=english_srt_path,
            amharic_srt_path=amharic_srt_path,
            audio_paths=[],
            tts_status="skipped_export_mode",
            runs_log_path=config.paths.runs_log_path,
        )

    if client is None:
        config.require_stage_credentials(STAGE_NAME)
        client = genai.Client(
            api_key=config.gemini.api_key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )

    cache_payload = {
        "model": config.gemini.script_model,
        "temperature": config.gemini.temperature,
        "seed": config.gemini.seed,
        "project": storyboard["project"],
        "shots": [
            {
                "shot_id": s["shot_id"],
                "order": s["order"],
                "act": s["act"],
                "duration_sec": s["duration_sec"],
                "scene_title": s["scene_title"],
                "setting": s["setting"],
            }
            for s in storyboard["shots"]
        ],
    }
    cache_key = compute_cache_key(STAGE_NAME, cache_payload)
    cached = None if force else cache.get_metadata(STAGE_NAME, cache_key)

    if cached is not None:
        _validate_and_apply_segments(storyboard, cached["segments"], cache_key, "cached")
        append_run_record(
            config.paths.runs_log_path,
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "stage": STAGE_NAME,
                "operation": "gemini_script",
                "status": "cache_hit",
                "model": config.gemini.script_model,
                "parameters": {
                    "temperature": config.gemini.temperature,
                    "cache_key": cache_key,
                },
                "seed": config.gemini.seed,
                "latency_ms": 0.0,
                "usage": None,
                "cost_usd": 0.0,
                "notes": "Loaded Stage 1 narration segments from local cache.",
            },
        )
    else:
        prompt = _build_gemini_prompt(storyboard)
        t0 = time.perf_counter()
        try:
            response = _call_gemini_script(client, config, prompt)
        except Exception as exc:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            append_run_record(
                config.paths.runs_log_path,
                {
                    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                    "stage": STAGE_NAME,
                    "operation": "gemini_script",
                    "status": "error",
                    "model": config.gemini.script_model,
                    "parameters": {"temperature": config.gemini.temperature},
                    "seed": config.gemini.seed,
                    "latency_ms": latency_ms,
                    "usage": None,
                    "cost_usd": None,
                    "notes": f"Gemini script generation failed: {exc}",
                },
            )
            raise

        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        parsed = getattr(response, "parsed", None)
        if parsed is None:
            raw_text = getattr(response, "text", None) or ""
            plan_data = json.loads(raw_text)
        elif isinstance(parsed, BaseModel):
            plan_data = parsed.model_dump()
        else:
            plan_data = dict(parsed)

        segments = plan_data["segments"]
        _validate_and_apply_segments(storyboard, segments, cache_key, "generated")
        cache.put_metadata(
            STAGE_NAME,
            cache_key,
            {
                "model": config.gemini.script_model,
                "seed": config.gemini.seed,
                "segments": segments,
            },
        )
        append_run_record(
            config.paths.runs_log_path,
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "stage": STAGE_NAME,
                "operation": "gemini_script",
                "status": "success",
                "model": config.gemini.script_model,
                "parameters": {
                    "temperature": config.gemini.temperature,
                    "cache_key": cache_key,
                },
                "seed": config.gemini.seed,
                "latency_ms": latency_ms,
                "usage": _extract_usage_dict(response),
                "cost_usd": None,
                "notes": "Generated Amharic narration and English subtitles via Gemini API.",
            },
        )

    narration_text_path, bilingual_script_path, english_srt_path, amharic_srt_path = (
        export_narration_artifacts(config, storyboard)
    )

    audio_paths: list[Path] = []
    wav_validations: dict[str, WavValidationResult] = {}
    if skip_tts:
        tts_status = "skipped_by_flag"
    else:
        tts_status = "generated"
        for shot in storyboard["shots"]:
            try:
                wav_path, validation = _synthesize_shot_tts(
                    config, cache, client, shot, force=force
                )
                audio_paths.append(wav_path)
                wav_validations[shot["shot_id"]] = validation
            except Exception as exc:
                tts_status = f"unavailable ({exc})"
                logger.warning("Gemini TTS failed for %s: %s", shot["shot_id"], exc)
                break

    _write_storyboard(storyboard_path, storyboard, schema_path)
    return Stage1Result(
        mode="gemini",
        storyboard_path=storyboard_path,
        narration_text_path=narration_text_path,
        bilingual_script_path=bilingual_script_path,
        english_srt_path=english_srt_path,
        amharic_srt_path=amharic_srt_path,
        audio_paths=audio_paths,
        tts_status=tts_status,
        runs_log_path=config.paths.runs_log_path,
        wav_validations=wav_validations or None,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 1: Mamush AI Film narration and subtitles.")
    parser.add_argument(
        "--mode",
        choices=["gemini", "export", "tts"],
        default="gemini",
        help="Use 'gemini' for script+TTS, 'tts' for TTS-only from storyboard.json, or 'export' for offline text/SRT.",
    )
    parser.add_argument(
        "--shot-id",
        action="append",
        dest="shot_ids",
        default=None,
        help="Shot ID to synthesize in --mode tts (repeatable; defaults to all shots).",
    )
    parser.add_argument("--force", action="store_true", help="Bypass Stage 1 cache.")
    parser.add_argument("--skip-tts", action="store_true", help="Skip Gemini TTS audio synthesis.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    configure_logging(config.logging)
    cache = StageCache(config.cache)

    try:
        if args.mode == "tts":
            result = run_tts_for_shots(
                config,
                cache,
                shot_ids=args.shot_ids,
                force=args.force,
            )
        else:
            result = run_script_narration_stage(
                config,
                cache,
                mode=args.mode,
                force=args.force,
                skip_tts=args.skip_tts,
            )
    except Exception as exc:
        logger.error("Stage 1 failed: %s", exc)
        return 1

    logger.info(
        "Stage 1 complete (%s). SRT: %s | TTS: %s",
        result.mode,
        result.english_srt_path,
        result.tts_status,
    )
    if result.wav_validations:
        for sid, wav_info in result.wav_validations.items():
            logger.info(
                "  WAV %s: %s (%d Hz, %d ch, %.2fs, peak=%d)",
                sid,
                wav_info.path,
                wav_info.sample_rate_hz,
                wav_info.channels,
                wav_info.duration_sec,
                wav_info.peak_amplitude,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
