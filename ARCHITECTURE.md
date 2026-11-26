# ARCHITECTURE.md

# Mamush AI Film — System Architecture

## 1. Project Overview

**Mamush — ማሙሽ - የእናት ፍቅር (A Mother's Love)** is an AI-assisted short-film production project built with Python and FFmpeg.

The system transforms a structured storyboard into a complete cinematic film through a sequence of production stages:

1. Screenplay and narration
2. AI image/keyframe generation
3. Video clip generation
4. Original music production
5. Film assembly and post-production
6. Horizontal and vertical video exports

The architecture is designed to be modular, reproducible, and easy to extend.

## 2. High-Level Architecture

```text
                  STORYBOARD
                      |
                      v
             storyboard.json
                      |
                      v
              CONFIGURATION
                      |
                      v
             PRODUCTION PIPELINE
                      |
       +--------------+--------------+
       |              |              |
       v              v              v
   NARRATION       KEYFRAMES       VIDEO
   GENERATION      GENERATION      GENERATION
       |              |              |
       v              v              v
   Audio WAVs      PNG Images     MP4 Clips
       |              |              |
       +--------------+--------------+
                      |
                      v
                MUSIC PRODUCTION
                      |
                      v
                Original Score
                      |
                      v
                FILM ASSEMBLY
                      |
          +-----------+-----------+
          |                       |
          v                       v
     HORIZONTAL EXPORT       VERTICAL EXPORT
        1920x1080               1080x1920
          |                       |
          +-----------+-----------+
                      |
                      v
                 FINAL FILMS
```

## 3. Technology Stack

| Component | Technology |
|---|---|
| Main programming language | Python |
| Configuration | JSON and environment variables |
| AI narration | Gemini Text-to-Speech |
| AI image generation | Cloudflare Workers AI |
| Video processing | FFmpeg |
| Image processing | Pillow |
| Music production | ACE-Step 1.5 |
| Video encoding | H.264 |
| Audio encoding | AAC |
| Frame rate | 24 FPS |

The production workflow prioritizes free and open-source tools and services with free access.

## 4. Project Directory Structure

```text
mamush-ai-film/
│
├── assets/
│   ├── audio/
│   │   └── shot_XX_narration.wav
│   │
│   ├── keyframes/
│   │   └── shot_XX.png
│   │
│   ├── videos/
│   │   └── shot_XX.mp4
│   │
│   ├── music/
│   │   └── mamush_original_score.mp3
│   │
│   └── output/
│       ├── narration/
│       │   ├── narration_am.txt
│       │   ├── narration_bilingual.md
│       │   ├── subtitles_am.srt
│       │   └── subtitles_en.srt
│       │
│       ├── end_card.png
│       ├── mamush_final.mp4
│       └── mamush_final_9x16.mp4
│
├── scripts/
│   ├── validate_storyboard.py
│   ├── generate_keyframes.py
│   ├── generate_video.py
│   ├── tts_test_shot01.py
│   ├── assemble_film.py
│   └── assemble_film_vertical.py
│
├── src/
│   └── mamush_film/
│       ├── __init__.py
│       ├── config.py
│       ├── cache.py
│       └── stages/
│           ├── script_narration.py
│           ├── keyframes.py
│           ├── video.py
│           ├── music.py
│           └── assembly.py
│
├── storyboards/
│   └── storyboard.json
│
├── .env
├── .env.example
├── .gitignore
├── README.md
├── ARCHITECTURE.md
└── runs.jsonl
```

## 5. Core Components

### 5.1 Storyboard

**Location:** `storyboards/storyboard.json`

The storyboard is the central source of truth for film production.

It defines:

- Project title and metadata.
- Film duration and frame rate.
- Shot ordering and duration.
- Visual descriptions and generation prompts.
- Narration text and timing.
- Audio and video file paths.
- Music generation settings.
- Subtitle and assembly configuration.

All production stages should use the storyboard rather than independently defining shot metadata.

### 5.2 Configuration

**Location:** `src/mamush_film/config.py`

Responsible for loading project configuration and environment variables.

Responsibilities:

- Load API credentials.
- Configure AI model providers.
- Define generation settings.
- Resolve project paths.
- Provide configuration objects to production stages.

Secrets must remain in `.env` and must never be committed to source control.

### 5.3 Cache Management

**Location:** `src/mamush_film/cache.py`

Provides reusable caching functionality to reduce unnecessary regeneration.

The caching system should:

- Identify previously generated assets.
- Avoid repeating successful generation requests.
- Support reproducible production runs.
- Keep track of generated assets and their associated metadata.

### 5.4 Narration Generation

**Location:** `src/mamush_film/stages/script_narration.py`

Generates the film's Amharic narration.

Outputs:

- Individual narration WAV files.
- Complete Amharic narration script.
- Bilingual narration document.
- Amharic subtitles.
- English subtitles.

Narration timing is defined in the storyboard.

### 5.5 Keyframe Generation

**Location:** `src/mamush_film/stages/keyframes.py`

Generates cinematic visual keyframes using the configured AI image provider.

Each storyboard shot produces one keyframe.

Output:

`assets/keyframes/shot_XX.png`

### 5.6 Video Generation

**Location:** `src/mamush_film/stages/video.py`

Transforms generated keyframes into video clips.

The current fallback implementation uses FFmpeg-based Ken Burns camera movement when a true AI video-generation provider is unavailable.

Output:

`assets/videos/shot_XX.mp4`

This approach provides camera movement from still images; it does not create independent character animation.

### 5.7 Music Production

**Location:** `src/mamush_film/stages/music.py`

The original score is generated separately using ACE-Step 1.5.

The score is instrumental and uses Ethiopian-inspired acoustic elements, including:

- Washint
- Krar
- Masinko

Output:

`assets/music/mamush_original_score.mp3`

The storyboard defines the intended mood progression, tempo, and target duration.

### 5.8 Film Assembly

**Location:** `src/mamush_film/stages/assembly.py`

The assembly stage is responsible for combining generated production assets into the final film.

The standalone assembly scripts are:

- `scripts/assemble_film.py`
- `scripts/assemble_film_vertical.py`

Assembly responsibilities include:

1. Reading storyboard shot order.
2. Concatenating video clips.
3. Synchronizing narration.
4. Mixing background music.
5. Adding English subtitles.
6. Adding the AI-assisted film end card.
7. Encoding the final output.

The horizontal and vertical assembly processes should remain independent so that changes to one output do not affect the other.

## 6. Production Pipeline

The production workflow follows these stages:

| Stage | Input | Output |
|---|---|---|
| Storyboard | Story concept | `storyboard.json` |
| Narration | Storyboard | WAV and SRT files |
| Keyframes | Visual prompts | PNG images |
| Video | Keyframes | MP4 clips |
| Music | Music prompt | MP3 score |
| Assembly | Video, audio, subtitles | Final MP4 files |

A stage should only be considered complete when its expected output files have been successfully generated and validated.

## 7. Video Export Specifications

### Horizontal export

| Property | Specification |
|---|---|
| Resolution | 1920 × 1080 |
| Aspect ratio | 16:9 |
| Frame rate | 24 FPS |
| Video codec | H.264 |
| Audio codec | AAC |
| Duration | 81 seconds |
| Output | `assets/output/mamush_final.mp4` |

### Vertical export

| Property | Specification |
|---|---|
| Resolution | 1080 × 1920 |
| Aspect ratio | 9:16 |
| Frame rate | 24 FPS |
| Video codec | H.264 |
| Audio codec | AAC |
| Duration | 81 seconds |
| Output | `assets/output/mamush_final_9x16.mp4` |

Vertical video should be assembled with portrait-specific framing, subtitles, and end-card layout rather than simply reusing horizontal graphics.

## 8. Audio Architecture

The final soundtrack contains two primary audio layers:

**Narration**
- Eight individual WAV files.
- Positioned according to storyboard narration timestamps.
- Mixed into a single narration track.

**Background music**
- Original instrumental score.
- Target gain: -12 dB.
- Padded or trimmed to match the film duration.
- Fades out during the final four seconds.

The final audio mix is limited to prevent excessive output peaks.

## 9. Execution Environment

The project is developed on Windows using Python and Git Bash.

Activate the project environment:

```bash
cd /e/Dev/mamush-ai-film
export PYTHONPATH="$PWD/src"
```

Validate the storyboard:

```bash
python scripts/validate_storyboard.py
```

Generate keyframes:

```bash
python scripts/generate_keyframes.py
```

Generate video clips:

```bash
python scripts/generate_video.py
```

Assemble horizontal video:

```bash
python scripts/assemble_film.py
```

Assemble vertical video:

```bash
python scripts/assemble_film_vertical.py
```

## 10. Output Validation

Use FFprobe to verify the final exports.

Horizontal:

```bash
ffprobe -v error \
  -show_entries format=duration,size \
  -show_entries stream=codec_name,width,height,r_frame_rate \
  -of json assets/output/mamush_final.mp4
```

Vertical:

```bash
ffprobe -v error \
  -show_entries format=duration,size \
  -show_entries stream=codec_name,width,height,r_frame_rate \
  -of json assets/output/mamush_final_9x16.mp4
```

Expected output:

- Horizontal: 1920 × 1080.
- Vertical: 1080 × 1920.
- Both: 81 seconds and 24 FPS.
- Both: H.264 video and AAC audio.

Technical validation should be followed by visual inspection and audio synchronization checks.

## 11. Security and Reliability

- Never commit `.env` or API credentials.
- Keep `.env.example` free of real secrets.
- Preserve generated assets when rerunning unrelated stages.
- Use caching where supported.
- Validate required inputs before assembly.
- Verify output files after encoding.
- Keep production logs and stage status updated.
- Do not mark a stage successful if its output has not been verified.

## 12. Future Improvements

Potential future enhancements:

- True AI-generated character animation.
- Automatic portrait-aware shot reframing.
- Crossfade and cinematic transition support.
- Automatic audio ducking during narration.
- Automated subtitle quality validation.
- Automated scene and output quality checks.
- A single command to execute the entire production pipeline.
- Automated production reports and asset manifests.

---

**Architecture principle:** The storyboard is the single source of truth, production stages are modular, generated assets are reusable, and final exports are independently validated.