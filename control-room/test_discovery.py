"""Checks discovery behavior against controlled platform metadata."""

from datetime import datetime, timedelta, timezone
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import discovery


def reply(data):
    return 200, {}, json.dumps(data).encode()


class YouTubeDiscoveryTest(unittest.TestCase):
    def test_market_and_creator_matching_include_small_reuploads(self):
        def result(video, title, channel, broadcast="none"):
            return {"id": {"videoId": video}, "snippet": {
                "title": title, "channelTitle": channel, "liveBroadcastContent": broadcast,
            }}

        items = [
            result("aaaaaaaaaaa", "Бустер проходит GTA 6", "Небольшой канал"),
            result("bbbbbbbbbbb", "Прямой эфир: GTA VI", "Buster архив", "live"),
            result("ccccccccccc", "Супербустер проходит GTA 6", "Без имени"),
            result("ddddddddddd", "Бустер проходит GTA V", "Его архив"),
            result("eeeeeeeeeee", "Бустер скоро запустит GTA 6", "Его архив", "upcoming"),
            result("aaaaaaaaaaa", "Бустер проходит GTA 6", "Небольшой канал"),
        ]
        env = {"STREAMCUT_YOUTUBE_KEY": "test", "STREAMCUT_YOUTUBE_RU": "Бустер,Buster"}
        with patch.dict(os.environ, env, clear=True), patch.object(discovery, "request", return_value=reply({"items": items})) as request:
            candidates = list(discovery.youtube("RU", "ru"))
        self.assertEqual([candidate["kind"] for candidate in candidates], ["video", "live"])
        self.assertEqual([candidate["url"].split("=")[-1] for candidate in candidates], ["aaaaaaaaaaa", "bbbbbbbbbbb"])
        request.assert_called_once()
        args, _ = request.call_args
        self.assertEqual(args[1:3], ("www.googleapis.com", "GET"))
        query = parse_qs(urlsplit(args[3]).query)
        self.assertEqual(query["regionCode"], ["RU"])
        self.assertEqual(query["relevanceLanguage"], ["ru"])
        self.assertIn("GTA VI Buster", query["q"][0])

    def test_default_roster_and_missing_api_keys(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(discovery, "request") as request:
            self.assertFalse(discovery.configured())
            self.assertIn("Бустер", discovery.roster()["RU"])
            self.assertIn("Kai Cenat", discovery.roster()["EN"])
            self.assertIn("IlloJuan", discovery.roster()["ESP"])
            self.assertEqual(list(discovery.youtube("EN", "en")), [])
            self.assertEqual(list(discovery.twitch({"ESP": "es"})), [])
            request.assert_not_called()


class TwitchDiscoveryTest(unittest.TestCase):
    def test_live_has_a_stable_key_for_each_broadcast(self):
        env = {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret",
               "STREAMCUT_TWITCH_RU": "@buster|22", "STREAMCUT_TWITCH_ESP": "ibai"}
        users = [{"id": "11", "login": "buster"}, {"id": "22", "login": "someone"},
                 {"id": "33", "login": "ibai"}]
        streams = [
            {"id": "101", "user_id": "11", "type": "live", "title": "Играем!", "language": "ru",
             "game_name": "Grand Theft Auto VI"},
            {"id": "102", "user_id": "22", "type": "live", "title": "GTA 6", "language": "en"},
            {"id": "103", "user_id": "33", "type": "live", "title": "GTA VI primeras reacciones", "language": "es"},
        ]
        responses = [reply({"access_token": "test"}), reply({"data": users}), reply({"data": streams})]
        with patch.dict(os.environ, env, clear=True), patch.object(discovery, "request", side_effect=responses) as request:
            candidates = list(discovery.twitch({"RU": "ru", "ESP": "es"}, live_only=True))
        self.assertEqual([(item["lang"], item["key"], item["url"]) for item in candidates], [
            ("RU", "twitch:stream:101", "https://www.twitch.tv/buster"),
            ("ESP", "twitch:stream:103", "https://www.twitch.tv/ibai"),
        ])
        args = request.call_args_list
        self.assertEqual(parse_qs(urlsplit(args[1].args[3]).query), {"id": ["22"], "login": ["buster", "ibai"]})
        self.assertEqual(parse_qs(urlsplit(args[2].args[3]).query), {"user_id": ["11", "22", "33"]})

    def test_vod_reuses_broadcast_key_and_filters_old_and_hidden_videos(self):
        env = {"TWITCH_CLIENT_ID": "client", "TWITCH_CLIENT_SECRET": "secret", "STREAMCUT_TWITCH_EN": "kaicenat"}
        fresh = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        item = {"id": "666", "stream_id": "101", "title": "GTA VI highlights",
                "language": "en", "created_at": fresh, "viewable": "public"}
        responses = [
            reply({"access_token": "test"}),
            reply({"data": [{"id": "11", "login": "kaicenat"}]}),
            reply({"data": [
                item, {**item, "id": "667", "created_at": old},
                {**item, "id": "668", "viewable": "private"},
                {**item, "id": "669", "language": "es"},
            ]}),
        ]
        with patch.dict(os.environ, env, clear=True), patch.object(discovery, "request", side_effect=responses):
            self.assertEqual(list(discovery.twitch({"EN": "en"})), [{
                "lang": "EN", "title": "GTA VI highlights", "url": "https://www.twitch.tv/videos/666",
                "kind": "vod", "key": "twitch:stream:101",
            }])


if __name__ == "__main__":
    unittest.main()
