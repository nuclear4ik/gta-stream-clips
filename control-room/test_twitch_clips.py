"""Cover standalone category clip discovery through the official Twitch API."""

from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import parse_qsl, urlsplit
import unittest
from unittest.mock import patch

import twitch_clips


GAME = "610"
CURRENT = datetime(2026, 11, 20, 9, 30, 15, tzinfo=timezone.utc)


def clip(identifier, minutes_ago=20, **fields):
    return dict(id=identifier, game_id=GAME, broadcaster_id="42", broadcaster_name="Стример",
                language="ru", created_at=(CURRENT - timedelta(minutes=minutes_ago)).isoformat(),
                title="А что там за поворотом?", duration=50.5, view_count=200) | fields


class TwitchClipSearchTests(unittest.TestCase):
    def test_category_search_follows_short_pages_and_checks_actual_metadata(self):
        requested = []
        headers = {"Client-Id": "fixture", "Authorization": "Bearer fixture"}

        def answer(route, host, method, path, request_headers):
            self.assertEqual((route, host, method), ("server", "api.twitch.tv", "GET"))
            self.assertEqual(request_headers, headers)
            url = urlsplit(path)
            self.assertEqual(url.path, "/helix/clips")
            options = dict(parse_qsl(url.query))
            self.assertEqual(options["game_id"], GAME)
            self.assertEqual(options["first"], "100")
            requested.append(options)
            if options.get("after") == "continue":
                rows = [clip("recent"), clip("second", 21, duration=25), clip("wrong-range", 190)]
                cursor = None
            elif len(requested) == 1:
                rows = [clip("recent"), clip("foreign", language="en"), clip("short", duration=24.99),
                        clip("long", duration=60.01), clip("wrong-game", game_id="611"),
                        clip("bad/id"), clip("private", is_public=False), clip("hidden", viewable="private")]
                cursor = "continue"
            elif len(requested) == 3:
                rows = [clip("older", 370, duration=60)]
                cursor = None
            else:
                rows, cursor = [], None
            return 200, {}, json.dumps({"data": rows, "pagination": {"cursor": cursor}}).encode()

        with patch("twitch_clips.discovery.request", side_effect=answer):
            found = twitch_clips.search(GAME, headers, CURRENT)

        self.assertEqual([item["clip_id"] for item in found], ["recent", "second", "older"])
        self.assertEqual([item["url"] for item in found], [
            "https://clips.twitch.tv/recent", "https://clips.twitch.tv/second", "https://clips.twitch.tv/older",
        ])
        self.assertTrue(all(item["kind"] == "video" and item["provider"] == "Twitch"
                            and item["lang"] == "RU" and item["directory_followup"] and item["twitch_user"] == "42"
                            for item in found))
        self.assertEqual(len(requested), 5)
        self.assertEqual(requested[0]["started_at"], "2026-11-20T08:30:00Z")
        self.assertEqual(requested[0]["ended_at"], "2026-11-20T09:30:00Z")
        self.assertEqual(requested[1]["after"], "continue")

    def test_repeating_cursor_stops_and_other_windows_share_a_fixed_budget(self):
        seen = Counter()

        def answer(_endpoint, params, _headers, _description):
            start = dict(params)["started_at"]
            seen[start] += 1
            cursor = "repeated" if start == "2026-11-20T08:30:00Z" else str(seen[start])
            return {"data": [], "pagination": {"cursor": cursor}}

        with patch("twitch_clips.helix_page", side_effect=answer):
            self.assertEqual(twitch_clips.search(GAME, {}, CURRENT), [])
        self.assertEqual(list(seen.values()), [2, 3, 3, 3])

    def test_older_windows_move_between_scheduled_polls(self):
        first = list(twitch_clips.search_windows(CURRENT))
        second = list(twitch_clips.search_windows(CURRENT + timedelta(minutes=30)))
        self.assertTrue(set(first).isdisjoint(second))
        self.assertTrue(all(second_start - first_start == timedelta(minutes=30)
                            for (first_start, _), (second_start, _) in zip(first, second)))
        self.assertTrue(all(CURRENT - timedelta(days=3) <= start < end <= CURRENT for start, end in first))

    def test_failed_recent_window_does_not_prevent_trying_older_searches(self):
        error = twitch_clips.discovery.ConnectionProblem("Twitch temporarily unavailable")
        with patch("twitch_clips.helix_page", side_effect=[
                error, {"data": [clip("older", 370)]}, {"data": []}, {"data": []},
        ]) as query:
            found = twitch_clips.search(GAME, {}, CURRENT)
        self.assertEqual([item["clip_id"] for item in found], ["older"])
        self.assertEqual(query.call_count, 4)
