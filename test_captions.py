"""Caption layout, text preservation and edit-timeline regressions."""

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from captions import (CAPTION_COLORS, Caption, Word, caption_parts, caption_position,
                      follow_edit, read_srt, write_ass)
from editing import CAMERA_HEIGHT_1080, plan_edit


class CaptionsTest(unittest.TestCase):
    def render(self, captions, **kwargs):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "captions.ass"
            write_ass(path, captions, "split", **kwargs)
            return path.read_text()

    def test_lower_gameplay_position_and_override(self):
        x, y = caption_position("split")
        self.assertEqual(x, 1080 / 2)
        self.assertGreater(y, (CAMERA_HEIGHT_1080 + 1920) / 2)
        self.assertLess(y + 78, 1920 * 0.78)
        for layout in ("fit", "crop"):
            self.assertEqual(caption_position(layout), (540, 1382))
        self.assertEqual(caption_position("split", 66.5), (540, 1277))

    def test_long_phrase_has_two_lines_without_losing_words(self):
        tokens = "Это невероятно длинная фраза которую нельзя обрезать".split()
        parts, size = caption_parts(tokens)
        self.assertEqual(" ".join(parts).replace(r"\N", ""), " ".join(tokens).upper())
        self.assertEqual("".join(parts).count(r"\N"), 1)
        self.assertLess(size, 78)

    def test_single_long_word_is_preserved(self):
        word = "сверхвысокопроизводительный"
        parts, _ = caption_parts([word])
        self.assertEqual(parts[0].replace(r"\N", ""), word.upper())
        self.assertNotIn("…", parts[0])

    def test_text_cannot_inject_ass_commands(self):
        text = self.render([Caption(0, 2, r"{\pos(0,0)} Привет")])
        events = [line for line in text.splitlines() if line.startswith("Dialogue:")]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].count(r"\pos("), 1)

    def test_highlighting_keeps_geometry_and_pauses(self):
        words = (Word(0.2, 0.5, "невероятный"), Word(0.8, 1.2, "прыжок"))
        for style, color in CAPTION_COLORS.items():
            text = self.render([Caption(0, 1.5, "невероятный прыжок", words)], caption_style=style)
            events = [line for line in text.splitlines() if line.startswith("Dialogue:")]
            self.assertEqual(len(events), 5)  # lead, word, pause, word, tail
            self.assertIn(rf"\1c&H{color}&", text)
            geometry = [re.sub(r"\{[^}]*\}", "", line.split(",", 9)[9]) for line in events]
            self.assertEqual(len(set(geometry)), 1)
            self.assertTrue(all(r"\pos(540,1382)" in line for line in events))

    def test_srt_preserves_text_without_fake_word_timing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "input.srt"
            phrase = "Попробуем невероятно длинное слово сверхвысокопроизводительный"
            path.write_text(f"1\n00:00:01,000 --> 00:00:06,000\n{phrase}\n")
            captions = read_srt(path)
        self.assertEqual(" ".join(c.text for c in captions), phrase)
        self.assertTrue(all(not c.words for c in captions))
        self.assertEqual(captions[0].start, 1)
        self.assertEqual(captions[-1].end, 6)

    def test_word_timing_survives_each_hook_mode(self):
        caption = Caption(4, 6, "Вот прыжок", (Word(4, 4.8, "Вот"), Word(5, 6, "прыжок")))
        for mode in ("off", "tease", "move"):
            sections = plan_edit(0, 10, 5, mode, 1.6)
            cues = follow_edit([caption], sections)
            duration = sum(s.duration for s in sections)
            self.assertTrue(cues)
            for cue in cues:
                self.assertTrue(0 <= cue.start < cue.end <= duration)
                self.assertTrue(all(cue.start <= w.start < w.end <= cue.end for w in cue.words))
            self.assertTrue(all(a.end <= b.start for a, b in zip(cues, cues[1:])))

    def test_cli_help_and_invalid_position(self):
        root = Path(__file__).resolve().parent
        help_result = subprocess.run([sys.executable, str(root / "clips.py"), "--help"], capture_output=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for value in ("nan", "inf", "39", "81"):
            result = subprocess.run([sys.executable, str(root / "clips.py"), "demo.mp4", "--caption-y", value],
                                    capture_output=True)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
