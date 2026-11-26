
"""CLI entry point for Stage 3 video generation."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from mamush_film.cache import StageCache
from mamush_film.config import load_config
from mamush_film.stages.video import run_video_stage


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Animate Mamush AI Film keyframes into video clips."
    )
    parser.add_argument(
        "--shot-id",
        action="append",
        dest="shot_ids",
        help="Shot to animate. Repeat to select multiple shots.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate clips even when cached clips exist.",
    )
    args = parser.parse_args()

    config = load_config(project_root=PROJECT_ROOT)
    cache = StageCache(config.cache)

    logging.basicConfig(
        level=getattr(logging, config.logging.level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    run_video_stage(
        config=config,
        cache=cache,
        shot_ids=args.shot_ids,
        force=args.force,
    )


if __name__ == "__main__":
    main()
