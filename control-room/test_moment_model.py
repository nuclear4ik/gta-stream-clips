"""Check model transport and conversion from cue IDs to original-video ranges."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from moment_model import select_moments


SRT = """1
00:00:02,500 --> 00:00:04,100
Look!

2
00:00:04,100 --> 00:00:08,050
Amazing!

3
00:00:08,050 --> 00:00:11,000
Replay!
"""


def moment(first, last, score, headline="Look at that boat"):
    return {"start_cue": f"C{first:03d}", "end_cue": f"C{last:03d}", "score": score, "headline": headline}


class MomentModelTests(unittest.TestCase):
    @patch.dict("os.environ", {"STREAMCUT_OLLAMA_MODEL": "test-model", "STREAMCUT_OLLAMA_PORT": "11435"})
    @patch("moment_model.HTTPConnection")
    def test_local_chat_request_and_real_srt_boundaries(self, http):
        response = http.return_value.getresponse.return_value
        response.status = 200
        response.read.return_value = json.dumps({
            "message": {"content": json.dumps({"moments": [moment(1, 2, 5), moment(2, 3, 4), moment(3, 3, 3)]})},
            "done_reason": "stop",
        }).encode()
        selected = select_moments(SRT, "ESP")
        self.assertEqual([(m["start_seconds"], m["end_seconds"]) for m in selected],
                         [(2.5, 8.05), (8.05, 11.0)])
        http.assert_called_once()
        self.assertEqual(http.call_args.args, ("127.0.0.1", 11435))
        request = http.return_value.request.call_args
        self.assertEqual(request.args, ("POST", "/api/chat"))
        body = json.loads(request.kwargs["body"])
        self.assertEqual(body["model"], "test-model")
        self.assertFalse(body["stream"])
        self.assertIn("Spanish", body["messages"][0]["content"])
        self.assertIn("C001 [2.500–4.100 s] Look!", body["messages"][1]["content"])
        properties = body["format"]["properties"]["moments"]["items"]["properties"]
        self.assertEqual(properties["start_cue"]["enum"], ["C001", "C002", "C003"])
        http.return_value.close.assert_called_once()

    @patch.dict("os.environ", {"STREAMCUT_OLLAMA_MODEL": "test-model"})
    @patch("moment_model.request_moments")
    def test_drops_unknown_ids_bad_types_and_invalid_headlines(self, request):
        request.return_value = [moment(1, 99, 5), dict(moment(1, 3, 5), start_cue=True),
                                moment(2, 3, 5, "Bravo")]
        with self.assertRaisesRegex(ValueError, "не нашла подходящих моментов"):
            select_moments(SRT, "RU")

    @patch.dict("os.environ", {"STREAMCUT_OLLAMA_MODEL": "test-model"})
    @patch("moment_model.MAX_BATCH_BYTES", 92)
    @patch("moment_model.request_moments")
    def test_analyzes_both_batches_and_compares_candidates(self, request):
        request.side_effect = [[moment(1, 1, 2)], [moment(1, 1, 5)]]
        selected = select_moments(SRT, "EN")
        self.assertEqual(request.call_count, 2)
        self.assertEqual([m["start_seconds"] for m in selected], [8.05, 2.5])

    @patch.dict("os.environ", {"STREAMCUT_OLLAMA_MODEL": "test-model"})
    @patch("moment_model.MAX_BATCHES", 1)
    @patch("moment_model.MAX_BATCH_BYTES", 92)
    @patch("moment_model.request_moments")
    def test_refuses_transcript_that_exceeds_batch_limit_before_sending(self, request):
        with self.assertRaisesRegex(ValueError, "Расшифровка слишком длинная"):
            select_moments(SRT, "EN")
        request.assert_not_called()

    @patch.dict("os.environ", {"STREAMCUT_OLLAMA_MODEL": ""})
    def test_missing_model_lists_required_configuration(self):
        with self.assertRaisesRegex(ValueError, "STREAMCUT_OLLAMA_MODEL"):
            select_moments(SRT, "EN")

    @patch.dict("os.environ", {"STREAMCUT_OLLAMA_MODEL": "test-model"})
    @patch("moment_model.HTTPConnection")
    def test_unavailable_or_truncated_model_returns_actionable_errors(self, http):
        response = http.return_value.getresponse.return_value
        response.status = 404
        with self.assertRaisesRegex(ValueError, "не нашла модель"):
            select_moments(SRT, "EN")
        response.status = 200
        response.read.return_value = b'{"message":{"content":"{\\"moments\\": []}"},"done_reason":"length"}'
        with self.assertRaisesRegex(ValueError, "не вернула готовый список"):
            select_moments(SRT, "EN")
