
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageOps

from mamush_film.config import AppConfig

logger = logging.getLogger(__name__)

MODEL_STEPS = 4
REFERENCE_MAX_SIZE = (480, 480)
REQUEST_TIMEOUT = 240

FORMATS = {
    "horizontal": {
        "width": 1920,
        "height": 1080,
        "directory": "keyframes_dir",
        "framing": (
            "Create a cinematic 16:9 widescreen landscape composition. "
            "Use intentional cinematic framing, environmental depth, "
            "and balanced character placement. Keep important subjects "
            "inside the frame with room around them."
        ),
    },
    "vertical": {
        "width": 1080,
        "height": 1920,
        "directory": "keyframes_vertical_dir",
        "framing": (
            "Create a native 9:16 vertical portrait composition, "
            "not a cropped landscape image. Compose specifically for "
            "a vertical mobile film. Position the main subject naturally "
            "within the portrait frame. Preserve faces, hands, and "
            "important story details. Leave a little visual breathing "
            "room near the bottom for subtitles, but do not generate "
            "any text or subtitles."
        ),
    },
}


def _project_root(config: AppConfig) -> Path:
    return Path.cwd()


def _storyboard_path(config: AppConfig) -> Path:
    return _project_root(config) / "storyboards" / "storyboard.json"


def _append_run_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _save_storyboard(path: Path, storyboard: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(storyboard, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    temporary.replace(path)


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value

    if isinstance(value, list):
        return " ".join(
            _flatten_text(item)
            for item in value
            if item is not None
        )

    if isinstance(value, dict):
        return " ".join(
            _flatten_text(item)
            for key, item in value.items()
            if key.lower() not in {"id", "character_id"}
            and item is not None
        )

    return ""


def _character_descriptions(
    storyboard: dict[str, Any],
    character_ids: list[str],
) -> str:
    characters = storyboard.get("characters", {})

    descriptions = []

    if isinstance(characters, list):
        characters = {
            item.get("id"): item
            for item in characters
            if isinstance(item, dict) and item.get("id")
        }

    for character_id in character_ids:
        character = characters.get(character_id)

        if character:
            description = _flatten_text(character)
            if description:
                descriptions.append(
                    f"Character {character_id}: {description}"
                )

    return "\n".join(descriptions)


def _reference_hash(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def _prepare_reference(path: Path) -> bytes:
    if not path.is_file():
        raise FileNotFoundError(
            f"Character reference image not found: {path}"
        )

    with Image.open(path) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")
        image.thumbnail(
            REFERENCE_MAX_SIZE,
            Image.Resampling.LANCZOS,
        )

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)

        return output.getvalue()


def _build_prompt(
    storyboard: dict[str, Any],
    shot: dict[str, Any],
    output_format: str,
) -> str:
    keyframe = shot.get("keyframe", {})

    shot_prompt = (
        keyframe.get("prompt")
        or shot.get("visual_prompt")
        or shot.get("prompt")
        or ""
    )

    character_ids = shot.get("character_ids", [])

    if isinstance(character_ids, str):
        character_ids = [character_ids]

    character_text = _character_descriptions(
        storyboard,
        character_ids,
    )

    style_guide = _flatten_text(
        storyboard.get("style_guide", "")
    )

    framing = FORMATS[output_format]["framing"]

    prompt = f"""
FILM: Mamush — ማሙሽ - የእናት ፍቅር
VISUAL STYLE: Cinematic photorealism, emotionally expressive,
natural skin texture, realistic anatomy, beautiful natural lighting.

STORYBOARD SHOT:
{shot_prompt}

CHARACTER CONTINUITY:
{character_text}

VISUAL STYLE GUIDE:
{style_guide}

CHARACTER REFERENCE INSTRUCTIONS:
Reference image 0 is the mother.
Reference image 1 is Mamush, the boy.

Use these images as identity and appearance references.
They are NOT images to copy, paste, reproduce, or place directly
inside the final scene.

Preserve the recognizable facial appearance, age, hairstyle,
skin tone, and defining clothing details of the characters.
Generate a completely new cinematic scene that follows the
storyboard action, location, camera angle, and emotion.

The mother should retain her Habesha/Ethiopian appearance and
traditional clothing identity. The boy should retain his short
dark hair, blue shirt, and colorful beaded necklace.

Only include characters required by this storyboard shot.
Do not introduce the mother or boy into a scene where the
storyboard does not call for them.

SETTING:
A believable rural Ethiopian / Habesha environment where
appropriate to the storyboard. Use authentic architecture,
landscape, materials, and cultural details. Avoid generic
foreign village settings.

COMPOSITION:
{framing}

STRICT NEGATIVE INSTRUCTIONS:
No text, captions, subtitles, lettering, logos, watermarks,
borders, collage layouts, duplicated characters, extra limbs,
deformed hands, distorted faces, plastic skin, cartoon style,
or artificial-looking facial features.

Create one coherent, cinematic film frame.
""".strip()

    return re.sub(r"\n{3,}", "\n\n", prompt)


def _decode_cloudflare_image(response: requests.Response) -> bytes:
    content_type = response.headers.get("content-type", "")

    if content_type.startswith("image/"):
        return response.content

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Cloudflare returned an unexpected response: "
            f"{response.text[:1000]}"
        ) from exc

    result = payload.get("result", payload)

    image_data = (
        result.get("image")
        or result.get("images", [None])[0]
    )

    if not image_data:
        raise RuntimeError(
            "Cloudflare response did not contain an image. "
            f"Response: {json.dumps(payload)[:1500]}"
        )

    if isinstance(image_data, str):
        if image_data.startswith("data:"):
            image_data = image_data.split(",", 1)[1]

        return base64.b64decode(image_data)

    raise RuntimeError("Unsupported Cloudflare image response format.")


def _normalize_image(
    image_bytes: bytes,
    output_path: Path,
    width: int,
    height: int,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(io.BytesIO(image_bytes)) as original:
        image = ImageOps.exif_transpose(original).convert("RGB")

        target_ratio = width / height
        actual_ratio = image.width / image.height

        # Only crop when the generated aspect ratio differs.
        # Keep the crop centered to preserve the intended composition.
        if abs(actual_ratio - target_ratio) > 0.015:
            image = ImageOps.fit(
                image,
                (width, height),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
        else:
            image = image.resize(
                (width, height),
                Image.Resampling.LANCZOS,
            )

        image.save(
            output_path,
            format="PNG",
            optimize=True,
        )


def _cache_key(
    model: str,
    prompt: str,
    shot_id: str,
    output_format: str,
    width: int,
    height: int,
    mother_hash: str,
    mamush_hash: str,
) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "shot_id": shot_id,
        "format": output_format,
        "width": width,
        "height": height,
        "mother_reference": mother_hash,
        "mamush_reference": mamush_hash,
        "generation": "flux2-klein-9b-reference-v1",
    }

    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
    )

    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()


def _generate_image(
    endpoint: str,
    token: str,
    prompt: str,
    mother_bytes: bytes,
    mamush_bytes: bytes,
    width: int,
    height: int,
    guidance: float,
) -> bytes:
    headers = {
        "Authorization": f"Bearer {token}",
    }

    files = {
        "input_image_0": (
            "mother_reference.png",
            mother_bytes,
            "image/png",
        ),
        "input_image_1": (
            "mamush_reference.png",
            mamush_bytes,
            "image/png",
        ),
    }

    data = {
        "prompt": prompt,
        "width": str(width),
        "height": str(height),
        "guidance": str(guidance),
    }

    response = requests.post(
        endpoint,
        headers=headers,
        files=files,
        data=data,
        timeout=REQUEST_TIMEOUT,
    )

    if not response.ok:
        raise RuntimeError(
            f"Cloudflare API error {response.status_code}: "
            f"{response.text[:2000]}"
        )

    return _decode_cloudflare_image(response)


def run_keyframes_stage(
    config: AppConfig,
    cache=None,
    shot_ids: list[str] | None = None,
    force: bool = False,
    formats: list[str] | None = None,
) -> dict[str, Any]:
    """
    Generate storyboard keyframes using both character references.

    By default, generates horizontal and vertical images for every shot.
    Existing output files are skipped unless force=True.
    """

    del cache  # Cache identity is recorded in the run log.

    if formats is None:
        formats = ["horizontal", "vertical"]

    invalid_formats = set(formats) - set(FORMATS)

    if invalid_formats:
        raise ValueError(
            f"Unsupported formats: {sorted(invalid_formats)}"
        )

    config.paths.ensure_directories()

    storyboard_path = _storyboard_path(config)

    if not storyboard_path.is_file():
        raise FileNotFoundError(
            f"Storyboard not found: {storyboard_path}"
        )

    storyboard = json.loads(
        storyboard_path.read_text(encoding="utf-8")
    )

    mother_path = Path(config.paths.mother_reference_path)
    mamush_path = Path(config.paths.mamush_reference_path)

    mother_hash = _reference_hash(mother_path)
    mamush_hash = _reference_hash(mamush_path)

    mother_bytes = _prepare_reference(mother_path)
    mamush_bytes = _prepare_reference(mamush_path)

    cloudflare = config.cloudflare

    endpoint = cloudflare.endpoint_url
    token = cloudflare.api_token
    model = cloudflare.image_model
    guidance = cloudflare.guidance

    if not endpoint or not token:
        raise RuntimeError(
            "Cloudflare endpoint or API token is missing. "
            "Check your existing .env configuration."
        )

    shots = storyboard.get("shots", [])

    if shot_ids:
        requested = set(shot_ids)
        shots = [
            shot for shot in shots
            if shot.get("shot_id", shot.get("id")) in requested
        ]

        found = {
            shot.get("shot_id", shot.get("id"))
            for shot in shots
        }
        missing = requested - found

        if missing:
            raise ValueError(
                f"Shots not found in storyboard: {sorted(missing)}"
            )

    if not shots:
        raise RuntimeError("No storyboard shots selected.")

    log_path = (
        _project_root(config)
        / "assets"
        / "output"
        / "keyframe_generation.jsonl"
    )

    results = {
        "generated": [],
        "skipped": [],
        "failed": [],
    }

    for shot in shots:
        shot_id = shot.get("shot_id", shot.get("id"))

        if not shot_id:
            logger.warning("Skipping storyboard entry without an ID.")
            continue

        shot.setdefault("keyframe", {})
        shot.setdefault("stage_status", {})

        for output_format in formats:
            spec = FORMATS[output_format]

            width = spec["width"]
            height = spec["height"]

            output_dir = getattr(
                config.paths,
                spec["directory"],
            )

            output_path = Path(output_dir) / f"{shot_id}.png"

            prompt = _build_prompt(
                storyboard,
                shot,
                output_format,
            )

            key = _cache_key(
                model,
                prompt,
                shot_id,
                output_format,
                width,
                height,
                mother_hash,
                mamush_hash,
            )

            if output_path.is_file() and not force:
                try:
                    with Image.open(output_path) as existing:
                        existing.verify()

                    logger.info(
                        "Skipping existing %s keyframe: %s",
                        output_format,
                        shot_id,
                    )

                    results["skipped"].append(
                        f"{shot_id}:{output_format}"
                    )
                    continue

                except Exception:
                    logger.warning(
                        "Existing keyframe is invalid; regenerating: %s",
                        output_path,
                    )

            logger.info(
                "Generating %s keyframe for %s (%sx%s)",
                output_format,
                shot_id,
                width,
                height,
            )

            started = time.time()

            try:
                image_bytes = _generate_image(
                    endpoint=endpoint,
                    token=token,
                    prompt=prompt,
                    mother_bytes=mother_bytes,
                    mamush_bytes=mamush_bytes,
                    width=width,
                    height=height,
                    guidance=guidance,
                )

                _normalize_image(
                    image_bytes,
                    output_path,
                    width,
                    height,
                )

                if output_format == "horizontal":
                    shot["keyframe"]["image_path"] = str(
                        output_path.as_posix()
                    )
                    shot["keyframe"]["cache_key"] = key
                    shot["stage_status"]["keyframe"] = "completed"

                else:
                    shot["keyframe"]["vertical_image_path"] = str(
                        output_path.as_posix()
                    )
                    shot["keyframe"]["vertical_cache_key"] = key
                    shot["stage_status"]["keyframe_vertical"] = (
                        "completed"
                    )

                record = {
                    "shot_id": shot_id,
                    "format": output_format,
                    "model": model,
                    "output": str(output_path),
                    "width": width,
                    "height": height,
                    "cache_key": key,
                    "mother_reference_sha256": mother_hash,
                    "mamush_reference_sha256": mamush_hash,
                    "duration_seconds": round(
                        time.time() - started, 2
                    ),
                    "status": "completed",
                    "timestamp": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }

                _append_run_record(log_path, record)
                _save_storyboard(storyboard_path, storyboard)

                results["generated"].append(
                    f"{shot_id}:{output_format}"
                )

                logger.info(
                    "Saved %s keyframe: %s",
                    output_format,
                    output_path,
                )

            except Exception as exc:
                logger.exception(
                    "Failed to generate %s keyframe for %s",
                    output_format,
                    shot_id,
                )

                status_key = (
                    "keyframe"
                    if output_format == "horizontal"
                    else "keyframe_vertical"
                )

                shot["stage_status"][status_key] = "failed"

                record = {
                    "shot_id": shot_id,
                    "format": output_format,
                    "status": "failed",
                    "error": str(exc),
                    "timestamp": datetime.now(
                        timezone.utc
                    ).isoformat(),
                }

                _append_run_record(log_path, record)
                _save_storyboard(storyboard_path, storyboard)

                results["failed"].append(
                    {
                        "shot_id": shot_id,
                        "format": output_format,
                        "error": str(exc),
                    }
                )

    logger.info(
        "Keyframe stage finished: %s generated, %s skipped, %s failed",
        len(results["generated"]),
        len(results["skipped"]),
        len(results["failed"]),
    )

    return results
