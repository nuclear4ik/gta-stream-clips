"""Exercise ranked markets, restart intervals and authorized inbox correlation."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import regional
import studio
from transport import ConnectionProblem


def row(lang, number, current, strength, *, kind="video"):
    video_id = str(number).rjust(11, "0")
    return regional.candidate(lang, f"Creator {number}", f"creator:{number}", f"GTA 6 with {number}",
                              f"https://www.youtube.com/watch?v={video_id}", kind,
                              f"youtube:{video_id}", "YouTube", current, strength)


class RegionalTests(unittest.TestCase):
    def test_bounded_youtube_accepts_unknown_languages_and_attributes_reupload(self):
        clock = datetime.now(timezone.utc)
        video_ids = ["abcdefghijkl"[:11], "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"]
        seen = clock - timedelta(hours=2)

        def response(_route, host, _method, path, _headers):
            self.assertEqual(host, "www.googleapis.com")
            url = urlsplit(path)
            params = parse_qs(url.query)
            if url.path.endswith("/search"):
                self.assertEqual(params["regionCode"], ["RU"])
                self.assertEqual(params["relevanceLanguage"], ["ru"])
                items = [{"id": {"videoId": vid}} for vid in video_ids]
            else:
                variants = [
                    ("Бустер проходит ГТА 6", "Архив", ""),
                    ("Начинаем ГТА 6", "Новый автор", "ru-RU"),
                    ("GTA 6 estreno", "Nuevo", "es"),
                    ("ГТА 6 | #Бустер", "Неизвестный канал", ""),
                ]
                items = [{"id": video, "snippet": dict(title=title, channelTitle=name,
                          channelId="UC" + str(i) * 22, defaultAudioLanguage=code,
                          publishedAt=seen.isoformat()), "statistics": {"viewCount": "1200"}}
                         for i, (video, (title, name, code)) in enumerate(zip(video_ids, variants))]
            return 200, {}, json.dumps({"items": items}).encode()

        with patch.dict(os.environ, {"STREAMCUT_YOUTUBE_KEY": "test"}, clear=True), patch(
                "discovery.request", side_effect=response) as request:
            values = regional.youtube_market("RU", "ru", clock)
        self.assertEqual([item["creator"] for item in values], ["Бустер", "Новый автор"])
        self.assertEqual(request.call_count, 6)  # 2 broad, 3 groups, 1 statistics batch.

    def test_twitch_category_finds_creator_outside_watchlist(self):
        clock = studio.now()

        def get_data(endpoint, params, _headers, _description):
            options = dict(params)
            if endpoint == "games":
                return [{"id": "610", "name": "Grand Theft Auto VI"}]
            self.assertEqual(options["game_id"], "610")
            self.assertEqual(options["language"], "es")
            if endpoint == "streams":
                return [dict(id="162", user_id="701", user_login="desconocido", user_name="Otro creador",
                             language="es", title="Primera partida", type="live", viewer_count=1800)]
            return [dict(id="809", user_id="701", stream_id="161", user_name="Otro creador", language="es",
                         title="Primeras horas", type="archive", viewable="public", view_count=2000,
                         created_at=(clock - timedelta(hours=2)).isoformat())]

        env = {"TWITCH_CLIENT_ID": "fixture", "TWITCH_CLIENT_SECRET": "fixture", "STREAMCUT_TWITCH_ESP": ""}
        with patch.dict(os.environ, env, clear=True), patch("discovery.twitch_headers", return_value={}), patch(
                "discovery.helix", side_effect=get_data):
            entries = regional.twitch_markets({"ESP": "es"}, clock, set())["ESP"]
        self.assertEqual([item["kind"] for item in entries], ["live", "vod"])
        self.assertEqual(regional.leaderboard(entries), [{"author": "twitch:701", "creator": "Otro creador"}])

    def test_ten_ranked_per_market_new_author_displaces_previous(self):
        current = studio.now()
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "STREAMCUT_YOUTUBE_KEY": "test", "STREAMCUT_DISCOVERY_MODE": "regional",
        }, clear=True):
            service = studio.Studio(Path(directory))
            self.addCleanup(service.close)
            service.search_status = ""

            def data(lang, code, instant, **_):
                base = list(studio.LANGS).index(lang) * 100
                return [row(lang, base + n, instant - timedelta(hours=1), float(n))
                        for n in range(1, 12)]

            with patch("regional.youtube_market", side_effect=data) as market:
                service.search()
                self.assertEqual(market.call_count, 3)
                service.search()
                self.assertEqual(market.call_count, 3)
            first = service.snapshot()
            self.assertEqual({lang: len(names) for lang, names in first["roster"].items()},
                             {lang: 10 for lang in studio.LANGS})
            self.assertNotIn("Creator 1", first["roster"]["RU"])

            service.close()
            service = studio.Studio(Path(directory))
            self.addCleanup(service.close)
            with patch("regional.youtube_market") as market:
                service.search()
            market.assert_not_called()

            def update(lang, code, instant, **kwargs):
                if lang == "EN":
                    raise ConnectionProblem("temporary")
                rows = data(lang, code, instant)
                if lang == "RU":
                    rows[0]["popularity"] = 50
                return rows

            with patch("regional.youtube_market", side_effect=update):
                service.search(force=True)
            final = service.snapshot()
            self.assertIn("Creator 1", final["roster"]["RU"])
            self.assertNotIn("Creator 2", final["roster"]["RU"])
            self.assertEqual(final["roster"]["EN"], first["roster"]["EN"])
            self.assertIn("EN · temporary", final["search_status"])

    def test_matching_drop_waits_for_metadata_and_ignores_wrong_language(self):
        with TemporaryDirectory() as directory:
            s = studio.Studio(Path(directory))
            self.addCleanup(s.close)
            file = "twitch-vod-900.mp4"
            for lang in ("EN", "RU"):
                path = s.input / lang / file
                path.write_bytes(lang.encode())
                age = (studio.now() - timedelta(minutes=1)).timestamp()
                os.utime(path, (age, age))
            s.scan()
            self.assertEqual(s.snapshot()["sources"], [])
            s.add_source({"lang": "EN", "title": "GTA VI", "url": "https://www.twitch.tv/videos/900",
                          "kind": "vod"}, key_override="twitch:stream:800", discovered_at=studio.iso(studio.now()))
            s.scan()
            s.scan()
            found, = s.snapshot()["sources"]
            self.assertEqual((found["lang"], found["status"], found["rel_file"]), ("EN", "queued", file))

    def test_stale_automatic_links_do_not_discard_manual_sources(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
            "STREAMCUT_YOUTUBE_KEY": "test", "STREAMCUT_DISCOVERY_MODE": "regional",
        }, clear=True):
            s = studio.Studio(Path(directory))
            self.addCleanup(s.close)
            stale = studio.now() - timedelta(days=5)
            for n in range(100):
                source = row("RU", n, stale, 1)
                s.add_source(source, discovered_at=studio.iso(stale))
            s.add_source({"lang": "EN", "url": "https://youtu.be/abcdefghijk"})
            fresh = row("RU", 101, studio.now() - timedelta(hours=1), 5)
            with patch("regional.youtube_market", side_effect=lambda lang, *_a, **_k: [fresh] if lang == "RU" else []):
                s.search()
            self.assertEqual({q["lang"] for q in s.snapshot()["sources"]}, {"RU", "EN"})
            self.assertEqual(len(s.snapshot()["sources"]), 2)


if __name__ == "__main__":
    unittest.main()
