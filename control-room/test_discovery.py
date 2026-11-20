"""Checks discovery behavior against synthetic responses shaped like the platform APIs."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import discovery
import regional
import studio


def reply(data):
    return 200, {}, json.dumps(data).encode()


class YouTubeDiscoveryTest(unittest.TestCase):
    def test_originals_reuploads_and_false_matches_in_each_market(self):
        current = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)

        def youtube_item(video, title, channel, broadcast="none", description=""):
            return {"id": video, "snippet": {
                "title": title, "channelTitle": channel, "description": description, "channelId": "UC" + "Z" * 22,
                "publishedAt": studio.iso(current - timedelta(hours=1)), "liveBroadcastContent": broadcast,
            }}

        markets = [
            ("RU", "ru", "RU", "Бустер,Buster", "Buster", "Бустер &amp; друзья", "ГТА‑VI",
             "Супербустер", "ГТА 5", "Подпишитесь на Бустера"),
            ("EN", "en", "US", "Kai Cenat,KaiCenat", "KaiCenat", "ＫＡＩ‑ＣＥＮＡＴ &amp; friends", "GTA６",
             "KaiCenatFan", "GTA V", "Shoutout to Kai Cenat"),
            ("ESP", "es", "ES", "AuronPlay,Auron Play", "AuronPlay", "Auron.Play &amp; amigos", "Grand Theft Auto: VI",
             "AuronPlaycito", "GTA Online", "Sígueme: AuronPlay"),
        ]
        for lang, code, region, names, alias, author, game, lookalike, other_game, promotion in markets:
            with self.subTest(lang=lang):
                original = youtube_item("aaaaaaaaaaa", game, alias, "live")
                reupload = youtube_item("bbbbbbbbbbb", f"{author} | {game}", "Fixture Archive")
                items = [
                    original, reupload, reupload,
                    youtube_item("ccccccccccc", game, lookalike),
                    youtube_item("ddddddddddd", f"{lookalike} | {game}", "Fixture Archive"),
                    youtube_item("eeeeeeeeeee", other_game, alias),
                    youtube_item("fffffffffff", f"{game} | {promotion}", "Fixture Archive"),
                    youtube_item("ggggggggggg", f"{other_game} | #GTA6", alias),
                    youtube_item("hhhhhhhhhhh", game, "Fixture Archive", description=alias),
                    youtube_item("iiiiiiiiiii", other_game, alias, description=game),
                    youtube_item("jjjjjjjjjjj", game, alias, "upcoming"),
                    youtube_item("invalid", game, alias),
                ]
                details = {item["id"]: item for item in items}

                def response(_route, host, method, path, _headers):
                    self.assertEqual((host, method), ("www.googleapis.com", "GET"))
                    endpoint = urlsplit(path)
                    query = parse_qs(endpoint.query)
                    if endpoint.path == "/youtube/v3/search":
                        self.assertEqual(query["regionCode"], [region])
                        self.assertEqual(query["relevanceLanguage"], [code])
                        return reply({"items": [{"id": {"videoId": item["id"]}} for item in items]})
                    self.assertEqual(endpoint.path, "/youtube/v3/videos")
                    identifiers = query["id"][0].split(",")
                    self.assertEqual(len(identifiers), len(set(identifiers)))
                    self.assertNotIn("invalid", identifiers)
                    return reply({"items": [details[identifier] for identifier in identifiers]})

                env = {"STREAMCUT_YOUTUBE_KEY": "fixture", f"STREAMCUT_YOUTUBE_{lang}": names}
                with patch.dict(os.environ, env, clear=True), patch.object(
                        discovery, "request", side_effect=response) as request:
                    candidates = regional.youtube_market(lang, code, current)
                self.assertEqual([(row["lang"], row["title"], row["kind"], row["url"]) for row in candidates], [
                    (lang, game, "live", "https://www.youtube.com/watch?v=aaaaaaaaaaa"),
                    (lang, reupload["snippet"]["title"].replace("&amp;", "&"), "video",
                     "https://www.youtube.com/watch?v=bbbbbbbbbbb"),
                ])
                queries = [parse_qs(urlsplit(call.args[3]).query) for call in request.call_args_list
                           if urlsplit(call.args[3]).path == "/youtube/v3/search"]
                self.assertIn("Grand Theft Auto VI", queries[0]["q"][0].split("|"))
                self.assertEqual(queries[0]["eventType"], ["live"])
                self.assertTrue(any(f"GTA 6 {alias}" in query["q"][0].split("|") for query in queries[1:]))
                if lang == "RU":
                    self.assertIn("ГТА VI", queries[0]["q"][0].split("|"))

    def test_default_roster_and_missing_api_keys(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(discovery, "request") as request:
            self.assertFalse(regional.configured())
            self.assertIn("Бустер", discovery.roster()["RU"])
            self.assertIn("Kai Cenat", discovery.roster()["EN"])
            self.assertIn("IlloJuan", discovery.roster()["ESP"])
            current = studio.now()
            self.assertIsNone(regional.youtube_market("EN", "en", current))
            self.assertIsNone(regional.twitch_markets({"ESP": "es"}, current, set()))
            request.assert_not_called()

    def test_overrides_support_alias_groups_and_empty_platform_lists(self):
        env = {"STREAMCUT_YOUTUBE_RU": " Бустер, buster, BUSTER | Другой ",
               "STREAMCUT_YOUTUBE_ESP": "", "STREAMCUT_TWITCH_ESP": " @Fixture_ES | 77 | fixture_es "}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(discovery.youtube_authors("RU"), [("Бустер", "buster"), ("Другой",)])
            self.assertEqual(discovery.youtube_authors("ESP"), [])
            self.assertEqual(discovery.twitch_authors("ESP"), ["fixture_es", "77"])


def twitch_stream(stream, user, code, title, category):
    return {"id": stream, "user_id": user, "type": "live", "title": title, "game_name": category,
            "language": code, "viewer_count": 125}


def twitch_archive(video, stream, user, code, title, age=timedelta(hours=1)):
    created = (datetime.now(timezone.utc) - age).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {"id": video, "stream_id": stream, "user_id": user, "type": "archive",
            "title": title, "language": code, "created_at": created, "viewable": "public", "view_count": 890}


class TwitchDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.env = {"TWITCH_CLIENT_ID": "fixture-client", "TWITCH_CLIENT_SECRET": "fixture-secret",
                    "STREAMCUT_DISCOVERY_MODE": "regional",
                    "STREAMCUT_TWITCH_RU": " @Fixture_RU ", "STREAMCUT_TWITCH_EN": "22",
                    "STREAMCUT_TWITCH_ESP": "fixture_es"}
        self.users = [{"id": "11", "login": "fixture_ru"}, {"id": "22", "login": "fixture_en"},
                      {"id": "33", "login": "fixture_es"}]

    def test_live_to_archive_updates_one_persisted_source_per_broadcast(self):
        live = [
            twitch_stream("101", "11", "ru", "Первый запуск", "Grand Theft Auto VI"),
            twitch_stream("102", "22", "en", "GTA6 trailer reactions", "Just Chatting"),
            twitch_stream("103", "33", "es", "GTA‑VI primeras reacciones", ""),
        ]
        streams = live + [
            live[0],
            twitch_stream("104", "11", "ru", "GTA 6 en el descanso", "Grand Theft Auto V"),
            twitch_stream("105", "22", "es", "GTA 6", "Grand Theft Auto VI"),
            twitch_stream("106", "33", "es", "Otro juego | etiquetas: GTA VI", "Just Chatting"),
            twitch_stream("107", "44", "es", "GTA 6", "Grand Theft Auto VI"),
        ]
        vods = [twitch_archive(f"20{i}", item["id"], item["user_id"], item["language"], item["title"])
                for i, item in enumerate(live, 1)]
        vods_by_user = {"11": [vods[0], vods[0], {**vods[0], "id": "300"},
                              {**vods[0], "id": "301", "stream_id": "999"}],
                        "22": [vods[1]], "33": [vods[2]]}

        def response(_, host, method, path, *args):
            if host == "id.twitch.tv":
                self.assertEqual((method, path), ("POST", "/oauth2/token"))
                return reply({"access_token": "fixture-token"})
            self.assertEqual((host, method), ("api.twitch.tv", "GET"))
            endpoint = urlsplit(path)
            query = parse_qs(endpoint.query)
            if endpoint.path == "/helix/users":
                self.assertEqual(query, {"id": ["22"], "login": ["fixture_ru", "fixture_es"]})
                return reply({"data": self.users})
            if endpoint.path == "/helix/games":
                return reply({"data": []})
            if endpoint.path == "/helix/streams":
                self.assertEqual(len(query["user_id"]), 1)
                return reply({"data": streams, "pagination": {}})
            self.assertEqual(endpoint.path, "/helix/videos")
            self.assertEqual(query["type"], ["archive"])
            archives = vods_by_user[query["user_id"][0]]
            self.assertEqual(query["language"], [archives[0]["language"]])
            return reply({"data": archives, "pagination": {}})

        directory = self.enterContext(TemporaryDirectory())
        with patch.dict(os.environ, self.env, clear=True), patch.object(
                discovery, "request", side_effect=response):
            service = studio.Studio(Path(directory))
            self.addCleanup(service.close)
            service.search(force=True, live_only=True)
            before = {row["lang"]: (row["id"], row["url"], row["kind"]) for row in service.snapshot()["sources"]}
            self.assertEqual({lang: (url, kind) for lang, (_, url, kind) in before.items()}, {
                "RU": ("https://www.twitch.tv/fixture_ru", "live"),
                "EN": ("https://www.twitch.tv/fixture_en", "live"),
                "ESP": ("https://www.twitch.tv/fixture_es", "live"),
            })
            streams.clear()
            service.close()
            service = studio.Studio(Path(directory))
            self.addCleanup(service.close)
            service.search(force=True)
            service.search(force=True)
            rows = service.snapshot()["sources"]
            self.assertEqual(len(rows), 3)
            self.assertEqual({row["lang"]: (row["id"], row["url"], row["kind"]) for row in rows}, {
                lang: (before[lang][0], f"https://www.twitch.tv/videos/{vod['id']}", "vod")
                for lang, vod in zip(studio.LANGS, vods)
            })

    def test_archives_apply_topic_language_account_visibility_and_recency_filters(self):
        item = twitch_archive("201", "101", "22", "en", "GTA VI highlights")
        archives = [
            item, item, {**item, "id": "202"},
            {**item, "id": "203", "stream_id": None},
            {**item, "id": "204", "stream_id": "104", "viewable": "private"},
            {**item, "id": "205", "stream_id": "105", "language": "es"},
            {**item, "id": "206", "stream_id": "106", "user_id": "11"},
            {**item, "id": "207", "stream_id": "107", "type": "upload"},
            {**item, "id": "208", "stream_id": "108", "title": "GTA V | tags: GTA6"},
            twitch_archive("209", "109", "22", "en", "GTA VI", age=timedelta(days=10)),
        ]
        def response(_route, host, _method, path, _headers, *_args):
            if host == "id.twitch.tv":
                return reply({"access_token": "fixture-token"})
            endpoint = urlsplit(path)
            if endpoint.path == "/helix/users":
                return reply({"data": [self.users[1]]})
            if endpoint.path in {"/helix/games", "/helix/streams"}:
                return reply({"data": []})
            self.assertEqual(endpoint.path, "/helix/videos")
            self.assertEqual(parse_qs(endpoint.query)["user_id"], ["22"])
            return reply({"data": archives})

        current = studio.now()
        with patch.dict(os.environ, self.env, clear=True), patch.object(
                discovery, "request", side_effect=response):
            candidates = regional.current_candidates(
                regional.twitch_markets({"EN": "en"}, current, set())["EN"], current)
        self.assertEqual([(row["lang"], row["url"], row["kind"], row["key"]) for row in candidates], [
            ("EN", "https://www.twitch.tv/videos/201", "vod", "twitch:stream:101"),
            ("EN", "https://www.twitch.tv/videos/203", "vod", "twitch:vod:203"),
        ])


if __name__ == "__main__":
    unittest.main()
