
"""Stage 3: Generate independent landscape and portrait video clips."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mamush_film.cache import StageCache, compute_cache_key
from mamush_film.config import AppConfig

logger = logging.getLogger("mamush_film.stages.video")

FORMATS = {
    "horizontal": {
        "image_field": "image_path",
        "cache_field": "cache_key",
        "clip_field": "clip_path",
        "status_field": "video",
        "cache_status_field": "video",
        "directory_field": "videos_dir",
        "width_field": "width",
        "height_field": "height",
    },
    "vertical": {
        "image_field": "vertical_image_path",
        "cache_field": "vertical_cache_key",
        "clip_field": "vertical_clip_path",
        "status_field": "video_vertical",
        "cache_status_field": "video_vertical",
        "directory_field": "videos_vertical_dir",
        "width_field": "vertical_width",
        "height_field": "vertical_height",
    },
}


def _save_storyboard(path: Path, storyboard: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(storyboard, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_run(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _run_ffmpeg(
    config: AppConfig,
    image_path: Path,
    output_path: Path,
    duration: float,
    shot_order: int,
    width: int,
    height: int,
) -> int:
    """Create an MP4 clip with a slow Ken Burns camera movement."""
    fps = config.film.fps
    frames = round(duration * fps)

    zoom_rate = 0.08 / max(frames - 1, 1)

    if shot_order % 2:
        zoom = f"min(1+{zoom_rate:.8f}*on,1.08)"
    else:
        zoom = f"max(1.08-{zoom_rate:.8f}*on,1.0)"

    video_filter = (
        f"zoompan=z='{zoom}':"
        "x='iw/2-(iw/zoom/2)':"
        "y='ih/2-(ih/zoom/2)':"
        f"d=1:s={width}x{height}:fps={fps},"
        "format=yuv420p"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        config.ffmpeg.binary_path,
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-loop", "1",
        "-framerate", str(fps),
        "-i", str(image_path),
        "-vf", video_filter,
        "-frames:v", str(frames),
        "-an",
        "-c:v", config.ffmpeg.video_codec,
        "-preset", "medium",
        "-crf", str(config.ffmpeg.crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed for {image_path.name}: "
            f"{result.stderr[-2000:]}"
        )

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"FFmpeg did not create {output_path}")

    return frames


def _is_valid_clip(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 10_000


def _resolve_project_path(config: AppConfig, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = config.paths.root_dir / path
    return path


def run_video_stage(
    config: AppConfig,
    cache: StageCache,
    shot_ids: list[str] | None = None,
    force: bool = False,
    formats: list[str] | None = None,
) -> None:
    """
    Generate video clips for selected shots and formats.

    Defaults to horizontal to preserve the original CLI behavior.
    Supported formats: horizontal, vertical.
    """
    config.paths.ensure_directories()

    selected_formats = formats or ["horizontal"]

    invalid_formats = set(selected_formats) - set(FORMATS)
    if invalid_formats:
        raise ValueError(
            f"Unsupported formats: {sorted(invalid_formats)}. "
            f"Choose from: {sorted(FORMATS)}"
        )

    storyboard_path = config.paths.storyboard_path
    storyboard = json.loads(
        storyboard_path.read_text(encoding="utf-8")
    )

    shots = storyboard.get("shots", [])
    requested_ids = set(shot_ids or [])

    selected_shots = [
        shot for shot in shots
        if not requested_ids or shot["shot_id"] in requested_ids
    ]

    if not selected_shots:
        raise ValueError("No matching shots found.")

    found_ids = {shot["shot_id"] for shot in selected_shots}
    missing_ids = requested_ids - found_ids

    if missing_ids:
        raise ValueError(f"Unknown shot IDs: {sorted(missing_ids)}")

    logger.info(
        "Starting video generation: %d shot(s), formats=%s",
        len(selected_shots),
        selected_formats,
    )

    for output_format in selected_formats:
        spec = FORMATS[output_format]

        width = getattr(config.film, spec["width_field"])
        height = getattr(config.film, spec["height_field"])
        fps = config.film.fps

        output_dir = getattr(
            config.paths,
            spec["directory_field"],
        )

        logger.info(
            "Generating %s clips at %dx%d.",
            output_format,
            width,
            height,
        )

        for shot in selected_shots:
            shot_id = shot["shot_id"]
            started = time.perf_counter()
            timestamp = datetime.now(timezone.utc).isoformat()

            keyframe = shot.get("keyframe", {})
            image_value = keyframe.get(spec["image_field"])

            if not image_value:
                raise ValueError(
                    f"{shot_id} has no {spec['image_field']} "
                    f"for {output_format} generation."
                )

            image_path = _resolve_project_path(config, image_value)

            if not image_path.is_file():
                raise FileNotFoundError(
                    f"Keyframe for {shot_id} not found: {image_path}"
                )

            duration = float(shot["duration_sec"])
            frames = round(duration * fps)

            output_path = output_dir / f"{shot_id}.mp4"
            relative_output = output_path.relative_to(
                config.paths.root_dir
            ).as_posix()

            motion_prompt = shot.get("video", {}).get(
                "motion_prompt", ""
            )

            keyframe_cache_key = keyframe.get(
                spec["cache_field"]
            )

            cache_payload = {
                "shot_id": shot_id,
                "format": output_format,
                "keyframe_cache_key": keyframe_cache_key,
                "motion_prompt": motion_prompt,
                "duration_sec": duration,
                "fps": fps,
                "width": width,
                "height": height,
                "method": "ffmpeg_ken_burns",
                "codec": config.ffmpeg.video_codec,
                "crf": config.ffmpeg.crf,
            }

            cache_key = compute_cache_key(
                "video",
                cache_payload,
            )

            parameters = {
                "shot_id": shot_id,
                "format": output_format,
                "method": "ffmpeg_ken_burns",
                "duration_sec": duration,
                "fps": fps,
                "frames": frames,
                "width": width,
                "height": height,
                "output_path": relative_output,
            }

            metadata = (
                cache.get_metadata("video", cache_key)
                if cache.enabled and not force
                else None
            )

            video_data = shot.setdefault("video", {})
            status_field = spec["status_field"]

            if metadata and _is_valid_clip(output_path):
                video_data[spec["clip_field"]] = relative_output
                video_data[f"{output_format}_cache_key"] = cache_key
                video_data[f"{output_format}_fps"] = fps
                shot.setdefault("stage_status", {})[
                    status_field
                ] = "cached"

                _save_storyboard(storyboard_path, storyboard)

                _append_run(
                    config.paths.runs_log_path,
                    {
                        "timestamp_utc": timestamp,
                        "stage": "video",
                        "operation": "ffmpeg_ken_burns",
                        "status": "cache_hit",
                        "format": output_format,
                        "model": "FFmpeg",
                        "parameters": parameters,
                        "seed": None,
                        "latency_ms": round(
                            (time.perf_counter() - started) * 1000,
                            2,
                        ),
                        "usage": None,
                        "cost_usd": 0,
                        "notes": "Reused existing video clip.",
                    },
                )

                logger.info(
                    "%s (%s): reused cached clip.",
                    shot_id,
                    output_format,
                )
                continue

            try:
                logger.info(
                    "Animating %s (%s): %.1fs, %d frames, %dx%d",
                    shot_id,
                    output_format,
                    duration,
                    frames,
                    width,
                    height,
                )

                _run_ffmpeg(
                    config=config,
                    image_path=image_path,
                    output_path=output_path,
                    duration=duration,
                    shot_order=int(shot["order"]),
                    width=width,
                    height=height,
                )

                video_data[spec["clip_field"]] = relative_output
                video_data[f"{output_format}_cache_key"] = cache_key
                video_data[f"{output_format}_fps"] = fps

                shot.setdefault("stage_status", {})[
                    status_field
                ] = "generated"

                if cache.enabled:
                    cache.put_metadata(
                        "video",
                        cache_key,
                        {
                            "shot_id": shot_id,
                            "format": output_format,
                            "output_path": relative_output,
                            "method": "ffmpeg_ken_burns",
                            "created_at_utc": timestamp,
                        },
                    )

                _save_storyboard(storyboard_path, storyboard)

                latency_ms = round(
                    (time.perf_counter() - started) * 1000,
                    2,
                )

                _append_run(
                    config.paths.runs_log_path,
                    {
                        "timestamp_utc": timestamp,
                        "stage": "video",
                        "operation": "ffmpeg_ken_burns",
                        "status": "success",
                        "format": output_format,
                        "model": "FFmpeg",
                        "parameters": parameters,
                        "seed": None,
                        "latency_ms": latency_ms,
                        "usage": None,
                        "cost_usd": 0,
                        "notes": (
                            "Still image animated with a slow camera "
                            "zoom. No AI character motion was generated."
                        ),
                    },
                )

                logger.info(
                    "%s (%s) completed: %s",
                    shot_id,
                    output_format,
                    relative_output,
                )

            except Exception as exc:
                shot.setdefault("stage_status", {})[
                    status_field
                ] = "failed"
                _save_storyboard(storyboard_path, storyboard)

                _append_run(
                    config.paths.runs_log_path,
                    {
                        "timestamp_utc": timestamp,
                        "stage": "video",
                        "operation": "ffmpeg_ken_burns",
                        "status": "error",
                        "format": output_format,
                        "model": "FFmpeg",
                        "parameters": parameters,
                        "seed": None,
                        "latency_ms": round(
                            (time.perf_counter() - started) * 1000,
                            2,
                        ),
                        "usage": None,
                        "cost_usd": 0,
                        "notes": str(exc)[:1000],
                    },
                )

                logger.exception(
                    "Video generation failed for %s (%s)",
                    shot_id,
                    output_format,
                )
                raise

    logger.info("Video stage finished.")
