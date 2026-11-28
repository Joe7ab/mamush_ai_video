from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
STORYBOARD = ROOT / "storyboards/storyboard.json"

OUTPUT = ROOT / "assets/output/mamush_final_9x16.mp4"
MUSIC = ROOT / "assets/music/mamush_original_score.mp3"
SUBTITLES = ROOT / "assets/output/narration/subtitles_en.srt"

END_CARD = ROOT / "assets/output/end_card_vertical.png"
CONCAT_FILE = ROOT / "assets/output/video_concat_vertical.txt"

WIDTH = 1080
HEIGHT = 1920
FPS = 24


def run(command: list[str]) -> None:
    print("\nRunning:", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, check=True)


def create_end_card() -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("Pillow is required. Install with: python -m pip install Pillow")
        raise

    image = Image.new("RGB", (WIDTH, HEIGHT), (15, 19, 20))
    draw = ImageDraw.Draw(image)

    gold = (213, 173, 102)
    white = (245, 242, 232)
    muted = (185, 185, 175)

    font_path = Path("C:/Windows/Fonts/arial.ttf")
    bold_path = Path("C:/Windows/Fonts/arialbd.ttf")

    def font(path: Path, size: int):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            return ImageFont.load_default()

    title_font = font(bold_path, 104)
    subtitle_font = font(font_path, 58)
    credit_font = font(font_path, 38)

    # Elegant vertical title composition.
    draw.line((220, 690, 860, 690), fill=gold, width=4)

    title = "MAMUSH"
    title_box = draw.textbbox((0, 0), title, font=title_font)
    title_width = title_box[2] - title_box[0]

    draw.text(
        ((WIDTH - title_width) / 2, 770),
        title,
        fill=white,
        font=title_font,
    )

    subtitle = "A MOTHER'S LOVE"
    subtitle_box = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    subtitle_width = subtitle_box[2] - subtitle_box[0]

    draw.text(
        ((WIDTH - subtitle_width) / 2, 940),
        subtitle,
        fill=gold,
        font=subtitle_font,
    )

    draw.line((340, 1080, 740, 1080), fill=gold, width=3)

    disclosure = "AN AI-ASSISTED FILM"
    disclosure_box = draw.textbbox((0, 0), disclosure, font=credit_font)
    disclosure_width = disclosure_box[2] - disclosure_box[0]

    draw.text(
        ((WIDTH - disclosure_width) / 2, 1170),
        disclosure,
        fill=muted,
        font=credit_font,
    )

    END_CARD.parent.mkdir(parents=True, exist_ok=True)
    image.save(END_CARD)

    print(f"Created portrait end card: {END_CARD}")


def main() -> int:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("FFmpeg and ffprobe must be available on PATH.")
        return 1

    data = json.loads(STORYBOARD.read_text(encoding="utf-8"))
    shots = sorted(data["shots"], key=lambda item: item["order"])

    total_duration = float(
        data["project"].get("duration_sec", 81.0)
    )

    if total_duration <= 0:
        total_duration = 81.0

    videos: list[Path] = []
    narration: list[tuple[Path, float]] = []

    print("\n=== CHECKING PORTRAIT ASSETS ===")

    for shot in shots:
        shot_id = shot["shot_id"]

        video_path = ROOT / shot["video"]["vertical_clip_path"]
        audio_path = ROOT / shot["narration"]["audio_path"]
        start = float(shot["narration"]["start_sec"])

        if not video_path.is_file():
            raise FileNotFoundError(
                f"Missing portrait video for {shot_id}: {video_path}"
            )

        if not audio_path.is_file():
            raise FileNotFoundError(
                f"Missing narration for {shot_id}: {audio_path}"
            )

        videos.append(video_path)
        narration.append((audio_path, start))

        print(
            f"{shot_id}: video OK | narration OK | "
            f"starts at {start:.2f}s"
        )

    for path in (MUSIC, SUBTITLES):
        if not path.is_file():
            raise FileNotFoundError(f"Missing required file: {path}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    create_end_card()

    # Build portrait video concat list.
    lines = []

    for video in videos:
        escaped = video.resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{escaped}'")

    CONCAT_FILE.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-f", "concat",
        "-safe", "0",
        "-i", str(CONCAT_FILE),
    ]

    # Inputs 1–8: narration WAV files.
    for audio_path, _ in narration:
        command += ["-i", str(audio_path)]

    # Input 9: original instrumental score.
    command += ["-i", str(MUSIC)]

    # Input 10: portrait end card.
    command += [
        "-loop", "1",
        "-framerate", str(FPS),
        "-i", str(END_CARD),
    ]

    filters = []

    # Portrait subtitles: cinematic, readable, bottom-centered.
    subtitle_path = "assets/output/narration/subtitles_vertical.srt"

    subtitle_filter = (
        f"subtitles=filename='{subtitle_path}':"
        "force_style='FontName=Arial,"
        "FontSize=10,"
        "Bold=1,"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "Outline=1.5,"
        "Shadow=1,"
        "Alignment=2,"
        "MarginL=15,"
        "MarginR=15,"
        "MarginV=35,"
        "WrapStyle=0'"
    )

    filters.append(
        f"[0:v]fps={FPS},"
        f"scale={WIDTH}:{HEIGHT}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1,"
        f"{subtitle_filter}[captioned]"
    )

    # Show the portrait end card during the final three seconds.
    filters.append(
        "[captioned][10:v]overlay=0:0:"
        f"enable='between(t,{total_duration - 3:.2f},{total_duration:.2f})'"
        "[video_out]"
    )

    # Synchronize narration tracks to storyboard start times.
    narration_labels = []

    for index, (_, start) in enumerate(narration):
        input_index = index + 1
        delay_ms = round(start * 1000)
        label = f"n{index}"

        filters.append(
            f"[{input_index}:a]aresample=48000,"
            f"adelay={delay_ms}:all=1,"
            f"atrim=duration={total_duration},"
            f"asetpts=PTS-STARTPTS[{label}]"
        )

        narration_labels.append(f"[{label}]")

    filters.append(
        "".join(narration_labels)
        + f"amix=inputs={len(narration)}:duration=longest:"
        "dropout_transition=0:normalize=0[narration_mix]"
    )

    # Background score at -12 dB, fading out during the final four seconds.
    music_gain = 10 ** (-12.0 / 20.0)

    filters.append(
        f"[9:a:0]aresample=48000,"
        f"volume={music_gain:.8f},"
        f"apad,atrim=duration={total_duration},"
        f"afade=t=out:st={max(0, total_duration - 4):.2f}:d=4"
        "[music_mix]"
    )

    # Final narration/music mix.
    filters.append(
        "[narration_mix][music_mix]"
        "amix=inputs=2:duration=longest:"
        "dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95[audio_out]"
    )

    command += [
        "-filter_complex", ";".join(filters),
        "-map", "[video_out]",
        "-map", "[audio_out]",
        "-t", str(total_duration),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "19",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        str(OUTPUT),
    ]

    print("\n=== PORTRAIT FILM ASSEMBLY ===")
    print(f"Resolution: {WIDTH}x{HEIGHT}")
    print(f"Frame rate: {FPS} fps")
    print(f"Duration: {total_duration:.2f} seconds")
    print(f"Shots: {len(videos)}")
    print("\nThis may take a few minutes...")

    run(command)

    # Verify finished portrait film.
    probe = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration,size",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate",
            "-of", "json",
            str(OUTPUT),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    result = json.loads(probe.stdout)

    print("\n=== PORTRAIT FILM VERIFICATION ===")
    print(json.dumps(result, indent=2))

    video_streams = [
        stream for stream in result["streams"]
        if stream.get("codec_name") == "h264"
    ]

    if not video_streams:
        raise RuntimeError(
            "Final output does not contain an H.264 video stream.")

    video_stream = video_streams[0]
    actual_duration = float(result["format"]["duration"])

    if (
        video_stream["width"] != WIDTH
        or video_stream["height"] != HEIGHT
    ):
        raise RuntimeError("Final portrait video has incorrect dimensions.")

    if abs(actual_duration - total_duration) > 0.5:
        raise RuntimeError(
            f"Unexpected duration: {actual_duration:.2f}s"
        )

    # Preserve the existing horizontal assembly metadata.
    # Store portrait assembly information separately.
    data.setdefault("assembly", {})
    data["assembly"]["vertical_stage_status"] = "generated"
    data["assembly"]["vertical_output_path"] = (
        "assets/output/mamush_final_9x16.mp4"
    )

    STORYBOARD.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("\nSUCCESS!")
    print(f"Portrait film: {OUTPUT}")
    print("Horizontal film metadata and output remain unchanged.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        KeyError,
        ValueError,
        RuntimeError,
    ) as exc:
        print(f"\nPORTRAIT ASSEMBLY FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
