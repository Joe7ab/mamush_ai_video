from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path.cwd()
STORYBOARD = ROOT / "storyboards/storyboard.json"
OUTPUT = ROOT / "assets/output/mamush_final.mp4"
MUSIC = ROOT / "assets/music/mamush_original_score.mp3"
SUBTITLES = ROOT / "assets/output/narration/subtitles_en.srt"
END_CARD = ROOT / "assets/output/end_card.png"
CONCAT_FILE = ROOT / "assets/output/video_concat.txt"


def run(command: list[str]) -> None:
    print("\nRunning:", subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, check=True)


def create_end_card() -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("Pillow is required. Install with: python -m pip install Pillow")
        raise

    image = Image.new("RGB", (1920, 1080), (15, 19, 20))
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

    title_font = font(bold_path, 76)
    subtitle_font = font(font_path, 42)
    credit_font = font(font_path, 30)

    draw.line((650, 365, 1270, 365), fill=gold, width=3)

    title = "MAMUSH"
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(
        ((1920 - (title_box[2] - title_box[0])) / 2, 420),
        title,
        fill=white,
        font=title_font,
    )

    subtitle = "A MOTHER'S LOVE"
    subtitle_box = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    draw.text(
        ((1920 - (subtitle_box[2] - subtitle_box[0])) / 2, 525),
        subtitle,
        fill=gold,
        font=subtitle_font,
    )

    disclosure = "AN AI-ASSISTED FILM"
    disclosure_box = draw.textbbox((0, 0), disclosure, font=credit_font)
    draw.text(
        ((1920 - (disclosure_box[2] - disclosure_box[0])) / 2, 690),
        disclosure,
        fill=muted,
        font=credit_font,
    )

    draw.line((800, 770, 1120, 770), fill=gold, width=2)
    END_CARD.parent.mkdir(parents=True, exist_ok=True)
    image.save(END_CARD)
    print(f"Created end card: {END_CARD}")


def main() -> int:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("FFmpeg and ffprobe must be available on PATH.")
        return 1

    data = json.loads(STORYBOARD.read_text(encoding="utf-8"))
    shots = sorted(data["shots"], key=lambda item: item["order"])
    total_duration = float(data["project"].get("duration_sec", 81.0))
    if total_duration <= 0:
        total_duration = 81.0

    videos: list[Path] = []
    narration: list[tuple[Path, float]] = []

    for shot in shots:
        video_path = ROOT / shot["video"]["clip_path"]
        audio_path = ROOT / shot["narration"]["audio_path"]
        start = float(shot["narration"]["start_sec"])

        if not video_path.is_file():
            raise FileNotFoundError(f"Missing video: {video_path}")
        if not audio_path.is_file():
            raise FileNotFoundError(f"Missing narration: {audio_path}")

        videos.append(video_path)
        narration.append((audio_path, start))

    for path in (MUSIC, SUBTITLES):
        if not path.is_file():
            raise FileNotFoundError(f"Missing required file: {path}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    create_end_card()

    # FFmpeg concat demuxer: keep all eight clips in storyboard order.
    lines = []
    for video in videos:
        escaped = video.resolve().as_posix().replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    CONCAT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")

    command = [
        "ffmpeg", "-y",
        "-hide_banner",
        "-f", "concat", "-safe", "0", "-i", str(CONCAT_FILE),
    ]

    # Inputs 1–8: narration WAVs.
    for audio_path, _ in narration:
        command += ["-i", str(audio_path)]

    # Input 9: original score. It contains embedded cover art, so map audio only.
    command += ["-i", str(MUSIC)]

    # Input 10: static end card, available throughout the timeline.
    command += ["-loop", "1", "-framerate", "24", "-i", str(END_CARD)]

    filters = []

    # Use a relative path to avoid Windows drive-letter parsing in FFmpeg.
    subtitle_path = "assets/output/narration/subtitles_en.srt"
    subtitle_filter = (
        f"subtitles=filename='{subtitle_path}':"
        "force_style='FontName=Arial,FontSize=22,"
        "Outline=2,Shadow=1,Alignment=2,MarginV=42'"
    )

    filters.append(
        "[0:v]fps=24,scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1,"
        f"{subtitle_filter}[captioned]"
    )
    filters.append(
        "[captioned][10:v]overlay=0:0:"
        "enable='between(t,78,81)'[video_out]"
    )

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

    music_gain = 10 ** (-12.0 / 20.0)
    filters.append(
        f"[9:a:0]aresample=48000,volume={music_gain:.8f},"
        f"apad,atrim=duration={total_duration},"
        f"afade=t=out:st={max(0, total_duration - 4):.2f}:d=4[music_mix]"
    )

    filters.append(
        "[narration_mix][music_mix]"
        "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
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
        "-r", "24",
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-movflags", "+faststart",
        str(OUTPUT),
    ]

    print("\nAssembling the 81-second film. This may take a few minutes...")
    run(command)

    # Verify the finished output before marking assembly complete.
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate",
            "-of", "json", str(OUTPUT),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(probe.stdout)
    print("\nFINAL VIDEO:")
    print(json.dumps(result, indent=2))

    data["music"]["stage_status"] = "generated"
    data["music"]["output_path"] = "assets/music/mamush_original_score.mp3"
    data["assembly"]["stage_status"] = "generated"
    data["assembly"]["output_path"] = "assets/output/mamush_final.mp4"
    STORYBOARD.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"\nSUCCESS: {OUTPUT}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, subprocess.CalledProcessError, KeyError, ValueError) as exc:
        print(f"\nASSEMBLY FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
