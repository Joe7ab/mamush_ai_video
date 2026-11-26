# """Stage 3 (stub): Image-to-video shot generation via Wan 2.2."""

# from __future__ import annotations

# import logging

# from mamush_film.cache import StageCache
# from mamush_film.config import AppConfig

# logger = logging.getLogger("mamush_film.stages.video")


# def run_video_stage(config: AppConfig, cache: StageCache) -> None:
#     """Placeholder for Wan 2.2 video generation (not yet implemented)."""
#     logger.info("Stage 'video' is not implemented yet; skipping API calls.")
#     raise NotImplementedError(
#         "Stage 3 (Wan 2.2 video generation) is a placeholder and not yet implemented."
#     )


"""Stage 3: Animate storyboard keyframes into video clips using FFmpeg."""

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
) -> int:
    """Create a 1080p Ken Burns clip from one still image."""
    fps = config.film.fps
    width = config.film.width
    height = config.film.height
    frames = round(duration * fps)

    # Alternate slow push-in and pull-out movements.
    if shot_order % 2:
        zoom = "min(1+0.00004*on,1.08)"
    else:
        zoom = "max(1.08-0.00004*on,1.0)"

    video_filter = (
        f"zoompan=z='{zoom}':"
        f"x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':"
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


def run_video_stage(
    config: AppConfig,
    cache: StageCache,
    shot_ids: list[str] | None = None,
    force: bool = False,
) -> None:
    """Generate video clips for selected shots or all storyboard shots."""
    config.paths.ensure_directories()

    storyboard_path = config.paths.storyboard_path
    storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))

    shots = storyboard.get("shots", [])
    selected_ids = set(shot_ids or [])

    selected_shots = [
        shot for shot in shots
        if not selected_ids or shot["shot_id"] in selected_ids
    ]

    if not selected_shots:
        raise ValueError("No matching shots found.")

    found_ids = {shot["shot_id"] for shot in selected_shots}
    missing_ids = selected_ids - found_ids
    if missing_ids:
        raise ValueError(f"Unknown shot IDs: {sorted(missing_ids)}")

    fps = config.film.fps
    width = config.film.width
    height = config.film.height

    logger.info(
        "Starting FFmpeg animation for %d shot(s).",
        len(selected_shots),
    )

    for shot in selected_shots:
        shot_id = shot["shot_id"]
        started = time.perf_counter()
        timestamp = datetime.now(timezone.utc).isoformat()

        keyframe_path = Path(shot["keyframe"]["image_path"])
        if not keyframe_path.is_absolute():
            keyframe_path = config.paths.root_dir / keyframe_path

        if not keyframe_path.is_file():
            raise FileNotFoundError(
                f"Keyframe for {shot_id} not found: {keyframe_path}"
            )

        duration = float(shot["duration_sec"])
        frames = round(duration * fps)

        output_path = config.paths.videos_dir / f"{shot_id}.mp4"
        relative_output = output_path.relative_to(
            config.paths.root_dir
        ).as_posix()

        motion_prompt = shot["video"].get("motion_prompt", "")
        cache_key = compute_cache_key(
            "video",
            {
                "shot_id": shot_id,
                "keyframe_cache_key": shot["keyframe"].get("cache_key"),
                "motion_prompt": motion_prompt,
                "duration_sec": duration,
                "fps": fps,
                "width": width,
                "height": height,
                "method": "ffmpeg_ken_burns",
                "codec": config.ffmpeg.video_codec,
                "crf": config.ffmpeg.crf,
            },
        )

        parameters = {
            "shot_id": shot_id,
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

        if (
            metadata
            and _is_valid_clip(output_path)
        ):
            shot["video"]["clip_path"] = relative_output
            shot["video"]["cache_key"] = cache_key
            shot["video"]["fps"] = fps
            shot["stage_status"]["video"] = "cached"
            _save_storyboard(storyboard_path, storyboard)

            _append_run(
                config.paths.runs_log_path,
                {
                    "timestamp_utc": timestamp,
                    "stage": "video",
                    "operation": "ffmpeg_ken_burns",
                    "status": "cache_hit",
                    "model": "FFmpeg",
                    "parameters": parameters,
                    "seed": None,
                    "latency_ms": round(
                        (time.perf_counter() - started) * 1000, 2
                    ),
                    "usage": None,
                    "cost_usd": 0,
                    "notes": "Reused existing video clip.",
                },
            )
            logger.info("%s: reused cached clip.", shot_id)
            continue

        try:
            logger.info(
                "Animating %s (%.1fs, %d frames)...",
                shot_id,
                duration,
                frames,
            )

            _run_ffmpeg(
                config=config,
                image_path=keyframe_path,
                output_path=output_path,
                duration=duration,
                shot_order=int(shot["order"]),
            )

            shot["video"]["clip_path"] = relative_output
            shot["video"]["cache_key"] = cache_key
            shot["video"]["fps"] = fps
            shot["stage_status"]["video"] = "generated"

            if cache.enabled:
                cache.put_metadata(
                    "video",
                    cache_key,
                    {
                        "shot_id": shot_id,
                        "output_path": relative_output,
                        "method": "ffmpeg_ken_burns",
                        "created_at_utc": timestamp,
                    },
                )

            _save_storyboard(storyboard_path, storyboard)

            latency_ms = round(
                (time.perf_counter() - started) * 1000, 2
            )

            _append_run(
                config.paths.runs_log_path,
                {
                    "timestamp_utc": timestamp,
                    "stage": "video",
                    "operation": "ffmpeg_ken_burns",
                    "status": "success",
                    "model": "FFmpeg",
                    "parameters": parameters,
                    "seed": None,
                    "latency_ms": latency_ms,
                    "usage": None,
                    "cost_usd": 0,
                    "notes": (
                        "Still image animated with a slow camera zoom. "
                        "No AI character motion was generated."
                    ),
                },
            )

            logger.info(
                "%s completed: %s (%.2fs)",
                shot_id,
                relative_output,
                duration,
            )

        except Exception as exc:
            shot["stage_status"]["video"] = "failed"
            _save_storyboard(storyboard_path, storyboard)

            _append_run(
                config.paths.runs_log_path,
                {
                    "timestamp_utc": timestamp,
                    "stage": "video",
                    "operation": "ffmpeg_ken_burns",
                    "status": "error",
                    "model": "FFmpeg",
                    "parameters": parameters,
                    "seed": None,
                    "latency_ms": round(
                        (time.perf_counter() - started) * 1000, 2
                    ),
                    "usage": None,
                    "cost_usd": 0,
                    "notes": str(exc)[:1000],
                },
            )
            logger.exception("Video generation failed for %s", shot_id)
            raise

    logger.info("Video stage finished.")
