# Mamush AI Film — ማሙሽ - የእናት ፍቅር

Pipeline foundation for **ማሙሽ - የእናት ፍቅር** (*Mamush - A Mother's Love*), a 60–90 second Ethiopian countryside short film telling the story of a rural Ethiopian mother named Mamush who helps her son overcome obstacles on his journey to education.

## Project Status

**Current milestone: Stage 1 — Screenplay, Amharic Narration, and English Subtitles**

Implemented:
- Modular Python package structure under `src/mamush_film/`
- Environment-driven configuration and UTF-8 logging (`src/mamush_film/config.py`)
- Deterministic SHA-256 stage caching helper (`src/mamush_film/cache.py`)
- JSON Schema Draft 2020-12 contract (`schemas/storyboard.schema.json`) with timed `narration.start_sec` / `end_sec` segments and `assembly` script/subtitle paths
- Storyboard with 8 timed shots totaling 72.0 seconds (`storyboards/storyboard.json`)
- Schema + semantic storyboard validation CLI (`scripts/validate_storyboard.py`)
- Stage 1 implementation (`src/mamush_film/stages/script_narration.py`): Gemini API script generation (with `tenacity` retries, caching, structured schema output, `runs.jsonl` provenance logging), offline storyboard export (`--mode export`), SRT/text artifact export under `assets/output/narration/`, and optional Gemini TTS audio synthesis (`gemini-2.5-flash-preview-tts`)

Not yet implemented (placeholders only; no image, video, music, or final assembly runs yet):
- Stage 2: Keyframe generation via **Cloudflare Workers AI** (`src/mamush_film/stages/keyframes.py`)
- Stage 3: Video clip generation via **Wan 2.2** (`src/mamush_film/stages/video.py`)
- Stage 4: Original score generation via **ACE-Step** (`src/mamush_film/stages/music.py`)
- Stage 5: Final audio/video assembly via **FFmpeg** (`src/mamush_film/stages/assembly.py`)

## Architecture

`storyboards/storyboard.json` is the central contract shared across all stages. Each pipeline stage is isolated so it can be run, cached, and re-run independently without regenerating unrelated stages.

```text
mamush-ai-film/
├── .env.example                     # Environment variable template (no hardcoded secrets)
├── .gitignore                       # Ignores .env, generated media, cache, logs, pycache
├── README.md
├── requirements.txt
├── schemas/
│   └── storyboard.schema.json       # JSON Schema (Draft 2020-12) for storyboard.json
├── storyboards/
│   └── storyboard.json              # Central contract with 8 placeholder shots (72.0s)
├── scripts/
│   └── validate_storyboard.py       # Storyboard schema + semantic validator
├── src/
│   └── mamush_film/
│       ├── __init__.py
│       ├── config.py                # Typed config loader, logging, stage credential checks
│       ├── cache.py                 # Content-addressed SHA-256 stage cache helper
│       └── stages/
│           ├── __init__.py
│           ├── script_narration.py  # Stage 1 stub (Gemini API)
│           ├── keyframes.py         # Stage 2 stub (Cloudflare Workers AI)
│           ├── video.py             # Stage 3 stub (Wan 2.2)
│           ├── music.py             # Stage 4 stub (ACE-Step)
│           └── assembly.py          # Stage 5 stub (FFmpeg)
└── assets/
    ├── cache/
    ├── logs/
    └── output/
```

## Setup

1. **Create and activate a virtual environment** (recommended):

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

2. **Install dependencies**:

   ```powershell
   pip install -r requirements.txt
   ```

3. **Configure environment variables (optional for validation, required later for API stages)**:

   ```powershell
   Copy-Item .env.example .env
   ```

   Edit `.env` to provide credentials when you are ready to implement and run individual stages. Never commit `.env` or hardcode keys in source files.

## Validating `storyboard.json`

Validation runs completely offline with zero API calls or media generation.

From the project root (`mamush-ai-film/`):

```powershell
python scripts/validate_storyboard.py
```

Optional flags:
- `--storyboard <path>`: Validate an alternate storyboard JSON file.
- `--schema <path>`: Validate against an alternate JSON Schema file.
- `--check-assets`: Verify that any non-null `audio_path`, `image_path`, `clip_path`, or `output_path` entries exist on disk.
- `--no-log-file`: Print validation logs to stdout only without writing `assets/logs/pipeline.log`.

### What the validator checks
1. **JSON Schema compliance** (`Draft202012Validator`): Required keys, types, enums, regex patterns (`shot_id`, character `id`), and `additionalProperties: false` strictness.
2. **Semantic film checks**:
   - Unique `shot_id` values and strictly sequential `order` values (`1..N`).
   - Total shot duration stays within `project.target_duration_sec` (`60s–90s`).
   - Every shot `narration.start_sec < narration.end_sec`, stays within the shot's cumulative time window, and does not overlap previous segments.
   - Every shot `character_id` exists in top-level `characters`.
   - Every shot `video.fps` matches `project.fps`.
   - `music.duration_sec` aligns with total shot duration (within 2.0s) and `mood_progression` timestamps stay within `music.duration_sec`.

## Running Stage 1 (Screenplay & Narration)

Export narration text and SRT subtitles from `storyboards/storyboard.json` without API calls:

```powershell
$env:PYTHONPATH = "src"
python -m mamush_film.stages.script_narration --mode export
```

Run live Gemini API generation (and optional Amharic TTS via `GEMINI_TTS_MODEL`) after setting `GEMINI_API_KEY` in `.env`:

```powershell
$env:PYTHONPATH = "src"
python -m mamush_film.stages.script_narration --mode gemini
```

Run unit tests:

```powershell
python -m unittest discover -s tests -v
```
# mamush_ai_video
