"""Tests for schedule metadata, limited capacity and remote reconciliation."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import transport
import youtube_release as youtube


VIDEO = "AbC012_defG"
OTHER = "XYz012_defG"
CHANNEL = "UCABCDEFGHIJKLMNOPQRSTUV"
HOST = "www.googleapis.com"
SESSION = {"location": f"https://{HOST}/upload/youtube/v3/videos?upload_id=fixture"}


def response(status, data, headers=None):
    return status, headers or {}, json.dumps(data).encode()


def later(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0)


class YouTubeReleaseTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.video_path = Path(tmp.name) / "short.mp4"
        self.video_path.write_bytes(b"fixture mp4")
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("STREAMCUT_YOUTUBE_REQUIRE_GATEWAY", None)
        os.environ.pop("STREAMCUT_UPLOAD_GATEWAY", None)

    def upload(self, route="server", **settings):
        arguments = {"title": "Встреча в Vice City", "description": "Тест #Shorts",
                     "tags": ["#GTA6", "GTA VI"], "lang": "RU", "path": self.video_path}
        return youtube.upload(route, "fixture-token", **{**arguments, **settings})

    def test_schedule_sends_private_license_and_audience_metadata(self):
        planned = later(6)

        def request(route, host, method, path, headers, body, **_):
            self.assertEqual((route, host), ("server", HOST))
            self.assertEqual(headers["Authorization"], "Bearer fixture-token")
            if method == "POST":
                payload = json.loads(body)
                self.assertEqual(parse_qs(urlsplit(path).query)["notifySubscribers"], ["false"])
                self.assertEqual(payload["snippet"]["description"], "Тест #Shorts")
                self.assertEqual(payload["snippet"]["tags"], ["GTA6", "GTA VI"])
                self.assertEqual(payload["status"], {
                    "privacyStatus": "private", "publishAt": planned.isoformat().replace("+00:00", "Z"),
                    "license": "creativeCommon", "selfDeclaredMadeForKids": False,
                })
                return 200, SESSION, b""
            self.assertEqual(path, "/upload/youtube/v3/videos?upload_id=fixture")
            self.assertEqual(body.read(), self.video_path.read_bytes())
            return response(201, {"id": VIDEO})

        with patch("transport.request", side_effect=request) as mocked:
            self.assertEqual(self.upload(publish_at=planned, license="creativeCommon"), VIDEO)
            self.assertEqual(mocked.call_count, 2)

    def test_test_upload_uses_private_without_publish_at_and_checks_host(self):
        with patch("transport.request", return_value=(200, {
            "Location": "https://www.googleapis.com.example/upload/youtube/v3/videos?upload_id=fixture",
        }, b"")) as mocked:
            with self.assertRaises(youtube.UploadRejected):
                self.upload()
            mocked.assert_called_once()
            self.assertEqual(json.loads(mocked.call_args.args[-1])["status"], {
                "privacyStatus": "private", "license": "youtube", "selfDeclaredMadeForKids": False,
            })

    def test_invalid_publish_time_never_starts_an_upload(self):
        with patch("transport.request") as mocked:
            for start in (later(-1), "2040-04-01T10:00:00"):
                with self.assertRaises(youtube.UploadRejected):
                    self.upload(publish_at=start)
            mocked.assert_not_called()

    def test_safe_slot_retry_differs_from_unknown_media_response(self):
        capacity = response(400, {"error": {"errors": [{"reason": "uploadLimitExceeded"}]}}, {"Retry-After": "90"})
        with patch("transport.request", side_effect=[(200, SESSION, b""), capacity]):
            with self.assertRaises(youtube.SlotUnavailable) as raised:
                self.upload()
            self.assertEqual(raised.exception.retry_after_seconds, 90)
        with patch("transport.request", side_effect=[
            (200, SESSION, b""), transport.ConnectionProblem("Connection timed out"),
        ]) as request:
            with self.assertRaises(youtube.PublishUncertain):
                self.upload()
            self.assertEqual(request.call_count, 2)

    def test_strict_gateway_rejects_direct_upload_and_reschedule(self):
        os.environ["STREAMCUT_YOUTUBE_REQUIRE_GATEWAY"] = "1"
        with patch("transport.request") as request:
            for route in ("server", "RU"):
                with self.assertRaises(transport.RouteProblem):
                    self.upload(route, expected_channel=CHANNEL)
                with self.assertRaises(transport.RouteProblem):
                    youtube.reschedule(route, "fixture-token", VIDEO, later(2), expected_channel=CHANNEL)
            request.assert_not_called()

    def test_gateway_confirms_channel_again_before_sending_media(self):
        os.environ["STREAMCUT_YOUTUBE_REQUIRE_GATEWAY"] = "1"
        os.environ["STREAMCUT_UPLOAD_GATEWAY"] = "http://trusted.example"
        correct = response(200, {"items": [{"id": CHANNEL}]})
        wrong = response(200, {"items": [{"id": "wrong-channel"}]})
        opened = (200, SESSION, b"")
        with patch("transport.request", side_effect=[correct, opened, wrong]) as request:
            with self.assertRaisesRegex(youtube.UploadRejected, "другой канал"):
                self.upload("gateway", expected_channel=CHANNEL)
            self.assertEqual([call.args[2] for call in request.call_args_list], ["GET", "POST", "GET"])
        with patch("transport.request", side_effect=[correct, opened, transport.NetworkProblem("gateway offline")]) as request:
            with self.assertRaises(transport.RouteProblem):
                self.upload("gateway", expected_channel=CHANNEL)
            self.assertEqual([call.args[2] for call in request.call_args_list], ["GET", "POST", "GET"])
        with patch("transport.request", side_effect=[
            correct, opened, correct, transport.NetworkProblem("response lost"),
        ]) as request:
            with self.assertRaises(youtube.PublishUncertain):
                self.upload("gateway", expected_channel=CHANNEL)
            self.assertEqual([call.args[2] for call in request.call_args_list], ["GET", "POST", "GET", "PUT"])
            self.assertTrue(all(call.args[:2] == ("gateway", HOST) for call in request.call_args_list))

    def test_schedule_counts_only_pending_private_uploads_on_the_verified_channel(self):
        start = later(2).isoformat().replace("+00:00", "Z")
        replies = [
            response(200, {"items": [{"id": CHANNEL, "contentDetails": {"relatedPlaylists": {"uploads": "UU-fixture"}}}]}),
            response(200, {"items": [{"contentDetails": {"videoId": VIDEO}}], "nextPageToken": "page2"}),
            response(200, {"items": [{"id": VIDEO, "status": {"privacyStatus": "private", "publishAt": start}}]}),
            response(200, {"items": [{"contentDetails": {"videoId": OTHER}}]}),
            response(200, {"items": [{"id": OTHER, "status": {"privacyStatus": "public", "publishAt": start}}]}),
        ]
        with patch("transport.request", side_effect=replies) as mocked:
            self.assertEqual(youtube.scheduled_entries("server", "secret", CHANNEL, 10),
                             {VIDEO: start.replace("Z", "+00:00")})
        self.assertIn("mine=true", mocked.call_args_list[0].args[3])
        self.assertIn("pageToken=page2", mocked.call_args_list[3].args[3])

    def test_remote_schedule_move_preserves_metadata_and_requires_confirmation(self):
        previous = later(1).isoformat()
        planned = later(5)
        known = {VIDEO: {"privacyStatus": "private", "publishAt": previous, "license": "creativeCommon",
                         "embeddable": False, "selfDeclaredMadeForKids": False, "uploadStatus": "processed"}}
        with patch.object(youtube, "publication_status", return_value=known), patch(
            "transport.request", return_value=response(200, {"status": {"privacyStatus": "private", "publishAt": planned.isoformat()}}),
        ) as request:
            youtube.reschedule("server", "fixture-token", VIDEO, planned)
            payload = json.loads(request.call_args.args[-1])
            self.assertEqual(payload["id"], VIDEO)
            self.assertEqual(payload["status"], {
                "privacyStatus": "private", "publishAt": planned.isoformat().replace("+00:00", "Z"),
                "license": "creativeCommon", "embeddable": False, "selfDeclaredMadeForKids": False,
            })
        with patch.object(youtube, "publication_status", return_value=known), patch(
            "transport.request", return_value=response(200, {"status": {"privacyStatus": "private"}}),
        ):
            with self.assertRaises(youtube.ScheduleUncertain):
                youtube.reschedule("server", "fixture-token", VIDEO, planned)
        with patch.object(youtube, "publication_status", return_value={
            VIDEO: {"privacyStatus": "public", "publishAt": previous},
        }), patch("transport.request") as request:
            with self.assertRaises(youtube.UploadRejected):
                youtube.reschedule("server", "fixture-token", VIDEO, planned)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
