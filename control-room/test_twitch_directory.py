"""Test discovery against the visible language-filtered Twitch directory."""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import discovery
import regional
from studio import Studio
import twitch_directory


LANGUAGES = {"RU": "ru", "EN": "en", "ESP": "es"}
SECRETS = {"TWITCH_CLIENT_ID": "test", "TWITCH_CLIENT_SECRET": "test"}
GAME = "610"


def live(user, language, viewers):
    return dict(id=str(user + 1000), user_id=str(user), user_login=f"player_{user}", user_name=f"Author {user}",
                type="live", game_id=GAME, language=language, viewer_count=viewers, title="В эфире")


def archive(user, language, current, **fields):
    return dict(id=str(user + 3000), user_id=str(user), stream_id=str(user + 1000),
                user_name=f"Author {user}", type="archive", viewable="public",
                language=language, created_at=(current - timedelta(hours=1)).isoformat(),
                view_count=20, title="Запись прямого эфира") | fields


class TwitchDirectoryTests(unittest.TestCase):
    def test_only_top_ten_broadcasters_per_language_are_queried_for_archives(self):
        offsets = {"ru": 0, "en": 100, "es": 200}
        current = datetime(2026, 11, 19, 8, tzinfo=timezone.utc)
        video_queries = []

        def feed(endpoint, params, _headers, _action):
            options = dict(params)
            if endpoint == "games":
                self.assertEqual(options, {"name": "Grand Theft Auto VI"})
                return [{"id": "501", "name": "Grand Theft Auto V"}, {"id": GAME, "name": "Grand Theft Auto VI"}]
            if endpoint == "streams":
                self.assertEqual(options["game_id"], GAME)
                code = options["language"]
                base = offsets[code]
                entries = [live(base + i, code, 1000 - i) for i in range(12, 0, -1)]
                return entries + [live(base + 1, code, 500),
                                  live(900, code, 5000) | {"game_id": "501"},
                                  live(901, "ja", 4000)]
            if endpoint == "videos":
                self.assertEqual(options["type"], "archive")
                self.assertEqual(options["sort"], "time")
                user = int(options["user_id"])
                video_queries.append(user)
                language = next(code for code, base in offsets.items() if 1 <= user - base <= 10)
                return [archive(user, language, current)]
            self.fail(f"Unexpected endpoint {endpoint}")

        with patch.dict(os.environ, SECRETS, clear=True), patch("discovery.twitch_headers", return_value={}), patch(
                "discovery.helix", side_effect=feed):
            result = twitch_directory.search(LANGUAGES, current)
        self.assertEqual(video_queries, [base + i for base in offsets.values() for i in range(1, 11)])
        for lang, code in LANGUAGES.items():
            entries = result[lang]
            self.assertEqual([entry["kind"] for entry in entries], ["live", "vod"] * 10)
            for rank in range(1, 11):
                stream, recording = entries[(rank - 1) * 2:rank * 2]
                self.assertEqual(stream["key"], recording["key"])
                self.assertEqual(stream["viewer_count"], 1000 - rank)
                self.assertEqual((stream["directory_rank"], recording["directory_rank"]), (rank, rank))
                self.assertEqual(recording["archive_id"], str(offsets[code] + rank + 3000))
            self.assertTrue(all(entry["kind"] == "vod" for entry in regional.current_candidates(entries, current)))

    def test_archive_must_be_public_recent_matching_and_linked_to_gta(self):
        current = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)
        entries = [
            archive(1, "ru", current),
            archive(1, "ru", current, id="20", stream_id="190", title="Исследуем ГТА 6"),
            archive(1, "ru", current, id="21", stream_id="166", title="Архив вчерашнего эфира"),
            archive(1, "ru", current, id="22", stream_id="199", title="GTA V"),
            archive(1, "ru", current, id="23", viewable="private"),
            archive(1, "en", current, id="24"),
            archive(2, "ru", current, id="25", title="GTA VI"),
            archive(1, "ru", current - timedelta(days=4), id="26"),
            archive(1, "ru", current, id="27", type="highlight"),
        ]
        with patch.dict(os.environ, SECRETS, clear=True), patch(
                "discovery.twitch_headers", return_value={}), patch("discovery.helix", side_effect=[
                    [{"id": GAME, "name": "Grand Theft Auto VI"}], [live(1, "ru", 20)], entries,
                ]):
            found = twitch_directory.search({"RU": "ru"}, current, known_live={("RU", "166")})["RU"]
        self.assertEqual([row["archive_id"] for row in found if row["kind"] == "vod"], ["3001", "20", "21"])

    def test_explicit_ru_channels_add_only_relevant_recent_public_archives_to_backlog(self):
        current = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)
        requested = []

        def feed(endpoint, params, _headers, _action):
            requested.append((endpoint, params))
            options = dict(params)
            if endpoint == "games":
                return [{"id": GAME, "name": "Grand Theft Auto VI"}]
            if endpoint == "streams":
                return [live(1, "ru", 20)] if options["language"] == "ru" else []
            if endpoint == "users":
                return [{"id": str(user), "login": f"creator_{user}"} for user in (1, 2, 3, 4)]
            if options["user_id"] == "1":
                return [archive(1, "ru", current)]
            if options["user_id"] == "3":
                raise discovery.ConnectionProblem("Нет ответа Twitch для одного канала.")
            self.assertEqual(options["user_id"], "2")
            return [archive(2, "ru", current, title="Проходим GTA 6"),
                    archive(2, "ru", current, id="601", stream_id="1600"),
                    archive(2, "ru", current, id="602", stream_id="1601", title="GTA V"),
                    archive(2, "ru", current, id="603", title="GTA 6", viewable="private"),
                    archive(2, "en", current, id="604", title="GTA 6"),
                    archive(4, "ru", current, id="605", title="GTA 6"),
                    archive(2, "ru", current - timedelta(days=4), id="606", title="GTA 6")]

        env = SECRETS | {"STREAMCUT_TWITCH_RU": "@Creator_1 | Creator_2 | creator_2 | creator_3 | bad-login"}
        with patch.dict(os.environ, env, clear=True), patch("discovery.twitch_headers", return_value={}), patch(
                "discovery.helix", side_effect=feed):
            found = twitch_directory.search(LANGUAGES, current, known_live={('RU', '1600')})
        self.assertEqual([params for endpoint, params in requested if endpoint == "users"],
                         [[("login", "creator_1"), ("login", "creator_2"), ("login", "creator_3")]])
        self.assertEqual([dict(params)["user_id"] for endpoint, params in requested if endpoint == "videos"],
                         ["1", "2", "3"])
        self.assertEqual([row["archive_id"] for row in found["RU"] if row["kind"] == "vod"], ["3001", "3002", "601"])
        self.assertEqual([row["twitch_user"] for row in found["RU"] if "directory_rank" in row], ["1", "1"])
        self.assertTrue(all(row["directory_followup"] for row in found["RU"] if row["twitch_user"] == "2"))
        self.assertEqual(found["EN"], [])
        self.assertEqual(found["ESP"], [])

    def test_missing_key_and_missing_exact_category(self):
        current = datetime(2026, 11, 19, tzinfo=timezone.utc)
        with patch.dict(os.environ, {}, clear=True), patch("discovery.helix") as helix:
            self.assertIsNone(twitch_directory.search(LANGUAGES, current))
            helix.assert_not_called()
        with patch.dict(os.environ, SECRETS, clear=True), patch(
                "discovery.twitch_headers", return_value={}), patch(
                "discovery.helix", return_value=[{"id": "501", "name": "Grand Theft Auto V"}]) as helix:
            with self.assertRaisesRegex(discovery.ConnectionProblem, "Grand Theft Auto VI"):
                twitch_directory.search(LANGUAGES, current)
            helix.assert_called_once()

    def test_selected_stream_is_checked_after_leaving_rank_and_after_restart(self):
        current = datetime(2026, 11, 19, 8, tzinfo=timezone.utc)
        phase = 0
        video_queries = []

        def feed(endpoint, params, _headers, _action):
            options = dict(params)
            if endpoint == "games":
                return [{"id": GAME, "name": "Grand Theft Auto VI"}]
            if endpoint == "streams":
                if options["language"] != "ru":
                    return []
                return [live(1 if phase == 0 else 2, "ru", 100)]
            user = options["user_id"]
            video_queries.append((phase, user))
            if user == "1" and phase == 1:
                raise discovery.ConnectionProblem("Временно нет ответа Twitch.")
            if user == "1" and phase == 2:
                return [archive(1, "ru", current, id="9999", stream_id="1234", title="Играем в GTA VI"),
                        archive(1, "ru", current)]
            return []

        with TemporaryDirectory() as root, patch.dict(os.environ, SECRETS, clear=True), patch(
                "studio.now", side_effect=lambda: current), patch("discovery.twitch_headers", return_value={}), patch(
                "discovery.helix", side_effect=feed):
            service = Studio(Path(root))
            try:
                service.search(force=True)
                with service.db() as db:
                    initial = db.execute("SELECT id,kind FROM sources WHERE source_key='twitch:stream:1001'").fetchone()
                    self.assertEqual(initial["kind"], "live")
                phase = 1
                current += timedelta(hours=5)
                service.search(force=True)
                with service.db() as db:
                    roster, _, _ = service.current_discovery(db, current)
                self.assertEqual(roster["RU"], ["Author 2"])
            finally:
                service.close()

            phase = 2
            current += timedelta(hours=5)
            service = Studio(Path(root))
            try:
                service.search(force=True)
                phase = 3
                current += timedelta(hours=5)
                service.search(force=True)
                with service.db() as db:
                    roster, _, candidates = service.current_discovery(db, current)
                    resolved = db.execute("SELECT id,kind,url,status FROM sources WHERE id=?",
                                          (initial["id"],)).fetchone()
                    self.assertEqual(tuple(resolved), (initial["id"], "vod", "https://www.twitch.tv/videos/3001", "waiting"))
                self.assertEqual(roster["RU"], ["Author 2"])
                vods = [item for item in candidates["RU"][1] if item["kind"] == "vod"]
                self.assertEqual([item["archive_id"] for item in vods], ["3001"])
                self.assertNotIn("directory_rank", vods[0])
            finally:
                service.close()
        self.assertEqual(video_queries, [(0, "1"), (1, "2"), (1, "1"), (2, "2"), (2, "1"), (3, "2")])

    def test_total_failure_preserves_feed_until_retry_in_five_minutes(self):
        current = datetime(2026, 11, 19, 8, tzinfo=timezone.utc)
        with TemporaryDirectory() as root, patch.dict(os.environ, SECRETS, clear=True), patch(
                "studio.now", side_effect=lambda: current), patch("discovery.twitch_headers", return_value={}), patch(
                "discovery.helix", side_effect=[
                    [{"id": GAME, "name": "Grand Theft Auto VI"}], [live(1, "ru", 100)], [], [], [],
                    discovery.ConnectionProblem("Временно нет ответа Twitch."),
                    [{"id": GAME, "name": "Grand Theft Auto VI"}], [live(2, "ru", 100)], [], [], [], [],
                ]) as helix:
            service = Studio(Path(root))
            try:
                service.search()
                current += timedelta(hours=5)
                service.search()
                self.assertIn("повтор через 5 минут", service.search_status)
                with service.db() as db:
                    roster, _, _ = service.current_discovery(db, current)
                self.assertEqual(roster["RU"], ["Author 1"])
                current += timedelta(minutes=4)
                service.search()
                self.assertEqual(helix.call_count, 6)
                current += timedelta(minutes=1)
                service.search()
                with service.db() as db:
                    roster, _, _ = service.current_discovery(db, current)
                self.assertEqual(roster["RU"], ["Author 2"])
                current += timedelta(minutes=5)
                before = helix.call_count
                service.search()
                self.assertEqual(helix.call_count, before)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()
