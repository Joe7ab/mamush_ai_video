"""Central configuration and logging setup for the Mamush AI Film pipeline.

All credentials are read from environment variables (or an optional .env file).
No API keys are ever hardcoded.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

StageName = Literal["script_narration", "keyframes", "video", "music", "assembly"]

DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    return int(value.strip())


def _parse_optional_int(value: str | None, default: int | None = None) -> int | None:
    if value is None or not value.strip():
        return default
    return int(value.strip())


def _parse_float(value: str | None, default: float) -> float:
    if value is None or not value.strip():
        return default
    return float(value.strip())


@dataclass(frozen=True)
class ProjectPaths:
    """Filesystem layout for storyboards, schemas, intermediate assets, and cache."""

    root_dir: Path
    storyboard_path: Path
    schema_path: Path
    assets_dir: Path
    references_dir: Path
    character_references_dir: Path
    mother_reference_path: Path
    mamush_reference_path: Path
    keyframes_dir: Path
    keyframes_vertical_dir: Path
    videos_dir: Path
    videos_vertical_dir: Path
    audio_dir: Path
    music_dir: Path
    output_dir: Path
    narration_output_dir: Path
    cache_dir: Path
    logs_dir: Path
    runs_log_path: Path

    @classmethod
    def from_root(cls, root_dir: Path) -> ProjectPaths:
        assets_dir = root_dir / "assets"
        output_dir = assets_dir / "output"
        return cls(
            root_dir=root_dir,
            storyboard_path=root_dir / "storyboards" / "storyboard.json",
            schema_path=root_dir / "schemas" / "storyboard.schema.json",
            assets_dir=assets_dir,
            references_dir=assets_dir / "references",
            character_references_dir=assets_dir / "references" / "characters",
            mother_reference_path=(
                assets_dir / "references" / "characters" / "mother_reference.png"
            ),
            mamush_reference_path=(
                assets_dir / "references" / "characters" / "mamush_reference.png"
            ),
            keyframes_dir=assets_dir / "keyframes",
            keyframes_vertical_dir=assets_dir / "keyframes_vertical",
            videos_dir=assets_dir / "videos",
            videos_vertical_dir=assets_dir / "videos_vertical",
            audio_dir=assets_dir / "audio",
            music_dir=assets_dir / "music",
            output_dir=output_dir,
            narration_output_dir=output_dir / "narration",
            cache_dir=assets_dir / "cache",
            logs_dir=assets_dir / "logs",
            runs_log_path=root_dir / "runs.jsonl",
        )

    def ensure_directories(self) -> None:
        """Create workspace output/cache/log directories if they do not exist."""
        for directory in (
            self.references_dir,
            self.character_references_dir,
            self.keyframes_dir,
            self.keyframes_vertical_dir,
            self.videos_dir,
            self.videos_vertical_dir,
            self.audio_dir,
            self.music_dir,
            self.output_dir,
            self.narration_output_dir,
            self.cache_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class FilmSpec:
    """Target technical and editorial specification for the short film."""

    title: str = "ማሙሽ - የእናት ፍቅር"
    english_title: str = "Mamush - A Mother's Love"
    target_min_duration_sec: float = 60.0
    target_max_duration_sec: float = 90.0
    fps: int = 24
    width: int = 1920
    height: int = 1080
    aspect_ratio: str = "16:9"
    vertical_width: int = 1080
    vertical_height: int = 1920
    vertical_aspect_ratio: str = "9:16"
    narration_language: str = "am"


@dataclass(frozen=True)
class GeminiConfig:
    """Settings for Stage 1 (script and narration via Gemini API)."""

    api_key: str | None = field(default=None, repr=False)
    script_model: str = "gemini-2.5-flash"
    tts_model: str = "gemini-3.8-flash-tts"
    tts_voice: str = "Charon"
    temperature: float = 0.6
    seed: int | None = 42
    timeout_sec: int = 60


@dataclass(frozen=True)
class CloudflareConfig:
    """Settings for Stage 2 (keyframe generation via Cloudflare Workers AI)."""

    account_id: str | None = field(default=None, repr=False)
    api_token: str | None = field(default=None, repr=False)
    image_model: str = "@cf/black-forest-labs/flux-2-klein-9b"
    guidance: float = 4.0
    timeout_sec: int = 90

    @property
    def endpoint_url(self) -> str | None:
        if not self.account_id:
            return None
        return (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self.account_id}/ai/run/{self.image_model}"
        )


@dataclass(frozen=True)
class WanConfig:
    """Settings for Stage 3 (video generation via Wan 2.2)."""

    api_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    model_name: str = "wan-2.2"
    timeout_sec: int = 300


@dataclass(frozen=True)
class AceStepConfig:
    """Settings for Stage 4 (music generation via ACE-Step)."""

    api_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    model_name: str = "ace-step"
    timeout_sec: int = 180


@dataclass(frozen=True)
class FFmpegConfig:
    """Settings for Stage 5 (final audio/video assembly via FFmpeg)."""

    binary_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    video_codec: str = "libx264"
    audio_codec: str = "aac"
    crf: int = 18


@dataclass(frozen=True)
class CacheConfig:
    """Settings for deterministic content-addressed stage caching."""

    enabled: bool = True
    cache_dir: Path = DEFAULT_PROJECT_ROOT / "assets" / "cache"


@dataclass(frozen=True)
class LoggingConfig:
    """Settings for structured console and file logging."""

    level: str = "INFO"
    log_file: Path | None = DEFAULT_PROJECT_ROOT / "assets" / "logs" / "pipeline.log"


@dataclass(frozen=True)
class AppConfig:
    """Top-level immutable configuration container for Mamush AI Film."""

    paths: ProjectPaths
    film: FilmSpec
    gemini: GeminiConfig
    cloudflare: CloudflareConfig
    wan: WanConfig
    acestep: AceStepConfig
    ffmpeg: FFmpegConfig
    cache: CacheConfig
    logging: LoggingConfig

    def missing_credentials_for_stage(self, stage: StageName) -> list[str]:
        """Return environment variable names required for a stage that are unset."""
        required_map: dict[StageName, list[tuple[str, str | None]]] = {
            "script_narration": [("GEMINI_API_KEY", self.gemini.api_key)],
            "keyframes": [
                ("CLOUDFLARE_ACCOUNT_ID", self.cloudflare.account_id),
                ("CLOUDFLARE_API_TOKEN", self.cloudflare.api_token),
            ],
            "video": [
                ("WAN_API_URL", self.wan.api_url),
                ("WAN_API_KEY", self.wan.api_key),
            ],
            "music": [
                ("ACESTEP_API_URL", self.acestep.api_url),
                ("ACESTEP_API_KEY", self.acestep.api_key),
            ],
            "assembly": [],
        }
        return [env_name for env_name, value in required_map[stage] if not value]

    def require_stage_credentials(self, stage: StageName) -> None:
        """Raise ValueError if any credential required for the stage is missing."""
        missing = self.missing_credentials_for_stage(stage)
        if missing:
            joined = ", ".join(missing)
            raise ValueError(
                f"Missing required configuration for stage '{stage}': {joined}. "
                "Set them in your environment or .env file."
            )


def load_config(
    project_root: Path | str | None = None,
    env_file: Path | str | None = None,
) -> AppConfig:
    """Load pipeline configuration from environment variables and optional .env file."""
    root = Path(project_root).resolve() if project_root else DEFAULT_PROJECT_ROOT
    dotenv_path = Path(env_file).resolve() if env_file else (root / ".env")
    if dotenv_path.is_file():
        load_dotenv(dotenv_path=dotenv_path, override=False)

    paths = ProjectPaths.from_root(root)
    log_level = os.getenv("MAMUSH_LOG_LEVEL", "INFO").strip().upper() or "INFO"

    return AppConfig(
        paths=paths,
        film=FilmSpec(),
        gemini=GeminiConfig(
            api_key=os.getenv("GEMINI_API_KEY") or None,
            script_model=os.getenv("GEMINI_SCRIPT_MODEL", "gemini-2.5-flash").strip(),
            tts_model=os.getenv("GEMINI_TTS_MODEL", "gemini-3.8-flash-tts").strip(),
            tts_voice=os.getenv("GEMINI_TTS_VOICE", "Charon").strip(),
            temperature=_parse_float(os.getenv("GEMINI_TEMPERATURE"), 0.6),
            seed=_parse_optional_int(os.getenv("GEMINI_SEED"), 42),
            timeout_sec=_parse_int(os.getenv("GEMINI_TIMEOUT_SEC"), 60),
        ),
        cloudflare=CloudflareConfig(
            account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID") or None,
            api_token=os.getenv("CLOUDFLARE_API_TOKEN") or None,
            image_model=os.getenv(
                "CLOUDFLARE_IMAGE_MODEL",
                "@cf/black-forest-labs/flux-2-klein-9b",
            ).strip(),
            guidance=_parse_float(
                os.getenv("CLOUDFLARE_IMAGE_GUIDANCE"), 4.0
            ),
            timeout_sec=_parse_int(os.getenv("CLOUDFLARE_TIMEOUT_SEC"), 90),
        ),
        wan=WanConfig(
            api_url=os.getenv("WAN_API_URL") or None,
            api_key=os.getenv("WAN_API_KEY") or None,
            model_name=os.getenv("WAN_MODEL_NAME", "wan-2.2").strip(),
            timeout_sec=_parse_int(os.getenv("WAN_TIMEOUT_SEC"), 300),
        ),
        acestep=AceStepConfig(
            api_url=os.getenv("ACESTEP_API_URL") or None,
            api_key=os.getenv("ACESTEP_API_KEY") or None,
            model_name=os.getenv("ACESTEP_MODEL_NAME", "ace-step").strip(),
            timeout_sec=_parse_int(os.getenv("ACESTEP_TIMEOUT_SEC"), 180),
        ),
        ffmpeg=FFmpegConfig(
            binary_path=os.getenv("FFMPEG_BINARY", "ffmpeg").strip(),
            ffprobe_path=os.getenv("FFPROBE_BINARY", "ffprobe").strip(),
        ),
        cache=CacheConfig(
            enabled=_parse_bool(os.getenv("MAMUSH_CACHE_ENABLED"), True),
            cache_dir=paths.cache_dir,
        ),
        logging=LoggingConfig(
            level=log_level,
            log_file=paths.logs_dir / "pipeline.log",
        ),
    )


def configure_logging(config: LoggingConfig, write_file: bool = True) -> logging.Logger:
    """Configure UTF-8 logging for console and optional file output."""
    logger = logging.getLogger("mamush_film")
    level = getattr(logging, config.level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Reconfigure stdout stream for UTF-8 on Windows so Amharic titles log cleanly.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except OSError:
            pass

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if write_file and config.log_file is not None:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(config.log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
