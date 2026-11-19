import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import autocut
import reaction
from clips import Recording
from moment_model import resolve_moment, select_moments


class AutoCutTests(unittest.TestCase):
    def setUp(self):
        self.recording = Recording(90, 0, 1, 1920, 1080)
        self.video = Path("twitch-host-1516.mp4")

    def test_long_recordings_sample_bounded_distributed_windows(self):
        self.assertEqual(autocut.analysis_windows(200), [(0, 120), (120, 80)])
        windows = autocut.analysis_windows(36 * 3600)
        self.assertEqual(len(windows), autocut.WINDOW_COUNT)
        self.assertEqual(sum(size for _, size in windows), 480)
        self.assertGreater(windows[-1][0], 60 * 60)
        self.assertLessEqual(windows[-1][0] + windows[-1][1], 24 * 3600)

    def test_layout_requires_all_selectors_to_match_and_validates_crops(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "layout.json"
            path.write_text(
                '[{"filename":"twitch-host-*.mp4","title":"GTA*","facecam":"0%,0%,22%,26%",'
                '"gameplay":"22%,0%,78%,100%"}]',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"STREAMCUT_AUTOCUT_LAYOUTS": str(path)}):
                self.assertIsNone(autocut.configured_layout(self.video, "Different game", self.recording))
                layout = autocut.configured_layout(self.video, "Gta VI реакции", self.recording)
                self.assertEqual(layout["gameplay"], "22%,0%,78%,100%")
                path.write_text('[{"title":"GTA*","facecam":"0%,0%,22%,26%","gameplay":"10%,0%,90%,100%"}]')
                with self.assertRaises(ValueError):
                    autocut.configured_layout(self.video, "Gta VI", self.recording)

    def test_camera_detector_requires_repeatable_unambiguous_face(self):
        left = (.10, .54, .05, .09)
        right = (.85, .24, .06, .10)
        self.assertEqual(autocut.stable_face([[left], [], [left], [left], [left]]), left)
        self.assertIsNone(autocut.stable_face([[left] * 8, []]))
        self.assertIsNone(autocut.stable_face([[left, right]] * 5))
        self.assertEqual(autocut.edge_faces([(900, 15, 110, 105), (50, 22, 100, 94)], 1920, 1080),
                         [(50 / 1920, 22 / 1080, 100 / 1920, 94 / 1080)])

    def test_prepare_returns_capped_profile_and_real_speech_evidence(self):
        cues = [autocut.Caption(7, 34, "Какая здесь большая карта."),
                autocut.Caption(55, 81, "Утром поедем на задание.")]
        choices = [
            {"start_seconds": 7, "end_seconds": 34, "headline": "Какая большая карта"},
            {"start_seconds": 55, "end_seconds": 81, "headline": "Поездка на задание"},
        ]
        evidence = {"code": "es", "confidence": .95}
        with patch.dict(os.environ, {"STREAMCUT_OLLAMA_MODEL": "fixture"}), \
             patch.object(autocut, "probe", return_value=self.recording), \
             patch.object(autocut, "configured_layout", return_value={"facecam": "0%,0%,22%,26%"}), \
             patch.object(autocut, "whisper_model"), \
             patch.object(autocut, "transcribe_recording", return_value=cues), \
             patch.object(autocut.moment_model, "select_moments", return_value=choices) as selected, \
             patch.object(autocut, "speech_language", return_value=evidence) as detected:
            result = autocut.prepare(self.video, "RU", "GTA VI", 1)
        self.assertEqual(result["mode"], "manual")
        self.assertEqual(result["speech_language"], evidence)
        self.assertEqual(detected.call_args.args[-1], result["moments"])
        self.assertEqual(selected.call_args.kwargs, {"minimum": 25, "maximum": 50, "complete": True})
        self.assertEqual(len(result["moments"]), 1)
        moment = result["moments"][0]
        self.assertEqual(moment, choices[0])

    def test_prepare_handles_missing_or_moving_camera_without_guessing(self):
        cues = [autocut.Caption(7, 37, "Так, идём смотреть карту.")]
        choices = [{"start_seconds": 7, "end_seconds": 37, "headline": "Идём смотреть карту"}]
        with patch.dict(os.environ, {"STREAMCUT_OLLAMA_MODEL": "fixture"}), \
             patch.object(autocut, "probe", return_value=self.recording), \
             patch.object(autocut, "configured_layout", return_value=None), \
             patch.object(autocut, "face_detector"), \
             patch.object(autocut, "whisper_model"), \
             patch.object(autocut, "transcribe_recording", return_value=cues), \
             patch.object(autocut.moment_model, "select_moments", return_value=choices), \
             patch.object(autocut, "detected_layout", return_value=None):
            with self.assertRaises(reaction.MissingReactionProfile):
                autocut.prepare(self.video, "RU", "GTA VI", 1)

    def test_detected_audio_must_agree_across_selected_intervals(self):
        intervals = [{"start_seconds": 0, "end_seconds": 30}, {"start_seconds": 32, "end_seconds": 62}]
        results = [("ru", .99, []), ("ru", .82, []), ("es", .96, []), ("en", .97, [])]
        with patch.object(autocut, "audio_samples", return_value=SimpleNamespace(size=16000)):
            model = Mock()
            model.detect_language.side_effect = results
            self.assertEqual(autocut.speech_language(self.video, self.recording, model, intervals),
                             {"code": "ru", "confidence": .82})
            self.assertIsNone(autocut.speech_language(self.video, self.recording, model, intervals))

    def test_short_model_interval_extends_at_whole_cues(self):
        cues = [
            autocut.Caption(0, 5, "Смотри, машина справа."),
            autocut.Caption(6, 12, "Она поворачивает"),
            autocut.Caption(12.1, 17, "за нами, вот это да!"),
            autocut.Caption(22, 27, "Мы успели оторваться."),
            autocut.Caption(30, 35, "Конец погони."),
        ]
        choice = {"start_cue":"C002", "end_cue":"C003", "score":5, "headline":"Спаслись от погони"}
        with patch.dict(os.environ, {"STREAMCUT_OLLAMA_MODEL": "fixture"}), \
             patch.object(autocut.moment_model, "request_moments", return_value=[choice]):
            selected = select_moments(autocut.as_srt(cues), "RU", minimum=25, maximum=50, complete=True)
        self.assertEqual(selected, [{"start_seconds":0, "end_seconds":27, "headline":"Спаслись от погони"}])

    def test_drops_unfinished_end_and_unrelated_pause(self):
        choice = {"start_cue":"C001", "end_cue":"C002", "score":5, "headline":"Спаслись от погони"}
        cues = [autocut.Caption(0, 9, "Сначала посмотрим карту."),
                autocut.Caption(15, 29, "А потом мы…")]
        self.assertIsNone(resolve_moment(choice, cues, minimum=25, maximum=50, complete=True))
        cues[1] = autocut.Caption(24, 29, "Мы успели оторваться.")
        self.assertIsNone(resolve_moment(choice, cues, minimum=25, maximum=50, complete=True))


if __name__ == "__main__":
    unittest.main()
