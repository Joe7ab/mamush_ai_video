"""Stage 4 (stub): Original soundtrack generation via ACE-Step."""

from __future__ import annotations

import logging

from mamush_film.cache import StageCache
from mamush_film.config import AppConfig

logger = logging.getLogger("mamush_film.stages.music")


def run_music_stage(config: AppConfig, cache: StageCache) -> None:
    """Placeholder for ACE-Step original music generation (not yet implemented)."""
    logger.info("Stage 'music' is not implemented yet; skipping API calls.")
    raise NotImplementedError(
        "Stage 4 (ACE-Step music generation) is a placeholder and not yet implemented."
    )
