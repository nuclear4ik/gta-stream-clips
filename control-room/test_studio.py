"""Regression tests for the route gate and the persistent publishing queue."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import schedule
import studio
import transport
import youtube_release


FIRST_VIDEO = "AbC012_defG"
SECOND_VIDEO = "XYz012_defG"
OTHER_VIDEO = "DEf012_defG"
AUTO_SOURCES = "STREAMCUT_YOUTUBE_AUTO_TWITCH_BROADCASTERS"


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = studio.Studio(Path(self.temp.name))
        self.addCleanup(self.s.close)
        self.clock = datetime(2026, 11, 19, 5, tzinfo=timezone.utc)
        self.start_mock(patch("studio.now", side_effect=lambda: self.clock))
        self.start_mock(patch.dict(os.environ))
        os.environ.pop(AUTO_SOURCES, None)
        os.environ.pop("STREAMCUT_YOUTUBE_REQUIRE_GATEWAY", None)
        os.environ.pop("STREAMCUT_UPLOAD_GATEWAY", None)
        self.start_mock(patch.object(studio.Studio, "uploads_enabled", return_value=True))
        self.start_mock(patch.object(studio.Studio, "shorts_valid", return_value=True))
        self.start_mock(patch("transport.request", side_effect=AssertionError("Unexpected network request")))
        self.prepare = self.start_mock(patch("transport.prepare_upload", return_value="access"))
        self.slots = self.start_mock(patch("youtube_release.scheduled_entries", return_value={}))
        self.slots.side_effect = lambda *_: dict(self.slots.return_value)
        self.status = self.start_mock(patch("youtube_release.publication_status", return_value={}))
        self.upload = self.start_mock(patch("youtube_release.upload", return_value=FIRST_VIDEO))
        for lang, suffix in (("RU", "r"), ("EN", "e"), ("ESP", "s")):
            self.s.add_account(dict(lang=lang, platform="YouTube", name=lang, route="server", target="UC" + suffix * 22))
        (self.s.output / "clip.mp4").write_bytes(b"fixture")
        with self.s.db() as db:
            account = db.execute("SELECT id FROM accounts WHERE lang='RU'").fetchone()[0]
            db.execute("UPDATE accounts SET auto=1,refresh_token='fixture' WHERE id=?", (account,))
            db.execute("INSERT INTO clips(lang,title,relative_path,verified) VALUES ('RU','Тест','clip.mp4',1)")
            self.s.populate(db)
            self.s.populate(db)
            db.execute("UPDATE queue SET at=?", (studio.iso(studio.now() - timedelta(hours=1)),))

    def start_mock(self, patcher):
        self.addCleanup(patcher.stop)
        return patcher.start()

    def set_time(self, identifier, instant):
        with self.s.db() as db:
            db.execute("UPDATE queue SET at=? WHERE id=?", (studio.iso(instant), identifier))

    def add_pending(self, title, instant, broadcaster=None):
        with self.s.db() as db:
            source = None
            if broadcaster is not None:
                source = db.execute("""INSERT INTO sources(lang,title,url,status,created,twitch_broadcaster)
                    VALUES ('RU',?,'https://www.twitch.tv/videos/1','ready',?,?) RETURNING id""",
                    (title, studio.iso(self.clock), broadcaster)).fetchone()[0]
            clip = db.execute("""INSERT INTO clips(source_id,lang,title,relative_path,verified)
                VALUES (?,'RU',?,'clip.mp4',1) RETURNING id""", (source, title)).fetchone()[0]
            self.s.populate(db)
            return db.execute("UPDATE queue SET at=? WHERE clip_id=? RETURNING id",
                              (studio.iso(instant), clip)).fetchone()[0]

    def confirm_last_upload(self, _route, _token, ids):
        planned = self.upload.call_args.kwargs["publish_at"]
        return {video: {"privacyStatus": "private", "publishAt": studio.iso(planned),
                        "uploadStatus": "processed"} for video in ids}

    def connect_next(self):
        self.s.add_account(dict(lang="RU", platform="YouTube", name="Second RU", route="server",
                                target="UC" + "n" * 22))
        with self.s.db() as db:
            return db.execute("""UPDATE accounts SET auto=1,refresh_token='second'
                WHERE name='Second RU' RETURNING id""").fetchone()[0]

    def test_language_dedup_and_preflight_failure(self):
        self.assertEqual([(q["lang"], q["status"]) for q in self.s.snapshot()["queue"]], [("RU", "scheduled")])
        self.prepare.side_effect = transport.ConnectionProblem("wrong region")
        self.s.publish_one()
        self.upload.assert_not_called()
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "blocked")
        self.assertNotIn("refresh_token", self.s.snapshot()["accounts"][0])

    def test_future_upload_preloads_before_deadline_and_persists_public_confirmation(self):
        identifier = self.s.snapshot()["queue"][0]["id"]
        requested = self.clock + timedelta(hours=8)
        self.set_time(identifier, requested)
        self.status.side_effect = self.confirm_last_upload
        self.s.change_setting("paused", True)
        self.s.publish_one()
        self.upload.assert_not_called()
        self.s.change_setting("paused", False)
        self.s.publish_one()
        self.s.publish_one()
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.kwargs["publish_at"], requested)
        self.assertIn("GTA VI", self.upload.call_args.kwargs["description"])
        self.assertIn("#Shorts", self.upload.call_args.kwargs["tags"])
        job = self.s.snapshot()["queue"][0]
        self.assertEqual((job["status"], job["external_id"], job["at"]),
                         ("preloaded", FIRST_VIDEO, studio.iso(requested)))
        self.clock = requested + timedelta(seconds=1)
        self.status.side_effect = None
        self.status.return_value = {FIRST_VIDEO: {"privacyStatus": "public", "uploadStatus": "processed"}}
        self.s.sync_youtube()
        job = self.s.snapshot()["queue"][0]
        self.assertEqual((job["status"], job["external_id"]), ("uploaded", FIRST_VIDEO))
        self.upload.assert_called_once()

    def test_uncertain_outcome_is_not_retried(self):
        self.upload.side_effect = youtube_release.PublishUncertain("timeout")
        self.s.publish_one()
        self.s.publish_one()
        self.upload.assert_called_once()
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "unknown")

    def test_local_review_time_does_not_create_a_youtube_upload(self):
        with self.s.db() as db:
            db.execute("UPDATE queue SET status='review'")
        self.s.publish_one()
        self.prepare.assert_not_called()
        self.upload.assert_not_called()
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "review")

    def test_populate_and_auto_enable_use_exact_trusted_twitch_broadcaster(self):
        os.environ[AUTO_SOURCES] = "21, 10"
        instant = self.clock + timedelta(hours=3)
        approved = self.add_pending("Разрешённый источник", instant, "10")
        unmatched = self.add_pending("Другой источник", instant, "210")
        unknown = self.add_pending("Без связанного источника", instant)
        account = next(a["id"] for a in self.s.snapshot()["accounts"] if a["lang"] == "RU")
        for enable_again in (False, True):
            if enable_again:
                self.s.auto_account(account, False)
                self.s.auto_account(account, True)
            statuses = {job["id"]: job["status"] for job in self.s.snapshot()["queue"]}
            self.assertEqual((statuses[approved], statuses[unmatched], statuses[unknown]),
                             ("scheduled", "review", "review"))

    def test_revoked_automatic_and_legacy_jobs_wait_but_manual_send_is_unchanged(self):
        legacy = self.s.snapshot()["queue"][0]["id"]
        with self.s.db() as db:
            db.execute("UPDATE queue SET auto_scheduled=NULL WHERE id=?", (legacy,))
        instant = self.clock + timedelta(hours=4)
        os.environ[AUTO_SOURCES] = "10"
        automatic = self.add_pending("Разрешение позднее снято", instant, "10")
        manual_at = (instant + timedelta(hours=1)).astimezone(schedule.ZONES["RU"]).strftime("%Y-%m-%dT%H:%M")
        self.s.queue_action({"id": legacy, "action": "send", "at": manual_at})
        blocked = self.add_pending("Другое задание без происхождения", instant)
        with self.s.db() as db:
            db.execute("UPDATE queue SET status='scheduled',auto_scheduled=NULL WHERE id=?", (blocked,))
        os.environ[AUTO_SOURCES] = "11"
        self.status.side_effect = self.confirm_last_upload
        self.s.publish_one()
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.kwargs["title"], "Тест")
        statuses = {job["id"]: job["status"] for job in self.s.snapshot()["queue"]}
        self.assertEqual((statuses[legacy], statuses[automatic], statuses[blocked]),
                         ("preloaded", "review", "review"))

    def test_empty_or_invalid_allowlist_never_opens_automatic_publication(self):
        for value in ("", "10,bad"):
            os.environ[AUTO_SOURCES] = value
            row = self.add_pending("Известный канал", self.clock + timedelta(hours=2), "10")
            self.assertEqual(next(job["status"] for job in self.s.snapshot()["queue"] if job["id"] == row), "review")
        self.s.publish_one()
        self.prepare.assert_not_called()
        self.upload.assert_not_called()

    def test_cancelled_item_frees_inventory_and_draft_does_not_hold_slot(self):
        job = self.s.snapshot()["queue"][0]
        first = self.clock.replace(hour=12)  # 15:00 in Moscow
        self.set_time(job["id"], first)
        with self.s.db() as db:
            self.assertNotEqual(self.s.next_slot(db, job["account_id"], "RU", self.clock), studio.iso(first))
            db.execute("UPDATE queue SET status='review' WHERE id=?", (job["id"],))
            self.assertEqual(self.s.next_slot(db, job["account_id"], "RU", self.clock), studio.iso(first))
            self.assertEqual(self.s.ready_counts(db)["RU"], 1)
        self.s.queue_action({"id": job["id"], "action": "cancel"})
        with self.s.db() as db:
            self.s.populate(db)
            self.assertEqual(self.s.ready_counts(db)["RU"], 0)
        self.assertEqual([item["status"] for item in self.s.snapshot()["queue"]], ["cancelled"])

    def test_rejected_job_releases_inventory_but_an_uncertain_upload_does_not(self):
        job = self.s.snapshot()["queue"][0]
        self.s.upload_status(job["id"], "unknown")
        self.assertEqual(self.s.snapshot()["queue_counts"]["RU"], 1)
        self.s.upload_status(job["id"], "blocked")
        self.assertEqual(self.s.snapshot()["queue_counts"]["RU"], 0)
        self.s.publish_one()
        self.upload.assert_not_called()

    def test_sync_checks_confirmed_uploads_after_first_hundred(self):
        job = self.s.snapshot()["queue"][0]
        moment = studio.iso(self.clock + timedelta(hours=1))
        with self.s.db() as db:
            db.execute("UPDATE queue SET status='preloaded',external_id=? WHERE id=?", (FIRST_VIDEO, job["id"]))
            for index in range(100):
                clip = db.execute("INSERT INTO clips(lang,title,relative_path) VALUES ('RU','Клип','clip.mp4')").lastrowid
                db.execute("""INSERT INTO queue(clip_id,account_id,at,status,external_id)
                    VALUES (?,?,?,'preloaded',?)""", (clip, job["account_id"], moment, f"v{index:010d}"))
        self.s.sync_youtube()
        self.status.assert_called_once()
        checked = self.status.call_args.args[2]
        self.assertEqual(len(checked), 101)
        self.assertIn("v0000000099", checked)

    def test_draft_can_save_a_requested_date_without_reserving_a_full_calendar_day(self):
        draft = self.s.snapshot()["queue"][0]
        first = self.clock.replace(hour=12)
        with self.s.db() as db:
            db.execute("UPDATE queue SET status='review' WHERE id=?", (draft["id"],))
            for index in range(schedule.DAY_LIMIT):
                clip = db.execute("INSERT INTO clips(lang,title,relative_path) VALUES ('RU','Клип','clip.mp4')").lastrowid
                db.execute("""INSERT INTO queue(clip_id,account_id,at,status)
                    VALUES (?,?,?,'scheduled')""", (clip, draft["account_id"],
                                                   studio.iso(first + timedelta(minutes=30 * index))))
        request = {"id": draft["id"], "at": first.astimezone(schedule.ZONES["RU"]).strftime("%Y-%m-%dT%H:%M")}
        self.s.queue_action(request | {"action": "save"})
        with self.assertRaisesRegex(ValueError, "лимит канала"):
            self.s.queue_action(request | {"action": "send"})

    def test_new_broadcaster_approval_uses_existing_render_without_account_toggle(self):
        os.environ[AUTO_SOURCES] = ""
        instant = self.clock + timedelta(hours=3)
        approved = self.add_pending("Архив готов к выпуску", instant, "10")
        waiting = self.add_pending("Архив другого автора", instant, "11")
        self.s.publish_one()
        self.upload.assert_not_called()

        os.environ[AUTO_SOURCES] = "10"
        self.status.side_effect = self.confirm_last_upload
        self.s.publish_one()
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.kwargs["title"], "Архив готов к выпуску")
        statuses = {job["id"]: job["status"] for job in self.s.snapshot()["queue"]}
        self.assertEqual((statuses[approved], statuses[waiting]), ("preloaded", "review"))

    def test_full_remote_slots_wait_then_preload_oldest_in_order(self):
        first = self.s.snapshot()["queue"][0]["id"]
        first_at = self.clock + timedelta(hours=8)
        second_at = self.clock + timedelta(hours=9)
        self.set_time(first, first_at)
        second = self.add_pending("Следующий", second_at)
        self.slots.return_value = {OTHER_VIDEO: studio.iso(self.clock + timedelta(hours=1))}
        self.status.side_effect = self.confirm_last_upload
        self.upload.side_effect = [FIRST_VIDEO, SECOND_VIDEO]
        with patch.object(studio.Studio, "schedule_limit", return_value=1):
            self.s.publish_one()
            self.upload.assert_not_called()
            with self.s.db() as db:
                self.assertTrue(all(row[0] for row in db.execute("SELECT retry_at FROM queue")))
            self.slots.return_value = {}
            third = self.add_pending("Добавлен во время ожидания", self.clock + timedelta(hours=10))
            self.s.publish_one()
            self.upload.assert_not_called()
            self.clock += timedelta(minutes=6)
            self.s.publish_one()
            self.assertEqual((self.upload.call_args.kwargs["title"], self.upload.call_args.kwargs["publish_at"]),
                             ("Тест", first_at))
            self.s.publish_one()
            self.upload.assert_called_once()
            self.clock = first_at + timedelta(minutes=1)
            self.s.publish_one()
            self.assertEqual((self.upload.call_args.kwargs["title"], self.upload.call_args.kwargs["publish_at"]),
                             ("Следующий", second_at))
        queue = {q["id"]: q for q in self.s.snapshot()["queue"]}
        self.assertEqual((queue[first]["external_id"], queue[second]["external_id"]), (FIRST_VIDEO, SECOND_VIDEO))
        self.assertIsNone(queue[third]["external_id"])

    def test_ru_automatic_release_uses_same_half_hour_window_on_any_day(self):
        monday = datetime(2026, 11, 23, 6, tzinfo=timezone.utc)
        saturday = datetime(2026, 11, 21, 6, tzinfo=timezone.utc)
        for now in (monday, saturday):
            requested = now.replace(hour=18, minute=15)
            first_slot = now.replace(hour=12)
            next_slot = first_slot + timedelta(minutes=30)
            job = {"at": studio.iso(requested), "lang": "RU", "auto_scheduled": 1}
            self.assertEqual(self.s.planned_time(job, [], now), first_slot)
            self.assertEqual(self.s.planned_time(job, [studio.iso(first_slot)], now), next_slot)
            job["at"] = studio.iso(next_slot)
            self.assertEqual(self.s.planned_time(job, [studio.iso(first_slot)], now), next_slot)
            job["at"] = studio.iso(requested)
            self.assertEqual(self.s.planned_time(job | {"auto_scheduled": 0}, [], now), requested)

    def test_platform_limit_then_retry_preserves_queue(self):
        self.upload.side_effect = youtube_release.SlotUnavailable("upload cap", retry_after_seconds=90)
        self.s.publish_one()
        with self.s.db() as db:
            waiting = db.execute("SELECT status,retry_at,external_id FROM queue").fetchone()
        self.assertEqual(tuple(waiting), ("scheduled", studio.iso(self.clock + timedelta(seconds=90)), None))
        self.s.publish_one()
        self.upload.assert_called_once()

    def test_primary_fills_its_day_before_assigning_new_clip_to_second_channel(self):
        primary = self.s.snapshot()["queue"][0]["account_id"]
        first = self.s.snapshot()["queue"][0]["id"]
        noon = self.clock.replace(hour=12)
        self.set_time(first, noon)
        backup = self.connect_next()
        with patch.dict(os.environ, {"STREAMCUT_YOUTUBE_DAILY_LIMIT": "2"}):
            second = self.add_pending("Основной", noon + timedelta(minutes=30))
            third = self.add_pending("Запасной", noon)
        assignments = {job["id"]: job["account_id"] for job in self.s.snapshot()["queue"]}
        self.assertEqual(assignments, {first: primary, second: primary, third: backup})

    def test_configured_remote_capacity_moves_only_unsent_automatic_job(self):
        item = self.s.snapshot()["queue"][0]
        backup = self.connect_next()
        self.slots.side_effect = lambda _route, _token, channel, _limit: (
            {OTHER_VIDEO: studio.iso(self.clock + timedelta(hours=8))}
            if channel == "UC" + "r" * 22 else {})
        self.status.side_effect = self.confirm_last_upload
        with patch.object(studio.Studio, "schedule_limit", return_value=1):
            self.s.publish_one()
            self.upload.assert_not_called()
            moved = self.s.snapshot()["queue"][0]
            self.assertEqual((moved["id"], moved["account_id"], moved["status"]),
                             (item["id"], backup, "scheduled"))
            self.s.publish_one()
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.kwargs["expected_channel"], "UC" + "n" * 22)
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "preloaded")

    def test_google_channel_limit_does_not_move_the_rejected_upload(self):
        first = self.s.snapshot()["queue"][0]
        self.connect_next()
        self.upload.side_effect = youtube_release.SlotUnavailable(
            "channel upload limit", reason="uploadLimitExceeded", retry_after_seconds=90)
        self.s.publish_one()
        self.s.publish_one()
        self.upload.assert_called_once()
        item = self.s.snapshot()["queue"][0]
        self.assertEqual((item["id"], item["account_id"], item["status"]),
                         (first["id"], first["account_id"], "scheduled"))

    def test_project_quota_blocks_new_jobs_and_preserves_existing_inflight_state(self):
        primary = self.s.snapshot()["queue"][0]["account_id"]
        backup = self.connect_next()
        first = self.s.snapshot()["queue"][0]["id"]
        self.s.upload_status(first, "sending")
        self.s.slot_wait(primary, 90, "project quota", project_wide=True)
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "sending")
        created = self.add_pending("После квоты", self.clock + timedelta(hours=7))
        with self.s.db() as db:
            db.execute("UPDATE queue SET account_id=? WHERE id=?", (backup, created))
        self.s.publish_one()
        self.upload.assert_not_called()
        self.clock += timedelta(seconds=91)
        self.s.publish_one()
        self.upload.assert_called_once()
        self.assertEqual(self.upload.call_args.kwargs["expected_channel"], "UC" + "n" * 22)

    def test_reschedule_changes_remote_publication_and_keeps_video_identity(self):
        identifier = self.s.snapshot()["queue"][0]["id"]
        previous = self.clock + timedelta(hours=2)
        changed = self.clock + timedelta(hours=5)
        self.s.upload_status(identifier, "preloaded", external=FIRST_VIDEO, at=studio.iso(previous))
        date = changed.astimezone(schedule.ZONES["RU"]).strftime("%Y-%m-%dT%H:%M")
        with patch("youtube_release.reschedule") as move:
            self.s.queue_action(dict(id=identifier, action="save", at=date))
            move.assert_called_once_with("server", "access", FIRST_VIDEO, changed,
                                         expected_channel="UC" + "r" * 22)
        job = self.s.snapshot()["queue"][0]
        self.assertEqual((job["status"], job["external_id"], job["at"]),
                         ("preloaded", FIRST_VIDEO, studio.iso(changed)))
        self.upload.assert_not_called()

    def test_uncertain_reschedule_is_verified_without_reupload(self):
        row = self.s.snapshot()["queue"][0]
        original = self.clock + timedelta(hours=2)
        proposed = self.clock + timedelta(hours=4)
        self.s.upload_status(row["id"], "preloaded", external=FIRST_VIDEO, at=studio.iso(original))
        date = proposed.astimezone(schedule.ZONES["RU"]).strftime("%Y-%m-%dT%H:%M")
        with patch("youtube_release.reschedule", side_effect=youtube_release.ScheduleUncertain("timeout")):
            with self.assertRaises(youtube_release.ScheduleUncertain):
                self.s.queue_action(dict(id=row["id"], action="save", at=date))
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "remote_check")
        self.status.return_value = {FIRST_VIDEO: {"privacyStatus": "private", "publishAt": studio.iso(original)}}
        self.s.sync_youtube()
        job = self.s.snapshot()["queue"][0]
        self.assertEqual((job["status"], job["at"]), ("preloaded", studio.iso(original)))
        self.upload.assert_not_called()

    def test_remote_removal_releases_preloaded_slot_and_never_reuploads_it(self):
        identifier = self.s.snapshot()["queue"][0]["id"]
        future = self.clock + timedelta(hours=2)
        self.s.upload_status(identifier, "preloaded", external=FIRST_VIDEO, at=studio.iso(future))
        self.s.sync_youtube()
        job = self.s.snapshot()["queue"][0]
        self.assertEqual((job["status"], job["external_id"]), ("blocked", FIRST_VIDEO))
        self.s.publish_one()
        self.upload.assert_not_called()

    def test_changed_route_requires_review_of_pending_job(self):
        account = next(row for row in self.s.snapshot()["accounts"] if row["lang"] == "RU")
        self.s.add_account(dict(id=account["id"], lang="RU", platform="YouTube", name="RU",
                                route="ES", target="UC" + "r" * 22))
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "review")

    def test_strict_gateway_holds_direct_without_using_its_healthy_route(self):
        os.environ["STREAMCUT_YOUTUBE_REQUIRE_GATEWAY"] = "1"
        self.s.publish_one()
        self.s.record_connection(dict(route="server", platform="YouTube", state="ok",
                                     country="RU", detail="", checked_at=studio.iso(self.clock)))
        self.s.publish_one()
        self.prepare.assert_not_called()
        self.upload.assert_not_called()
        snapshot = self.s.snapshot()
        self.assertTrue(snapshot["youtube_gateway_required"])
        self.assertEqual(snapshot["queue"][0]["status"], "route_wait")
        self.assertEqual(snapshot["queue"][0]["detail"], transport.GATEWAY_REQUIRED_DETAIL)

    def test_gateway_waits_without_config_and_recovers_after_safe_pre_media_failure(self):
        os.environ["STREAMCUT_YOUTUBE_REQUIRE_GATEWAY"] = "1"
        with self.s.db() as db:
            db.execute("UPDATE accounts SET route='gateway' WHERE lang='RU'")
        self.s.publish_one()
        self.prepare.assert_not_called()
        self.upload.assert_not_called()
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "route_wait")
        os.environ["STREAMCUT_UPLOAD_GATEWAY"] = "http://trusted.example:8080"
        self.upload.side_effect = transport.RouteProblem("gateway failed before media")
        healthy = dict(route="gateway", platform="YouTube", state="ok",
                       country=None, detail="", checked_at=studio.iso(self.clock))
        self.s.record_connection(healthy)
        self.s.publish_one()
        self.assertEqual(self.s.snapshot()["queue"][0]["status"], "route_wait")
        self.upload.assert_called_once()
        self.upload.side_effect = None
        self.s.record_connection(healthy)
        self.s.publish_one()
        self.assertEqual(self.upload.call_count, 2)
        self.assertTrue(all(call.args[0] == "gateway" for call in self.upload.call_args_list))

    def test_route_failure_blocks_and_recovery_moves_overdue_job_forward(self):
        self.prepare.side_effect = transport.RouteProblem("route went down")
        self.s.publish_one()
        self.upload.assert_not_called()
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
    def test_ru_slots_cover_five_moscow_hours_then_roll_to_tomorrow(self):
        start = datetime(2026, 11, 21, 12, tzinfo=timezone.utc)
        slots = [start + timedelta(minutes=30 * i) for i in range(10)]
        before = start - timedelta(minutes=1)
        for index, slot in enumerate(slots):
            self.assertTrue(schedule.in_release_window(slot, "RU"))
            self.assertEqual(schedule.available_slot(before, "RU", slots[:index]), slot)
        self.assertFalse(schedule.in_release_window(start + timedelta(hours=5), "RU"))
        self.assertFalse(schedule.in_release_window(start + timedelta(minutes=15), "RU"))
        self.assertEqual(schedule.available_slot(before, "RU", slots), start + timedelta(days=1))

    def test_en_and_esp_keep_hourly_local_slots(self):
        for lang in ("EN", "ESP"):
            start = schedule.local_time("2026-11-21T09:00", lang)
            half_hour = start + timedelta(minutes=30)
            self.assertFalse(schedule.in_release_window(half_hour, lang))
            self.assertEqual(schedule.available_slot(start, lang, [start]), start + timedelta(hours=1))

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


if __name__ == "__main__":
    unittest.main()
