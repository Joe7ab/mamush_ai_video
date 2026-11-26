# """Stage 2 (stub): Keyframe image generation via Cloudflare Workers AI."""

# from __future__ import annotations

# import logging

# from mamush_film.cache import StageCache
# from mamush_film.config import AppConfig

# logger = logging.getLogger("mamush_film.stages.keyframes")


# def run_keyframes_stage(config: AppConfig, cache: StageCache) -> None:
#     """Placeholder for Cloudflare Workers AI keyframe generation (not yet implemented)."""
#     logger.info("Stage 'keyframes' is not implemented yet; skipping API calls.")
#     raise NotImplementedError(
#         "Stage 2 (Cloudflare Workers AI keyframes) is a placeholder and not yet implemented."
#     )


"""Stage 2: Generate cinematic storyboard keyframes with Cloudflare Workers AI."""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageOps

from mamush_film.cache import StageCache
from mamush_film.config import AppConfig

logger = logging.getLogger("mamush_film.stages.keyframes")

STAGE_NAME = "keyframes"
MODEL_STEPS = 8
IMAGE_FORMAT = "PNG"


def _append_run_record(path: Path, record: dict[str, Any]) -> None:
    """Append one structured execution record to runs.jsonl."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _save_storyboard(path: Path, storyboard: dict[str, Any]) -> None:
    """Atomically save storyboard updates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".json.tmp")

    temporary_path.write_text(
        json.dumps(storyboard, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _flatten_text(value: Any) -> list[str]:
    """Extract readable text from nested character/style configuration."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []

    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_text(item))
        return result

    if isinstance(value, dict):
        result: list[str] = []
        for key, item in value.items():
            if key.lower() in {"id", "character_id"}:
                continue
            result.extend(_flatten_text(item))
        return result

    return []


def _character_descriptions(
    storyboard: dict[str, Any],
    character_ids: list[str],
) -> list[str]:
    """Find the visual descriptions for the characters in this shot."""
    characters = storyboard.get("characters", [])
    descriptions: list[str] = []

    if isinstance(characters, dict):
        characters = list(characters.values())

    for character in characters:
        if not isinstance(character, dict):
            continue

        character_id = character.get("id") or character.get("character_id")
        if character_id not in character_ids:
            continue

        parts = _flatten_text(character)
        if parts:
            descriptions.append("; ".join(parts))

    return descriptions


def _build_prompt(
    storyboard: dict[str, Any],
    shot: dict[str, Any],
) -> str:
    """Combine shot direction, character continuity, and the visual style bible."""
    keyframe = shot["keyframe"]
    prompt = keyframe["prompt"].replace("[PLACEHOLDER]", "").strip()

    style_parts = _flatten_text(storyboard.get("style_guide", {}))
    character_parts = _character_descriptions(
        storyboard,
        shot.get("character_ids", []),
    )

    sections = [
        prompt,
        "CINEMATIC STYLE: " + "; ".join(style_parts),
    ]

    if character_parts:
        sections.append(
            "CHARACTER CONTINUITY: " + "; ".join(character_parts)
        )

    sections.append(
        "Create one coherent cinematic film frame. "
        "Natural human anatomy, realistic Ethiopian highland environment, "
        "consistent character appearance, believable lighting, "
        "documentary-inspired cinematic realism. "
        "No captions, no typography, no logos, no watermark."
    )

    negative_prompt = keyframe.get("negative_prompt", "").strip()
    if negative_prompt:
        sections.append("AVOID: " + negative_prompt)

    return "\n\n".join(section for section in sections if section.strip())


def _decode_cloudflare_image(response: requests.Response) -> bytes:
    """Extract image bytes from a Cloudflare Workers AI response."""
    content_type = response.headers.get("Content-Type", "").lower()

    if content_type.startswith("image/"):
        return response.content

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Cloudflare returned an unexpected response: "
            f"{response.text[:500]}"
        ) from exc

    if payload.get("success") is False:
        errors = payload.get("errors", [])
        raise RuntimeError(f"Cloudflare API error: {errors}")

    result = payload.get("result") or {}
    encoded_image = result.get("image")

    if not encoded_image:
        raise RuntimeError(
            "Cloudflare response did not contain result.image. "
            f"Response keys: {list(payload.keys())}"
        )

    # Accommodate either raw base64 or a data URI.
    if isinstance(encoded_image, str) and encoded_image.startswith("data:"):
        encoded_image = encoded_image.split(",", 1)[1]

    try:
        return base64.b64decode(encoded_image, validate=True)
    except Exception as exc:
        raise RuntimeError("Could not decode the generated image.") from exc


def _normalize_image(image_bytes: bytes, output_path: Path, width: int, height: int) -> None:
    """Validate, center-crop to target aspect ratio, and resize without distortion."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = output_path.with_suffix(".tmp.png")

    try:
        from io import BytesIO

        with Image.open(BytesIO(image_bytes)) as source:
            source.verify()

        with Image.open(BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")

            image = ImageOps.fit(
                image,
                (width, height),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )

            image.save(temporary_path, format=IMAGE_FORMAT, optimize=True)

        # Verify the saved output before making it the final artifact.
        with Image.open(temporary_path) as check:
            check.verify()

        temporary_path.replace(output_path)

    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _cache_key(model: str, prompt: str, shot_id: str, width: int, height: int) -> str:
    """Build a deterministic key for this generated image."""
    from mamush_film.cache import compute_cache_key

    return compute_cache_key(
        STAGE_NAME,
        {
            "model": model,
            "shot_id": shot_id,
            "prompt": prompt,
            "steps": MODEL_STEPS,
            "output_width": width,
            "output_height": height,
            "output_format": IMAGE_FORMAT,
        },
    )


def _cached_output(
    cache: StageCache,
    cache_key: str,
    output_path: Path,
) -> bool:
    """Return True when a valid cached artifact already exists."""
    if not cache.enabled:
        return False

    metadata = cache.get_metadata(STAGE_NAME, cache_key)
    if not metadata:
        return False

    cached_path = Path(metadata.get("output_path", str(output_path)))
    if not cached_path.is_absolute():
        cached_path = output_path.parent.parent.parent / cached_path

    candidate = cached_path if cached_path.is_file() else output_path
    if not candidate.is_file():
        return False

    try:
        with Image.open(candidate) as image:
            image.verify()
        return candidate.resolve() == output_path.resolve()
    except Exception:
        return False


def run_keyframes_stage(
    config: AppConfig,
    cache: StageCache,
    shot_ids: list[str] | None = None,
    force: bool = False,
) -> None:
    """Generate keyframes for selected shots, or all shots when none are selected."""
    config.require_stage_credentials(STAGE_NAME)
    config.paths.ensure_directories()

    storyboard_path = config.paths.storyboard_path
    if not storyboard_path.is_file():
        raise FileNotFoundError(f"Storyboard not found: {storyboard_path}")

    storyboard = json.loads(storyboard_path.read_text(encoding="utf-8"))
    shots = storyboard.get("shots", [])

    selected_ids = set(shot_ids or [])
    selected_shots = [
        shot for shot in shots
        if not selected_ids or shot.get("shot_id") in selected_ids
    ]

    if not selected_shots:
        raise ValueError(
            f"No matching shots found for: {sorted(selected_ids)}")

    found_ids = {shot["shot_id"] for shot in selected_shots}
    missing_ids = selected_ids - found_ids
    if missing_ids:
        raise ValueError(f"Unknown shot IDs: {sorted(missing_ids)}")

    endpoint = config.cloudflare.endpoint_url
    token = config.cloudflare.api_token

    width = config.film.width
    height = config.film.height
    model = config.cloudflare.image_model

    logger.info(
        "Starting keyframe generation: %d shot(s), model=%s",
        len(selected_shots),
        model,
    )

    for shot in selected_shots:
        shot_id = shot["shot_id"]
        started = time.perf_counter()
        timestamp = datetime.now(timezone.utc).isoformat()

        output_path = config.paths.keyframes_dir / f"{shot_id}.png"
        relative_output_path = output_path.relative_to(
            config.paths.root_dir
        ).as_posix()

        prompt = _build_prompt(storyboard, shot)
        storyboard_seed = shot["keyframe"].get("seed")
        cache_key = _cache_key(model, prompt, shot_id, width, height)

        parameters = {
            "shot_id": shot_id,
            "steps": MODEL_STEPS,
            "output_width": width,
            "output_height": height,
            "output_format": IMAGE_FORMAT,
            "output_path": relative_output_path,
            "seed_supported_by_model": False,
            "prompt": prompt,
        }

        if not force and _cached_output(cache, cache_key, output_path):
            shot["keyframe"]["image_path"] = relative_output_path
            shot["keyframe"]["cache_key"] = cache_key
            shot["stage_status"]["keyframe"] = "cached"
            _save_storyboard(storyboard_path, storyboard)

            _append_run_record(
                config.paths.runs_log_path,
                {
                    "timestamp_utc": timestamp,
                    "stage": STAGE_NAME,
                    "operation": "cloudflare_image",
                    "status": "cache_hit",
                    "model": model,
                    "parameters": parameters,
                    "seed": storyboard_seed,
                    "latency_ms": round(
                        (time.perf_counter() - started) * 1000, 2
                    ),
                    "usage": None,
                    "cost_usd": 0,
                    "notes": "Reused previously generated keyframe.",
                },
            )

            logger.info("%s: using cached keyframe", shot_id)
            continue

        try:
            logger.info("Generating %s...", shot_id)

            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "prompt": prompt,
                    "steps": MODEL_STEPS,
                },
                timeout=config.cloudflare.timeout_sec,
            )

            response.raise_for_status()
            image_bytes = _decode_cloudflare_image(response)

            _normalize_image(
                image_bytes,
                output_path,
                width,
                height,
            )

            shot["keyframe"]["image_path"] = relative_output_path
            shot["keyframe"]["cache_key"] = cache_key
            shot["stage_status"]["keyframe"] = "generated"

            if cache.enabled:
                cache.put_metadata(
                    STAGE_NAME,
                    cache_key,
                    {
                        "shot_id": shot_id,
                        "model": model,
                        "output_path": relative_output_path,
                        "width": width,
                        "height": height,
                        "created_at_utc": timestamp,
                    },
                )

            _save_storyboard(storyboard_path, storyboard)

            latency_ms = round(
                (time.perf_counter() - started) * 1000, 2
            )

            _append_run_record(
                config.paths.runs_log_path,
                {
                    "timestamp_utc": timestamp,
                    "stage": STAGE_NAME,
                    "operation": "cloudflare_image",
                    "status": "success",
                    "model": model,
                    "parameters": parameters,
                    "seed": storyboard_seed,
                    "latency_ms": latency_ms,
                    "usage": None,
                    "cost_usd": None,
                    "notes": (
                        "Generated image normalized to "
                        f"{width}x{height}. "
                        "FLUX Schnell seed parameter is unsupported."
                    ),
                },
            )

            logger.info(
                "%s generated successfully: %s (%d ms)",
                shot_id,
                relative_output_path,
                latency_ms,
            )

        except Exception as exc:
            shot["stage_status"]["keyframe"] = "failed"
            _save_storyboard(storyboard_path, storyboard)

            _append_run_record(
                config.paths.runs_log_path,
                {
                    "timestamp_utc": timestamp,
                    "stage": STAGE_NAME,
                    "operation": "cloudflare_image",
                    "status": "error",
                    "model": model,
                    "parameters": parameters,
                    "seed": storyboard_seed,
                    "latency_ms": round(
                        (time.perf_counter() - started) * 1000, 2
                    ),
                    "usage": None,
                    "cost_usd": None,
                    "notes": str(exc)[:1000],
                },
            )

            logger.exception("Keyframe generation failed for %s", shot_id)
            raise

    logger.info("Keyframe stage finished.")
