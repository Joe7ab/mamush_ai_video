"""Deterministic content-addressed caching utilities for independent pipeline stages."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from mamush_film.config import CacheConfig

logger = logging.getLogger("mamush_film.cache")


def compute_cache_key(stage: str, payload: dict[str, Any]) -> str:
    """Compute a stable SHA-256 hex digest for a stage name and input payload."""
    canonical = json.dumps(
        {"stage": stage, "payload": payload},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StageCache:
    """File-backed metadata and artifact path cache scoped per pipeline stage."""

    def __init__(self, config: CacheConfig) -> None:
        self._enabled = config.enabled
        self._cache_dir = config.cache_dir

    @property
    def enabled(self) -> bool:
        return self._enabled

    def stage_dir(self, stage: str) -> Path:
        return self._cache_dir / stage

    def metadata_path(self, stage: str, cache_key: str) -> Path:
        return self.stage_dir(stage) / f"{cache_key}.json"

    def get_metadata(self, stage: str, cache_key: str) -> dict[str, Any] | None:
        """Return cached stage metadata if caching is enabled and the entry exists."""
        if not self._enabled:
            logger.debug("Cache disabled; skipping lookup for %s:%s", stage, cache_key[:12])
            return None

        path = self.metadata_path(stage, cache_key)
        if not path.is_file():
            logger.debug("Cache miss for %s:%s", stage, cache_key[:12])
            return None

        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        logger.info("Cache hit for %s:%s", stage, cache_key[:12])
        return data

    def put_metadata(self, stage: str, cache_key: str, metadata: dict[str, Any]) -> Path | None:
        """Persist stage metadata under its deterministic cache key."""
        if not self._enabled:
            logger.debug("Cache disabled; skipping write for %s:%s", stage, cache_key[:12])
            return None

        path = self.metadata_path(stage, cache_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(metadata, handle, indent=2, ensure_ascii=False)
        logger.debug("Cached metadata for %s:%s -> %s", stage, cache_key[:12], path)
        return path
