"""Exact source intervals through the existing video editor."""

import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from clips import read_ranges


EDITOR = Path(__file__).resolve().with_name("clips.py")


class RangesTest(unittest.TestCase):
    def test_rejects_invalid_ranges_before_editing(self):
        invalid = (
            {"start_seconds": True, "end_seconds": 4},
            {"start_seconds": 1, "end_seconds": float("nan")},
            {"start_seconds": 4, "end_seconds": 3},
            {"start_seconds": 0, "end_seconds": 10.01},
            {"start_seconds": 0, "end_seconds": 0.0001},
            {"start_seconds": 1, "end_seconds": 4, "headline": "Too short"},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ranges.json"
            for moment in invalid:
                with self.subTest(moment=moment):
                    path.write_text(json.dumps({"moments": [moment]}))
                    with self.assertRaisesRegex(ValueError, "(?i)интервале? 1"):
                        read_ranges(path, 10)

    def test_rejects_other_timeline_modes(self):
        for args in (("--at", "2"), ("--hook", "move"), ("--hook", "tease")):
            with self.subTest(args=args):
                result = subprocess.run([
                    sys.executable, str(EDITOR), "source.mp4", "--ranges", "ranges.json", *args,
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("--ranges", result.stderr)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg and ffprobe")
    def test_render_exact_ranges_with_original_audio_and_split_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video, ranges, srt, output = (folder / path for path in ("source.mp4", "ranges.json", "input.srt", "out"))
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=6",
                "-f", "lavfi", "-i", "sine=f=440:r=48000:d=3.75[a];sine=f=880:r=48000:d=2.25[b];"
                "[a][b]concat=n=2:v=0:a=1", "-c:v", "libx264", "-c:a", "aac", str(video),
            ], check=True, capture_output=True)
            ranges.write_text(json.dumps({"moments": [
                {"start_seconds": 4, "end_seconds": 5.25, "headline": "Exact ranged clip"},
                {"start_seconds": 1.25, "end_seconds": 3.5},
            ]}))
            srt.write_text("1\n00:00:00,500 --> 00:00:05,500\nSource audio stays here\n")
            process = subprocess.run([
                sys.executable, str(EDITOR), str(video), "--ranges", str(ranges), "--srt", str(srt),
                "--facecam", "0,0,160,180", "--gameplay", "160,0,160,180", "--headline", "Shared reaction title",
                "--require-speech", "--burn-captions", "--width", "720", "--count", "1", "--seconds", "5",
                "--output", str(output),
            ], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            clips = json.loads((output / "manifest.json").read_text())["clips"]
            self.assertEqual([clip["headline"] for clip in clips], ["Exact ranged clip", "Shared reaction title"])
            for clip, (start, end, crossings) in zip(clips, ((4, 5.25, 220), (1.25, 3.5, 110))):
                self.assertEqual(clip["timeline"], [{"source_start": start, "source_end": end, "role": "story"}])
                self.assertIsNotNone(clip["styled_captions"])
                rendered = str(output / clip["video"])
                probe = json.loads(subprocess.check_output([
                    "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", rendered,
                ]))
                video_stream, audio_stream = probe["streams"]
                self.assertEqual((video_stream["width"], video_stream["height"]), (720, 1280))
                self.assertEqual(audio_stream["codec_type"], "audio")
                self.assertAlmostEqual(float(probe["format"]["duration"]), end - start, delta=1 / 30)
                raw = subprocess.check_output([
                    "ffmpeg", "-v", "error", "-ss", "0.25", "-i", rendered, "-t", "0.25", "-vn", "-ac", "1",
                    "-f", "s16le", "-",
                ])
                samples = struct.unpack(f"<{len(raw) // 2}h", raw)
                self.assertAlmostEqual(sum(a <= 0 < b for a, b in zip(samples, samples[1:])), crossings, delta=2)
