"""Validate storyboard.json against the JSON Schema and film-pipeline invariants."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mamush_film.config import AppConfig, configure_logging, load_config  # noqa: E402


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"JSON file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_schema(storyboard: Any, schema: dict[str, Any]) -> list[str]:
    """Validate storyboard data against the Draft 2020-12 JSON Schema."""
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(storyboard), key=lambda err: list(err.absolute_path))

    messages: list[str] = []
    for err in errors:
        location = ".".join(str(part) for part in err.absolute_path) or "<root>"
        messages.append(f"Schema error at '{location}': {err.message}")
    return messages


def validate_semantics(
    storyboard: dict[str, Any],
    config: AppConfig,
    check_assets: bool = False,
) -> list[str]:
    """Check cross-field invariants that JSON Schema alone cannot express."""
    errors: list[str] = []

    project = storyboard["project"]
    min_dur = float(project["target_duration_sec"]["min"])
    max_dur = float(project["target_duration_sec"]["max"])
    if min_dur > max_dur:
        errors.append(
            f"project.target_duration_sec.min ({min_dur}) cannot exceed max ({max_dur})."
        )

    character_ids = [char["id"] for char in storyboard["characters"]]
    if len(character_ids) != len(set(character_ids)):
        errors.append("Duplicate character 'id' values found in characters list.")
    known_characters = set(character_ids)

    shots: list[dict[str, Any]] = storyboard["shots"]
    seen_shot_ids: set[str] = set()
    orders: list[int] = []
    total_duration = 0.0
    prev_narration_end = 0.0
    project_fps = int(project["fps"])

    for idx, shot in enumerate(shots):
        shot_id = shot["shot_id"]
        if shot_id in seen_shot_ids:
            errors.append(f"Duplicate shot_id '{shot_id}' at shots[{idx}].")
        seen_shot_ids.add(shot_id)

        orders.append(int(shot["order"]))
        shot_window_start = total_duration
        shot_duration = float(shot["duration_sec"])
        total_duration += shot_duration
        shot_window_end = total_duration

        narration = shot["narration"]
        narration_start = float(narration["start_sec"])
        narration_end = float(narration["end_sec"])
        if narration_start >= narration_end:
            errors.append(
                f"Shot '{shot_id}' narration.start_sec ({narration_start}) must be < "
                f"end_sec ({narration_end})."
            )
        if narration_start < shot_window_start - 1e-6 or narration_end > shot_window_end + 1e-6:
            errors.append(
                f"Shot '{shot_id}' narration timing [{narration_start:.2f}s, {narration_end:.2f}s] "
                f"falls outside shot window [{shot_window_start:.2f}s, {shot_window_end:.2f}s]."
            )
        if narration_start < prev_narration_end - 1e-6:
            errors.append(
                f"Shot '{shot_id}' narration.start_sec ({narration_start:.2f}s) overlaps "
                f"previous narration end ({prev_narration_end:.2f}s)."
            )
        prev_narration_end = narration_end

        for char_id in shot["character_ids"]:
            if char_id not in known_characters:
                errors.append(
                    f"Shot '{shot_id}' references unknown character_id '{char_id}'."
                )

        if int(shot["video"]["fps"]) != project_fps:
            errors.append(
                f"Shot '{shot_id}' video.fps ({shot['video']['fps']}) does not match "
                f"project.fps ({project_fps})."
            )

        if check_assets:
            for field_path, value in (
                ("narration.audio_path", narration["audio_path"]),
                ("keyframe.image_path", shot["keyframe"]["image_path"]),
                ("video.clip_path", shot["video"]["clip_path"]),
            ):
                if value is not None:
                    asset_path = (config.paths.root_dir / value).resolve()
                    if not asset_path.is_file():
                        errors.append(
                            f"Shot '{shot_id}' {field_path} points to missing file: {asset_path}"
                        )

    expected_orders = list(range(1, len(shots) + 1))
    if orders != expected_orders:
        errors.append(
            f"Shot 'order' values must be sequential {expected_orders}, got {orders}."
        )

    if not (min_dur <= total_duration <= max_dur):
        errors.append(
            f"Total shot duration ({total_duration:.2f}s) is outside target range "
            f"[{min_dur:.2f}s, {max_dur:.2f}s]."
        )

    music = storyboard["music"]
    music_dur = float(music["duration_sec"])
    if abs(music_dur - total_duration) > 2.0:
        errors.append(
            f"music.duration_sec ({music_dur:.2f}s) differs from total shot duration "
            f"({total_duration:.2f}s) by more than 2.0s."
        )

    for entry in music["mood_progression"]:
        if float(entry["time_sec"]) > music_dur:
            errors.append(
                f"music.mood_progression timestamp ({entry['time_sec']}s) exceeds "
                f"music.duration_sec ({music_dur}s)."
            )

    if check_assets and music["output_path"] is not None:
        music_path = (config.paths.root_dir / music["output_path"]).resolve()
        if not music_path.is_file():
            errors.append(f"music.output_path points to missing file: {music_path}")

    if check_assets:
        assembly = storyboard["assembly"]
        for field_name in ("output_path", "subtitle_path", "narration_script_path"):
            rel_path = assembly.get(field_name)
            if rel_path is not None:
                full_path = (config.paths.root_dir / rel_path).resolve()
                if not full_path.is_file():
                    errors.append(f"assembly.{field_name} points to missing file: {full_path}")

    return errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Mamush AI Film storyboard.json against schema and pipeline rules."
    )
    parser.add_argument(
        "--storyboard",
        type=Path,
        default=None,
        help="Path to storyboard.json (defaults to storyboards/storyboard.json).",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=None,
        help="Path to storyboard.schema.json (defaults to schemas/storyboard.schema.json).",
    )
    parser.add_argument(
        "--check-assets",
        action="store_true",
        help="Also verify that any non-null asset paths exist on disk.",
    )
    parser.add_argument(
        "--no-log-file",
        action="store_true",
        help="Write logs only to stdout without creating assets/logs/pipeline.log.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(project_root=PROJECT_ROOT)
    logger = configure_logging(config.logging, write_file=not args.no_log_file)

    storyboard_path = (args.storyboard or config.paths.storyboard_path).resolve()
    schema_path = (args.schema or config.paths.schema_path).resolve()

    logger.info("Validating storyboard: %s", storyboard_path)
    logger.info("Using schema: %s", schema_path)

    try:
        schema = _load_json(schema_path)
        storyboard = _load_json(storyboard_path)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Failed to load JSON inputs: %s", exc)
        return 1

    schema_errors = validate_schema(storyboard, schema)
    if schema_errors:
        for msg in schema_errors:
            logger.error("%s", msg)
        logger.error("Storyboard validation FAILED with %d schema error(s).", len(schema_errors))
        return 1

    semantic_errors = validate_semantics(storyboard, config, check_assets=args.check_assets)
    if semantic_errors:
        for msg in semantic_errors:
            logger.error("%s", msg)
        logger.error(
            "Storyboard validation FAILED with %d semantic error(s).",
            len(semantic_errors),
        )
        return 1

    shots = storyboard["shots"]
    total_duration = sum(float(shot["duration_sec"]) for shot in shots)
    logger.info(
        "Storyboard validation PASSED | title='%s' | shots=%d | total_duration=%.1fs | fps=%d",
        storyboard["project"]["title"],
        len(shots),
        total_duration,
        storyboard["project"]["fps"],
    )
    for shot in shots:
        logger.info(
            "  %s (#%d, %s): %.1fs - %s",
            shot["shot_id"],
            shot["order"],
            shot["act"],
            shot["duration_sec"],
            shot["scene_title"],
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
