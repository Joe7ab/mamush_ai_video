# FAILURES.md

# Mamush AI Film — Production Failures and Resolutions

This document records technical, production, and content-related problems encountered while developing **Mamush — ማሙሽ - የእናት ፍቅር (A Mother's Love)**.

It documents what happened, what was attempted, what worked, and what limitations remain.

The purpose is to make the production process reproducible and prevent the same problems from recurring.

## 1. Failure Tracking Summary

| Area | Problem | Status |
|---|---|---|
| API access and quota | Paid video-generation provider returned HTTP 403 | Worked around |
| AI generation resources | Reliance on remote generation and local video processing resources | Partially mitigated |
| Character consistency | Maintaining consistent characters across independently generated shots | Requires visual review |
| Amharic language | Natural narration and accurate language rendering | Generated; requires final review |
| Video generation | No configured AI video-generation endpoint | Worked around |
| FFmpeg subtitles | Windows path parsing failure | Resolved |
| Assembly script | Python `NameError` involving `OUTPUT` | Requires verification |
| Vertical export | Center cropping may cut off important visual elements | Technically generated; visual review required |

## 2. API Access and Quota Problems

### 2.1 Paid video-generation provider returned HTTP 403

**Problem**

An attempt to use a hosted video-generation API through fal.ai resulted in an HTTP 403 response.

The requested service could not be used successfully.

**What was tried**

- Configuring the provider API credentials.
- Attempting to access the video-generation endpoint.
- Investigating the provider response.

**Resolution / workaround**

The project returned to its original requirement of prioritizing free tools.

Instead of paying for another hosted video-generation service, the implementation used an FFmpeg-based Ken Burns animation fallback.

**Result**

All eight video clips were generated successfully.

**Status:** Worked around.

**Lesson learned:** Do not make the production pipeline dependent on a paid API when a free alternative can satisfy the immediate delivery requirements.

### 2.2 AI provider availability and quota considerations

AI image and narration generation depend on external provider availability, account access, and applicable usage limits.

**What was done**

- Used Cloudflare Workers AI for image generation.
- Used Gemini TTS for Amharic narration.
- Generated and retained successful assets locally.
- Used cached or existing assets where available.

**Resolution / workaround**

Keep generated assets locally so a provider interruption does not require regenerating the entire film.

**Status:** Production completed; future quota interruptions remain possible.

**Note:** Exact quota-limit errors and account usage figures were not retained in the production log.

## 3. Memory and Resource Constraints

### 3.1 Video-processing resource requirements

Video assembly requires FFmpeg to process multiple video streams, narration tracks, music, subtitles, and an end-card overlay.

This can create substantial CPU, memory, and temporary-storage requirements.

**What was done**

- Generated video clips separately.
- Kept narration as individual WAV files.
- Used FFmpeg for final assembly.
- Used H.264 encoding with CRF 19 and the medium preset.

**Workaround**

The production workflow is divided into independent stages rather than attempting to generate and process every asset in one operation.

This allows individual stages to be rerun without repeating the entire production process.

**Status:** Pipeline completed successfully.

**Limitation:** No confirmed out-of-memory error or measured peak RAM failure was retained in the available logs. A specific memory failure should only be recorded after confirming its error output.

### 3.2 Asset storage and reuse

Repeated image, narration, and video generation can consume storage and processing resources.

**Mitigation**

- Keep generated assets in their designated directories.
- Reuse existing successful outputs.
- Use caching where supported.
- Avoid regenerating completed shots unnecessarily.

## 4. Character and Visual Consistency

### 4.1 Independently generated keyframes

**Problem**

Each shot is generated independently from its visual prompt.

This can introduce differences in:

- Character facial appearance.
- Clothing and accessories.
- Character proportions.
- Lighting and color grading.
- Background details.
- Scene continuity.

**What was done**

- Used a shared storyboard as the source of truth.
- Generated eight individual keyframes.
- Reviewed the first keyframe before continuing production.
- Generated video clips from the approved keyframe assets.

**Workaround**

Use the same character descriptions, visual references, clothing descriptions, and cinematic style across all shot prompts.

Inspect the generated keyframes in storyboard order before generating the final clips.

**Status:** Eight keyframes and eight video clips generated.

**Remaining limitation:** Successful generation does not guarantee perfect character consistency. Final visual inspection is still necessary.

## 5. Amharic Language and Narration

### 5.1 Amharic narration quality

**Problem**

The film requires emotionally expressive Amharic narration.

AI-generated speech can introduce:

- Incorrect pronunciation.
- Unnatural pauses.
- Incorrect emphasis.
- Inconsistent emotional delivery.
- Differences in pronunciation between narration segments.

**What was done**

- Used Gemini TTS.
- Selected the Charon voice.
- Generated eight separate narration WAV files.
- Used a warm, mature, reflective cinematic delivery direction.
- Kept narration timing in the storyboard.

**Resolution**

All eight narration segments were generated successfully.

The resulting audio files use 24 kHz, mono, 16-bit PCM WAV format.

**Status:** Generated; final linguistic and pronunciation review recommended.

### 5.2 English subtitle synchronization

**Problem**

The film uses Amharic narration and English subtitles.

Subtitle timing must match the spoken narration rather than simply the full duration of each shot.

**What was done**

- Created Amharic and English SRT files.
- Used storyboard narration timestamps.
- Burned English subtitles into the assembled film.

**Result**

The final horizontal export successfully loaded the English subtitle file through FFmpeg's libass filter.

**Status:** Technically resolved; final subtitle timing and readability require visual review.

## 6. Video Generation Limitations

### 6.1 No configured AI video-generation endpoint

**Problem**

A working AI video-generation endpoint was not configured for the project.

The pipeline could not generate true animated character performances through that provider.

**What was tried**

- Checked the video-generation configuration.
- Investigated the available generation options.
- Avoided introducing an additional paid dependency.

**Resolution / workaround**

Implemented an FFmpeg Ken Burns animation fallback using generated still images.

The fallback creates camera movement, zoom, and visual motion from the keyframes.

**Result**

All eight video clips were generated.

**Status:** Worked around.

**Remaining limitation:** Camera movement is not equivalent to true AI-generated character animation.

## 7. FFmpeg and Windows Path Issues

### 7.1 Subtitle path parsing failure

**Problem**

The first horizontal assembly attempt failed because FFmpeg interpreted the Windows drive-letter path incorrectly inside the subtitles filter.

**What was tried**

The subtitle file was initially supplied using an absolute Windows path.

**Resolution**

Changed the FFmpeg subtitle filter to use a project-relative path:

```python
subtitle_path = "assets/output/narration/subtitles_en.srt"
```

**Result**

FFmpeg successfully loaded the subtitle file and completed the horizontal assembly.

**Status:** Resolved.

### 7.2 FFmpeg audio stream selection

**Problem**

The original music MP3 contains an embedded image stream in addition to its audio stream.

Selecting the input without explicitly choosing the audio stream can create mapping problems.

**Resolution**

The assembly script explicitly selects:

```text
[9:a:0]
```

This ensures that the music mix uses the audio stream rather than the embedded cover art.

**Status:** Resolved in the horizontal assembly script.

## 8. Python Assembly Script Errors

### 8.1