"""Unit tests for Stage 1 (script and narration) using offline mocks and fixtures."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mamush_film.cache import StageCache  # noqa: E402
from mamush_film.config import load_config  # noqa: E402
from mamush_film.stages.script_narration import (  # noqa: E402
    build_srt,
    run_script_narration_stage,
)


class _FakeModels:
    def __init__(self, script_segments: list[dict], fail_tts: bool = True) -> None:
        self._segments = script_segments
        self._fail_tts = fail_tts
        self.calls: list[str] = []

    def generate_content(self, *, model: str, contents: str, config: object) -> object:
        self.calls.append(model)
        modalities = getattr(config, "response_modalities", None) or []
        if "AUDIO" in modalities:
            if self._fail_tts:
                raise RuntimeError("Simulated TTS model unavailable for test")
            return SimpleNamespace(
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(
                                    inline_data=SimpleNamespace(
                                        data=b"\x00\x00" * 240,
                                        mime_type="audio/L16;codec=pcm;rate=24000",
                                    )
                                )
                            ]
                        )
                    )
                ],
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10,
                    candidates_token_count=5,
                    thoughts_token_count=None,
                    total_token_count=15,
                ),
            )

        return SimpleNamespace(
            parsed={"segments": self._segments},
            text=json.dumps({"segments": self._segments}, ensure_ascii=False),
            usage_metadata=SimpleNamespace(
                prompt_token_count=120,
                candidates_token_count=180,
                thoughts_token_count=None,
                total_token_count=300,
            ),
        )


class _FakeGenaiClient:
    def __init__(self, script_segments: list[dict], fail_tts: bool = True) -> None:
        self.models = _FakeModels(script_segments, fail_tts=fail_tts)


class Stage1ScriptNarrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="mamush_stage1_test_"))
        (self.tmp_dir / "schemas").mkdir()
        (self.tmp_dir / "storyboards").mkdir()
        shutil.copy2(
            PROJECT_ROOT / "schemas" / "storyboard.schema.json",
            self.tmp_dir / "schemas" / "storyboard.schema.json",
        )
        shutil.copy2(
            PROJECT_ROOT / "storyboards" / "storyboard.json",
            self.tmp_dir / "storyboards" / "storyboard.json",
        )
        self.config = load_config(project_root=self.tmp_dir)
        self.cache = StageCache(self.config.cache)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_build_srt_formatting(self) -> None:
        srt = build_srt(
            [
                {"start_sec": 0.6, "end_sec": 7.4, "text_en": "Line one."},
                {"start_sec": 64.5, "end_sec": 71.3, "text_en": "Line two."},
            ],
            "text_en",
        )
        self.assertIn("1\n00:00:00,600 --> 00:00:07,400\nLine one.", srt)
        self.assertIn("2\n00:01:04,500 --> 00:01:11,300\nLine two.", srt)

    def test_export_mode_writes_outputs_and_runs_log(self) -> None:
        result = run_script_narration_stage(self.config, self.cache, mode="export")
        self.assertTrue(result.narration_text_path.is_file())
        self.assertTrue(result.english_srt_path.is_file())
        self.assertTrue(result.amharic_srt_path.is_file())
        self.assertTrue(result.bilingual_script_path.is_file())
        self.assertTrue(result.runs_log_path.is_file())

        runs = [
            json.loads(line)
            for line in result.runs_log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["operation"], "export_from_storyboard")
        self.assertIsNone(runs[0]["model"])
        self.assertIsNone(runs[0]["cost_usd"])

    def test_gemini_mode_caches_and_handles_tts_limitation(self) -> None:
        storyboard = json.loads(
            self.config.paths.storyboard_path.read_text(encoding="utf-8")
        )
        segments = [
            {
                "shot_id": s["shot_id"],
                "text_am": s["narration"]["text_am"],
                "text_en": s["narration"]["text_en"],
                "emotion": s["narration"]["emotion"],
                "start_sec": s["narration"]["start_sec"],
                "end_sec": s["narration"]["end_sec"],
            }
            for s in storyboard["shots"]
        ]
        fake_client = _FakeGenaiClient(segments, fail_tts=True)

        res1 = run_script_narration_stage(
            self.config, self.cache, mode="gemini", client=fake_client
        )
        self.assertIn("unavailable", res1.tts_status)
        self.assertEqual(res1.audio_paths, [])
        self.assertEqual(fake_client.models.calls.count(self.config.gemini.script_model), 1)

        # Second run with skip_tts=True should hit cache and make zero script API calls.
        res2 = run_script_narration_stage(
            self.config, self.cache, mode="gemini", skip_tts=True, client=fake_client
        )
        self.assertEqual(res2.tts_status, "skipped_by_flag")
        self.assertEqual(fake_client.models.calls.count(self.config.gemini.script_model), 1)

    def test_gemini_mode_requires_api_key_without_mock_client(self) -> None:
        with self.assertRaises(ValueError):
            run_script_narration_stage(self.config, self.cache, mode="gemini")


if __name__ == "__main__":
    unittest.main()
