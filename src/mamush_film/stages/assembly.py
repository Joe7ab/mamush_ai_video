"""Stage 5 (stub): Final audio/video assembly via FFmpeg."""

from __future__ import annotations

import logging

from mamush_film.cache import StageCache
from mamush_film.config import AppConfig

logger = logging.getLogger("mamush_film.stages.assembly")


def run_assembly_stage(config: AppConfig, cache: StageCache) -> None:
    """Placeholder for FFmpeg final assembly (not yet implemented)."""
    logger.info("Stage 'assembly' is not implemented yet; skipping media assembly.")
    raise NotImplementedError(
        "Stage 5 (FFmpeg assembly) is a placeholder and not yet implemented."
    )
