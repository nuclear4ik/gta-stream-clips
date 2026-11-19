"""Test the authorized Hypertext Transfer Protocol download path without network calls."""

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import discovery
import twitch_media


CURRENT = datetime(2026, 11, 19, 4, tzinfo=timezone.utc)
VOD = {"kind": "vod", "url": "https://www.twitch.tv/videos/10", "archive_id": "10",
       "twitch_user": "11", "lang": "RU", "game_id": "13",
       "observed": (CURRENT - timedelta(hours=2)).isoformat()}
ENV = {"TWITCH_CLIENT_ID": "test", "STREAMCUT_TWITCH_CLIP_TOKEN_11": "protected-token",
       "STREAMCUT_TWITCH_CLIP_EDITOR_ID_11": "15"}
CDN = "production.assets.clips.twitchcdn.net"
FILE = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20


def clip(identifier, **fields):
    return dict(id=identifier, video_id="10", broadcaster_id="11", game_id="13", language="ru",
                duration=50.0, view_count=4,
                created_at=(CURRENT - timedelta(hours=1)).isoformat()) | fields


class FakeResponse(BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.status = 200
        self.length = length

    def getheader(self, name):
        assert name == "Content-Length"
        return self.length


class TwitchMediaTests(unittest.TestCase):
    def test_receives_creator_archive_only_for_exact_vod_and_broadcaster(self):
        with TemporaryDirectory() as folder, patch("discovery.helix") as helix, patch(
                "twitch_media.HTTPSConnection") as connect:
            target = Path(folder) / "twitch-vod-10.mp4"
            index = Path(folder) / "archive-index.json"
            index.write_text(json.dumps({"10": {"broadcaster_id": "11",
                                                "url": "https://media.example.com/vod.mp4?signature=private"}}))
            with patch.dict(os.environ, {twitch_media.ARCHIVE_INDEX: str(index)}, clear=True):
                self.assertTrue(twitch_media.configured())
                self.assertTrue(twitch_media.configured(VOD))
                self.assertFalse(twitch_media.configured(VOD | {"twitch_user": "22"}))
                self.assertIsNone(twitch_media.fetch(VOD | {"twitch_user": "22"}, target))
                connection = connect.return_value
                connection.getresponse.return_value = FakeResponse(FILE, str(len(FILE)))
                self.assertEqual(twitch_media.fetch(VOD, target, current=CURRENT), target)
                self.assertEqual(target.read_bytes(), FILE)
                connect.assert_called_once_with("media.example.com", timeout=20)
                connection.request.assert_called_once_with("GET", "/vod.mp4?signature=private")
                helix.assert_not_called()

    def test_invalid_archive_index_is_ignored_without_request(self):
        with TemporaryDirectory() as folder, patch("twitch_media.HTTPSConnection") as connect:
            index = Path(folder) / "index.json"
            target = Path(folder) / "clip.mp4"
            with patch.dict(os.environ, {twitch_media.ARCHIVE_INDEX: str(index)}, clear=True):
                for value in (None, b"{", b"[]", b'{"10":{"broadcaster_id":"11","url":"http://media.example.com/vod"}}'):
                    if value is not None:
                        index.write_bytes(value)
                    self.assertFalse(twitch_media.configured(VOD))
                    self.assertIsNone(twitch_media.fetch(VOD, target))
            connect.assert_not_called()
            self.assertFalse(target.exists())

    def test_downloads_matching_clip_and_keeps_bearer_off_cdn_request(self):
        rows = [
            clip("irrelevant", video_id="12", view_count=1000),
            clip("owner-mismatch", broadcaster_id="14", view_count=900),
            clip("not-public", viewable="private", view_count=800),
            clip("wrong-game", game_id="15", view_count=700),
            clip("short", duration=24.99, view_count=600),
            clip("old", created_at=(CURRENT - timedelta(days=4)).isoformat(), view_count=500),
            clip("first", view_count=9),
            clip("second", view_count=5),
        ]
        downloads = [{"clip_id": "other", "landscape_download_url": "https://unrelated.invalid/mp4"},
                     {"clip_id": "first", "landscape_download_url": None},
                     {"clip_id": "second", "landscape_download_url": f"https://{CDN}/test.mp4?signature=abc"}]
        with TemporaryDirectory() as folder, patch.dict(os.environ, ENV, clear=True), patch(
                "discovery.helix", side_effect=[rows, downloads]) as helix, patch(
                "twitch_media.HTTPSConnection") as connect:
            target = Path(folder) / "twitch-vod-10.mp4"
            connection = connect.return_value
            connection.getresponse.return_value = FakeResponse(FILE, str(len(FILE)))
            self.assertTrue(twitch_media.configured())
            self.assertTrue(twitch_media.configured(VOD))
            self.assertEqual(twitch_media.fetch(VOD, target, current=CURRENT), target)
            self.assertEqual(target.read_bytes(), FILE)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(list(Path(folder).iterdir()), [target])
            endpoints = [call.args[0] for call in helix.call_args_list]
            self.assertEqual(endpoints, ["clips", "clips/downloads"])
            self.assertEqual(helix.call_args.args[1], [
                ("broadcaster_id", "11"), ("editor_id", "15"), ("clip_id", "first"), ("clip_id", "second"),
            ])
            self.assertEqual(helix.call_args.args[2]["Authorization"], "Bearer protected-token")
            connect.assert_called_once_with(CDN, timeout=20)
            connection.request.assert_called_once_with("GET", "/test.mp4?signature=abc")
            connection.close.assert_called_once()

    def test_disallows_wrong_host_redirects_credentials_and_non_https(self):
        with patch("twitch_media.HTTPSConnection") as connect:
            addresses = [f"http://{CDN}/video", f"https://{CDN}@other.invalid/v",
                         f"https://user:secret@{CDN}/video", f"https://{CDN}:443/video",
                         f"https://{CDN}.other.invalid/video", "https://127.0.0.1/video"]
            for address in addresses:
                with self.subTest(address=address), self.assertRaises(discovery.ConnectionProblem):
                    twitch_media.download(address, Path("copy.mp4"))
            connect.assert_not_called()

        with TemporaryDirectory() as folder, patch("twitch_media.HTTPSConnection") as connect:
            target = Path(folder) / "clip.mp4"
            response = FakeResponse(b"")
            response.status = 302
            connect.return_value.getresponse.return_value = response
            with self.assertRaisesRegex(discovery.ConnectionProblem, "HTTP 302"):
                twitch_media.download(f"https://{CDN}/video", target)
            self.assertFalse(target.exists())
            connect.assert_called_once()

    def test_rejects_truncated_or_oversized_media_atomically(self):
        connection = Mock()
        with TemporaryDirectory() as folder, patch("twitch_media.HTTPSConnection", return_value=connection):
            target = Path(folder) / "clip.mp4"
            for contents, size in ((FILE, str(len(FILE) + 4)), (FILE, str(twitch_media.MAX_BYTES + 1)),
                                   (b"this is not an mp4", None)):
                connection.getresponse.return_value = FakeResponse(contents, size)
                with self.assertRaises(discovery.ConnectionProblem):
                    twitch_media.download(f"https://{CDN}/video", target)
                self.assertEqual(list(Path(folder).iterdir()), [])
            connection.getresponse.return_value = FakeResponse(FILE)
            with patch.object(twitch_media, "MAX_BYTES", 20), self.assertRaises(discovery.ConnectionProblem):
                twitch_media.download(f"https://{CDN}/video", target)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_without_broadcaster_authorization_or_with_existing_file_no_network(self):
        with TemporaryDirectory() as folder, patch("discovery.helix") as helix:
            target = Path(folder) / "clip.mp4"
            with patch.dict(os.environ, {"TWITCH_CLIENT_ID": "test"}, clear=True):
                self.assertFalse(twitch_media.configured())
                self.assertIsNone(twitch_media.fetch(VOD, target))
            target.write_bytes(b"existing")
            with patch.dict(os.environ, ENV, clear=True):
                self.assertEqual(twitch_media.fetch(VOD, target), target)
                self.assertIsNone(twitch_media.fetch(VOD | {"kind": "live"}, target))
                with self.assertRaisesRegex(ValueError, "ID записи"):
                    twitch_media.fetch(VOD | {"archive_id": "99"}, target)
            self.assertEqual(target.read_bytes(), b"existing")
            helix.assert_not_called()


if __name__ == "__main__":
    unittest.main()
