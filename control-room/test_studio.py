"""Regression tests for the route gate and the persistent publishing queue."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import schedule
import studio
import transport


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = studio.Studio(Path(self.temp.name))
        self.addCleanup(self.s.close)
        for lang, suffix in (("RU", "r"), ("EN", "e"), ("ESP", "s")):
            self.s.add_account(dict(lang=lang, platform="YouTube", name=lang, route="server", target="UC" + suffix * 22))
        (self.s.output / "clip.mp4").write_bytes(b"fixture")
        with self.s.db() as db:
            account = db.execute("SELECT id FROM accounts WHERE lang='RU'").fetchone()[0]
            db.execute("UPDATE accounts SET auto=1,refresh_token='fixture' WHERE id=?", (account,))
            db.execute("INSERT INTO clips(lang,title,relative_path,verified) VALUES ('RU','Тест','clip.mp4',1)")
            with patch.object(studio.Studio, "uploads_enabled", return_value=True):
                self.s.populate(db)
                self.s.populate(db)
            db.execute("UPDATE queue SET at=?", (studio.iso(studio.now() - timedelta(hours=1)),))

    def test_language_dedup_and_preflight_failure(self):
        self.assertEqual([(q["lang"], q["status"]) for q in self.s.snapshot()["queue"]], [("RU", "scheduled")])
        with patch.object(studio.Studio, "uploads_enabled", return_value=True), \
                patch("transport.prepare_upload", side_effect=transport.ConnectionProblem("wrong region")), \
                patch("transport.upload_private") as upload:
            self.s.publish_one()
        upload.assert_not_called()
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "blocked")
        self.assertNotIn("refresh_token", self.s.snapshot()["accounts"][0])

    def test_paused_then_confirmation_persists(self):
        with patch.object(studio.Studio, "uploads_enabled", return_value=True), \
                patch("transport.prepare_upload", return_value="access"), \
                patch("transport.upload_private", return_value="video-a") as upload:
            self.s.change_setting("paused", True)
            self.s.publish_one()
            upload.assert_not_called()
            self.s.change_setting("paused", False)
            self.s.publish_one()
            self.s.publish_one()
            upload.assert_called_once()
        job = self.s.snapshot()["queue"][0]
        self.assertEqual((job["status"], job["external_id"]), ("uploaded", "video-a"))

    def test_uncertain_outcome_is_not_retried(self):
        with patch.object(studio.Studio, "uploads_enabled", return_value=True), \
                patch("transport.prepare_upload", return_value="access"), \
                patch("transport.upload_private", side_effect=transport.ConnectionProblem("timeout")) as upload:
            self.s.publish_one()
            self.s.publish_one()
        upload.assert_called_once()
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "unknown")

    def test_changed_route_requires_review_of_pending_job(self):
        account = next(row for row in self.s.snapshot()["accounts"] if row["lang"] == "RU")
        self.s.add_account(dict(id=account["id"], lang="RU", platform="YouTube", name="RU",
                                route="ES", target="UC" + "r" * 22))
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "review")

    def test_route_failure_blocks_and_recovery_moves_overdue_job_forward(self):
        with patch.object(studio.Studio, "uploads_enabled", return_value=True), \
                patch("transport.prepare_upload", side_effect=transport.RouteProblem("route went down")), \
                patch("transport.upload_private") as upload:
            self.s.publish_one()
        upload.assert_not_called()
        job = self.s.snapshot()["queue"][0]
        self.assertEqual(job["status"], "route_wait")
        self.s.record_connection(dict(route="server", platform="YouTube", state="ok",
                                      country="RU", detail="", checked_at=studio.iso(studio.now())))
        retried = self.s.snapshot()["queue"][0]
        self.assertEqual(retried["status"], "scheduled")
        self.assertGreater(datetime.fromisoformat(retried["at"]), studio.now())

    def test_second_account_is_kept_and_clip_assigns_once_per_platform(self):
        self.s.add_account(dict(lang="RU", platform="YouTube", name="Second RU", route="server",
                                target="UC" + "n" * 22))
        self.s.add_account(dict(lang="RU", platform="Reels", name="RU Instagram", route="server",
                                target="@ru_test"))
        data = self.s.snapshot()
        self.assertEqual(sum(a["lang"] == "RU" and a["platform"] == "YouTube" for a in data["accounts"]), 2)
        self.assertEqual({q["platform"] for q in data["queue"]}, {"YouTube", "Reels"})
        self.assertEqual(len(data["queue"]), 2)
        self.assertEqual(data["queue_counts"], {"RU": 1, "EN": 0, "ESP": 0})


class SourceTests(unittest.TestCase):
    def test_upload_attaches_selected_event_preserves_previous_content_and_rejects_duplicate(self):
        with TemporaryDirectory() as directory:
            service = studio.Studio(Path(directory))
            try:
                live = {"lang": "ESP", "title": "Especial GTA 6", "url": "https://twitch.tv/illojuan",
                        "kind": "live"}
                service.add_source(live, key_override="twitch:stream:1")
                service.add_source(live, key_override="twitch:stream:2")
                source = service.snapshot()["sources"][0]
                req = {**live, "id": source["id"], "name": "test.mp4"}
                received = service.receive_video(BytesIO(b"test-bytes"), 10, req)
                self.assertEqual(received["name"], "test.mp4")
                states = {row["id"]: row["status"] for row in service.snapshot()["sources"]}
                self.assertEqual(states[source["id"]], "queued")
                self.assertEqual(list(states.values()).count("waiting"), 1)
                with self.assertRaisesRegex(ValueError, "уже был добавлен"):
                    service.receive_video(BytesIO(b"test-bytes"), 10, {"lang":"ESP", "name":"test.mp4"})
                self.assertEqual([p.name for p in (service.input / "ESP").iterdir()], ["test.mp4"])
            finally:
                service.close()

    def test_migration_preserves_queue_and_allows_second_channel(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            with sqlite3.connect(path / "studio.db") as db:
                db.executescript(studio.SCHEMA.replace("UNIQUE(platform, target)", "UNIQUE(lang, platform), UNIQUE(platform, target)"))
                db.execute("INSERT INTO accounts(id,lang,platform,name,target,route) VALUES (5,'EN','Reels','Old','@a','server')")
                db.execute("INSERT INTO clips(id,lang,title,relative_path) VALUES (7,'EN','Clip','clip.mp4')")
                db.execute("INSERT INTO queue(clip_id,account_id,at,status) VALUES (7,5,?,'review')", (studio.iso(studio.now()),))
            service = studio.Studio(path)
            try:
                service.add_account(dict(lang="EN", platform="Reels", name="Other", target="@b", route="server"))
                self.assertEqual([a["id"] for a in service.snapshot()["accounts"]], [5, 6])
                self.assertEqual(service.snapshot()["queue"][0]["account"], "Old")
                with service.db() as db:
                    self.assertIsNone(db.execute("PRAGMA foreign_key_check").fetchone())
            finally:
                service.close()


class ScheduleTests(unittest.TestCase):
    def test_same_local_time_maps_to_different_cities_and_seasons(self):
        expected = {"RU": 6, "EN": 14, "ESP": 8}
        for lang, hour in expected.items():
            self.assertEqual(schedule.local_time("2026-11-19T09:00", lang).hour, hour)
        self.assertEqual(schedule.local_time("2026-07-19T09:00", "EN").hour, 13)
        self.assertEqual(schedule.local_time("2026-07-19T09:00", "ESP").hour, 7)

    def test_rejects_nonexistent_and_ambiguous_wall_time(self):
        for value in ("2026-03-08T02:30", "2026-11-01T01:30"):
            with self.assertRaises(ValueError):
                schedule.local_time(value, "EN")
        start, end = schedule.civil_day(datetime(2026, 3, 8, 18, tzinfo=timezone.utc), "EN")
        self.assertEqual(end - start, timedelta(hours=23))



class TransportTests(unittest.TestCase):
    def test_mismatch_does_not_contact_platform(self):
        with patch("transport.request", return_value=(200, {}, b"ip=8.8.8.8\nloc=US\n")) as request:
            with self.assertRaisesRegex(transport.ConnectionProblem, "ожидается ES"):
                transport.check_route("ES", "YouTube")
        request.assert_called_once_with("ES", "www.cloudflare.com", "GET", "/cdn-cgi/trace")

    def test_both_requests_use_selected_route(self):
        with patch("transport.request", side_effect=[(200, {}, b"ip=8.8.8.8\nloc=ES\n"),
                                                      (403, {}, b"")]) as request:
            result = transport.check_route("ES", "Reels")
        self.assertEqual(result["country"], "ES")
        self.assertEqual(request.call_args.args, ("ES", "graph.instagram.com", "GET", "/"))

    def test_no_credential_forward_to_unexpected_upload_host(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "test.mp4"
            path.write_bytes(b"fixture")
            reply = (200, {"Location": "https://example.com/upload/session"}, b"")
            with patch("transport.request", return_value=reply) as request:
                with self.assertRaises(transport.ConnectionProblem):
                    transport.upload_private("server", "fixture-token", "Clip", "EN", path)
            request.assert_called_once()


if __name__ == "__main__":
    unittest.main()
