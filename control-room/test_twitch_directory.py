"""Test discovery against the visible language-filtered Twitch directory."""

from datetime import datetime, timedelta, timezone
import json
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


def paged(rows, cursor=None):
    return {"data": rows, "pagination": {"cursor": cursor} if cursor else {}}


def paging_from_helix(endpoint, params, headers, action):
    return paged(discovery.helix(endpoint, params, headers, action))


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
                self.assertEqual(options["language"], language)
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

    def test_ru_pilot_prioritizes_configured_authors_but_checks_the_whole_category(self):
        current = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)
        videos_requested = []

        def feed(endpoint, params, _headers, _action):
            options = dict(params)
            if endpoint == "users":
                self.assertEqual(params, [("login", "creator_6"), ("login", "creator_4")])
                return [{"id": str(user), "login": f"creator_{user}"} for user in (6, 4)]
            if endpoint == "games":
                return [{"id": GAME, "name": "Grand Theft Auto VI"}]
            if endpoint == "streams":
                self.assertEqual((options["language"], options["game_id"]), ("ru", GAME))
                if "user_id" in options:
                    self.assertEqual([value for key, value in params if key == "user_id"], ["6", "4"])
                    return [live(4, "ru", 25)]
                return [live(1, "ru", 400), live(2, "en", 600)]
            self.assertEqual(options["language"], "ru")
            if "game_id" in options:
                return [archive(5, "ru", current), archive(7, "en", current),
                        archive(8, "ru", current, viewable="private"), archive(1, "ru", current)]
            if options["sort"] != "time":
                return []
            user = options["user_id"]
            videos_requested.append(user)
            return {"1": [archive(1, "ru", current)],
                    "4": [archive(4, "ru", current), archive(4, "ru", current, id="802", stream_id="1802")],
                    "6": [archive(6, "ru", current, title="Проходим GTA VI")]}.get(user, [])

        env = SECRETS | {twitch_directory.STRICT_RU: "1", "STREAMCUT_TWITCH_RU": "creator_6|creator_4"}
        with patch.dict(os.environ, env, clear=True), patch(
                "discovery.twitch_headers", return_value={}), patch("discovery.helix", side_effect=feed), patch(
                "twitch_clips.helix_page", side_effect=paging_from_helix), patch(
                "twitch_clips.search", return_value=[]), patch("discovery.request") as direct:
            found = twitch_directory.search(LANGUAGES, current)
        direct.assert_not_called()
        self.assertEqual(videos_requested, ["1", "6", "4"])
        self.assertEqual([(row["twitch_user"], row.get("directory_rank"))
                          for row in found["RU"] if row["kind"] == "live"], [("4", None), ("1", 1)])
        self.assertEqual([(row["archive_id"], row.get("directory_priority"))
                          for row in found["RU"] if row["kind"] == "vod"],
                         [("3006", 1), ("3004", 2), ("3001", None), ("3005", None)])
        self.assertEqual((found["EN"], found["ESP"]), ([], []))

    def test_ru_directory_without_a_priority_list_checks_recent_offline_archives(self):
        current = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)
        env = SECRETS | {twitch_directory.STRICT_RU: "1"}
        with patch.dict(os.environ, env, clear=True), patch(
                "discovery.twitch_headers", return_value={}), patch("twitch_clips.search", return_value=[]), patch(
                "twitch_clips.helix_page", side_effect=paging_from_helix), patch(
                "discovery.helix", side_effect=[
                    [{"id": GAME, "name": "Grand Theft Auto VI"}], [],
                    [archive(10, "ru", current)], [archive(10, "ru", current)],
                ]) as helix:
            found = twitch_directory.search(LANGUAGES, current)
        self.assertEqual([call.args[0] for call in helix.call_args_list], ["games", "streams", "videos", "videos"])
        self.assertEqual([entry["archive_id"] for entry in found["RU"]], ["3010"])
        self.assertEqual((found["EN"], found["ESP"]), ([], []))

    def test_ru_directory_rechecks_cached_archives_and_drops_unavailable_or_expired(self):
        current = datetime(2026, 11, 19, 9, tzinfo=timezone.utc)
        env = SECRETS | {twitch_directory.STRICT_RU: "1"}

        def feed(endpoint, params, _headers, _action):
            options = dict(params)
            if endpoint == "games":
                return [{"id": GAME, "name": "Grand Theft Auto VI"}]
            if endpoint == "streams" or "game_id" in options:
                return []
            self.assertEqual(params, [("id", "3010"), ("id", "3011"), ("id", "3012")])
            return [archive(10, "ru", current), archive(12, "ru", current, viewable="private")]

        prior = [
            dict(regional.twitch_item(row, "RU", f"player_{row['user_id']}", current, live=False, category=True),
                 archive_id=row["id"], stream_id=row["stream_id"], game_id=GAME, directory_followup=True,
                 directory_priority=1) for row in (archive(10, "ru", current), archive(11, "ru", current),
                                                 archive(12, "ru", current), archive(13, "ru", current))
        ]
        prior[-1]["observed"] = (current - timedelta(days=4)).isoformat()
        with patch.dict(os.environ, env, clear=True), patch(
                "discovery.twitch_headers", return_value={}), patch("discovery.helix", side_effect=feed), patch(
                "twitch_clips.helix_page", side_effect=paging_from_helix), patch("twitch_clips.search", return_value=[]):
            found = twitch_directory.search(LANGUAGES, current, previous={"RU": prior})
        self.assertEqual([item["archive_id"] for item in found["RU"]], ["3010"])
        self.assertNotIn("directory_priority", found["RU"][0])

    def test_ru_poll_rotates_paged_variants_and_keeps_only_ten_display_ranks(self):
        current = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        env = SECRETS | {twitch_directory.STRICT_RU: "1"}
        recording_queries = []
        clip = dict(lang="RU", kind="video", provider="Twitch", directory_followup=True, game_id=GAME,
                    twitch_user="201", key="twitch:clip:example", clip_id="example", observed=current.isoformat())

        def helix(endpoint, params, _headers, _action):
            if endpoint == "games":
                return []
            if endpoint == "search/categories":
                return [{"id": "501", "name": "Grand Theft Auto V"}, {"id": GAME, "name": "Grand Theft Auto VI"}]
            if endpoint == "videos":
                return []
            self.fail(f"Unexpected endpoint {endpoint}")

        def page(endpoint, params, _headers, _action):
            options = dict(params)
            if endpoint == "streams":
                cursor = options.get("after")
                if cursor:
                    self.assertEqual(cursor, "more-streams")
                    return paged([live(9, "ru", 10), live(101, "ru", 5), live(102, "en", 5000)])
                return paged([live(user, "ru", 100 - user) for user in range(1, 11)], "more-streams")
            self.assertEqual(options["language"], "ru")
            if "user_id" in options:
                return paged([])
            recording_queries.append((options["sort"], options["period"], options.get("after")))
            if options["sort"] == "time":
                return paged([])
            if options.get("after"):
                self.assertEqual(options["after"], "more-archives")
                return paged([archive(200, "ru", current)])
            return paged([], "more-archives")

        with patch.dict(os.environ, env, clear=True), patch("discovery.twitch_headers", return_value={}), patch(
                "discovery.helix", side_effect=helix), patch("twitch_clips.helix_page", side_effect=page), patch(
                "twitch_clips.search", side_effect=[[clip.copy(), clip.copy()], []]):
            found = twitch_directory.search(LANGUAGES, current)["RU"]
            next_poll = twitch_directory.search(LANGUAGES, current + timedelta(minutes=30), previous={"RU": found})["RU"]
        self.assertEqual([row["directory_rank"] for row in found if "directory_rank" in row], list(range(1, 11)))
        self.assertTrue(any(row["twitch_user"] == "101" and row["directory_followup"] for row in found))
        self.assertFalse(any(row["twitch_user"] == "102" for row in found))
        self.assertEqual([row["archive_id"] for row in found if row["kind"] == "vod"], ["3200"])
        self.assertEqual(sum(row.get("clip_id") == "example" for row in found), 1)
        self.assertEqual(sum(row.get("clip_id") == "example" for row in next_poll), 1)
        self.assertEqual(recording_queries[0], ("time", "week", None))
        self.assertEqual(recording_queries[1][:2], recording_queries[2][:2])
        self.assertEqual(recording_queries[2][2], "more-archives")
        self.assertEqual(recording_queries[3], recording_queries[0])
        self.assertNotEqual(recording_queries[4][:2], recording_queries[1][:2])

    def test_rapid_ru_polls_rotate_archive_variants_and_channels_every_five_minutes(self):
        current = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        env = SECRETS | {twitch_directory.STRICT_RU: "1"}
        orders = []
        checked_users = []

        def page(endpoint, params, _headers, _action):
            if endpoint == "streams":
                return paged([live(user, "ru", 50 - user) for user in range(1, 35)])
            options = dict(params)
            if "game_id" in options and options["sort"] != "time":
                orders.append((options["sort"], options["period"]))
            return paged([])

        def records(user, _code, _headers):
            checked_users[-1].append(user)
            return []

        with patch.dict(os.environ, env, clear=True), patch("discovery.twitch_headers", return_value={}), patch(
                "twitch_directory.category_id", return_value=GAME), patch(
                "twitch_directory.archive_rows", side_effect=records), patch(
                "twitch_clips.helix_page", side_effect=page), patch("twitch_clips.search", return_value=[]):
            for minute in range(0, 20, 5):
                checked_users.append([])
                twitch_directory.search(LANGUAGES, current + timedelta(minutes=minute), rapid=True)

        self.assertEqual(set(orders), set(twitch_directory.ARCHIVE_ORDERS))
        first, second = checked_users[:2]
        self.assertEqual(first[:10], second[:10])
        self.assertNotEqual(set(first[10:]), set(second[10:]))

    def test_directory_pagination_uses_helix_response_data_and_cursor(self):
        responses = [paged([{"id": "1"}], "next+page"), paged([{"id": "2"}])]
        with patch("discovery.request", side_effect=[
            (200, {}, json.dumps(response).encode()) for response in responses
        ]) as request:
            rows = twitch_directory.directory_pages("videos", [("language", "ru")], {}, "Каталог", 3)
        self.assertEqual(rows, [{"id": "1"}, {"id": "2"}])
        self.assertEqual([call.args[3] for call in request.call_args_list],
                         ["/helix/videos?language=ru", "/helix/videos?language=ru&after=next%2Bpage"])

    def test_ru_poll_refreshes_first_page_then_resumes_deeper_catalog_from_saved_cursor(self):
        current = datetime(2026, 11, 20, 8, tzinfo=timezone.utc)
        requests = []

        def page(endpoint, params, _headers, _description):
            if endpoint != "streams":
                return paged([])
            cursor = dict(params).get("after")
            requests.append(cursor)
            return {
                None: paged([live(1, "ru", 100)], "second"),
                "second": paged([live(2, "ru", 40)], "third"),
                "third": paged([live(3, "ru", 10)]),
            }[cursor]

        env = SECRETS | {twitch_directory.STRICT_RU: "1"}
        with patch.dict(os.environ, env, clear=True), patch("discovery.twitch_headers", return_value={}), patch(
                "twitch_directory.category_id", return_value=GAME), patch("twitch_directory.archive_rows", return_value=[]), patch(
                "twitch_clips.search", return_value=[]), patch("twitch_directory.CATEGORY_STREAM_PAGES", 2), patch(
                "twitch_clips.helix_page", side_effect=page):
            first = twitch_directory.search(LANGUAGES, current)
            second = twitch_directory.search(LANGUAGES, current + timedelta(minutes=30), previous=first)
            twitch_directory.search(LANGUAGES, current + timedelta(hours=1), previous=second)

        self.assertEqual(requests, [None, "second", None, "third", None, "second"])
        self.assertEqual(first["RU"][0]["directory_after"], "third")
        self.assertTrue(any(row["twitch_user"] == "3" for row in second["RU"]))
        self.assertTrue(all("directory_after" not in row for row in second["RU"]))

    def test_an_expired_stream_cursor_returns_to_the_fresh_page_chain(self):
        requests = []

        def page(_endpoint, params, _headers, _description):
            cursor = dict(params).get("after")
            requests.append(cursor)
            if cursor == "expired":
                raise discovery.ConnectionProblem("Invalid cursor")
            return paged([{"id": "head"}], "fresh") if cursor is None else paged([{"id": "deep"}])

        with patch("twitch_clips.helix_page", side_effect=page):
            pages, cursor = twitch_directory.browse_directory_pages("streams", [], {}, "Каталог", 3,
                                                                    previous_cursor="expired")
        self.assertEqual(requests, [None, "expired", "fresh"])
        self.assertEqual(pages, [[{"id": "head"}], [{"id": "deep"}]])
        self.assertIsNone(cursor)

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
