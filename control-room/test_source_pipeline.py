"""Exercise the contract between sources, the editor and the dashboard."""

from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import studio
from captions import read_srt


URL = "https://www.youtube.com/watch?v=abcdefghijk"
TRANSCRIPT = "1\n00:00:01,000 --> 00:00:03,500\nThe whole city sees it.\n"
PROFILE = {
    "facecam": "75%,0%,25%,50%",
    "gameplay": "0%,0%,75%,100%",
    "headline": "The whole city reacts",
    "transcript": TRANSCRIPT,
}


class SourcePipelineTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def service(self):
        service = studio.Studio(self.root)
        self.addCleanup(service.close)
        return service

    def test_normalizes_platform_urls_on_the_server(self):
        for link in (URL, "https://youtu.be/abcdefghijk", "https://youtube.com/live/abcdefghijk",
                     "https://m.youtube.com/shorts/abcdefghijk"):
            self.assertEqual(studio.source_url(link), (URL, "youtube:abcdefghijk"))
        self.assertEqual(studio.source_url("https://twitch.tv/IlloJuan")[1], "twitch:live:illojuan")
        self.assertEqual(studio.source_url("https://twitch.tv/videos/12345")[1], "twitch:vod:12345")
        for clip in ("https://twitch.tv/creator/clip/Clip-A", "https://clips.twitch.tv/Clip-A"):
            self.assertEqual(studio.source_url(clip), ("https://clips.twitch.tv/Clip-A", "twitch:clip:Clip-A"))
        for link in ("http://youtu.be/abcdefghijk", "https://youtube.com.example.com/watch?v=abcdefghijk",
                     "https://user:password@youtube.com/watch?v=abcdefghijk", "https://twitch.tv/directory"):
            with self.assertRaises(ValueError):
                studio.source_url(link)

    def test_draft_preserves_transcript_and_requeues_after_layout(self):
        service = self.service()
        source_id = service.add_source({"lang": "EN", "url": URL, "prepare_upload": True}, return_id=True)
        service.receive_video(BytesIO(b"fixture"), 7, {"id": source_id, "name": "source.mp4", "lang": "EN"})
        service.edit_one()
        self.assertEqual(service.get_source(source_id)["status"], "waiting_layout")
        self.assertEqual(service.add_source({"id": source_id, "edit": PROFILE}, return_id=True), source_id)
        self.assertEqual(service.get_source(source_id)["status"], "queued")
        self.assertEqual(service.source_edit(source_id)["edit"]["transcript"], TRANSCRIPT)
        self.assertNotIn("edit_profile", service.snapshot()["sources"][0])
        with self.assertRaisesRegex(ValueError, "недоступно"):
            service.add_source({"id": source_id, "edit": PROFILE, "prepare_upload": True})

    def test_discovered_twitch_sources_show_safe_acquisition_status(self):
        service = self.service()
        checked = studio.iso(studio.now())
        items = [
            dict(kind="live", url="https://www.twitch.tv/creator", twitch_user="11", key="twitch:stream:13"),
            dict(kind="vod", url="https://www.twitch.tv/videos/124", twitch_user="12", key="twitch:stream:14"),
            dict(kind="vod", url="https://www.twitch.tv/videos/125", twitch_user="11", key="twitch:stream:15"),
        ]
        for rank, entry in enumerate(items, 1):
            entry.update(lang="RU", provider="Twitch", title="GTA VI", creator=f"Автор {rank}",
                         author=entry["twitch_user"], directory_rank=rank, observed=checked, popularity=8)
            service.add_source(entry, key_override=entry["key"], discovered_at=checked)
        service.add_source({"lang": "RU", "url": "https://www.twitch.tv/videos/126"})
        with service.db() as db:
            db.execute("INSERT INTO discovery_feeds VALUES (?,?,?,?)", ("RU", "Twitch", checked, json.dumps(items)))

        def source_states():
            state = service.snapshot()
            self.assertNotIn("private-fixture", json.dumps(state))
            return {source["url"]: source.get("media_state") for source in state["sources"]}

        env = {"TWITCH_CLIENT_ID": "fixture", "STREAMCUT_TWITCH_CLIP_TOKEN_11": "private-fixture"}
        with patch.dict(os.environ, env, clear=True), patch("studio.automatic_editing", return_value=True):
            self.assertEqual(source_states(), {
                items[0]["url"]: "twitch_wait_archive",
                items[1]["url"]: "twitch_wait_access",
                items[2]["url"]: "twitch_wait_media",
                "https://www.twitch.tv/videos/126": None,
            })
            with patch("twitch_media.fetch", side_effect=studio.transport.ConnectionProblem("private-fixture")):
                service.fetch_twitch_media()
            self.assertEqual(source_states()[items[2]["url"]], "twitch_media_error")
            service.media_checks.clear()
            with patch("twitch_media.fetch", return_value=None):
                service.fetch_twitch_media()
            self.assertEqual(source_states()[items[2]["url"]], "twitch_media_missing")
            service.change_setting("scan", False)
            self.assertEqual(source_states()[items[2]["url"]], "twitch_paused")

    def test_archive_index_links_found_vod_to_same_source_and_queues_montage(self):
        service = self.service()
        url = "https://www.twitch.tv/videos/125"
        submitted = dict(lang="RU", url=url, provider="Twitch", twitch_user="99")
        source = service.add_source(submitted, return_id=True)
        self.assertIsNone(service.get_source(source)["twitch_broadcaster"])
        checked = studio.iso(studio.now())
        discovered = submitted | dict(kind="vod", twitch_user="11", title="GTA VI", creator="Автор",
                                      author="11", directory_rank=1, observed=checked, popularity=8,
                                      key="twitch:stream:15", archive_id="125")
        service.add_source(discovered, key_override=discovered["key"], discovered_at=checked)
        self.assertEqual(service.get_source(source)["twitch_broadcaster"], "11")
        with service.db() as db:
            db.execute("INSERT INTO discovery_feeds VALUES (?,?,?,?)", ("RU", "Twitch", checked, json.dumps([discovered])))
        index = self.root / "archives.json"
        index.write_text(json.dumps({"125": {"broadcaster_id": "11", "url": "https://example.com/approved.mp4"}}))

        def deliver(_url, destination, *, archive):
            self.assertTrue(archive)
            destination.write_bytes(b"test-media")
            earlier = time.time() - 6
            os.utime(destination, times=(earlier, earlier))
            return destination

        with patch.dict(os.environ, {"STREAMCUT_TWITCH_ARCHIVE_INDEX": str(index)}, clear=True), patch(
                "studio.automatic_editing", return_value=True), patch("twitch_media.download", side_effect=deliver):
            service.fetch_twitch_media()
            service.scan()
        state = service.get_source(source)
        self.assertEqual((state["status"], state["rel_file"]), ("queued", "twitch-vod-125.mp4"))
        with service.db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)

    def test_strict_discovery_does_not_save_sources_without_media_access(self):
        service = self.service()
        observed = studio.iso(studio.now())
        candidate = dict(lang="RU", kind="vod", url="https://www.twitch.tv/videos/125", twitch_user="11",
                         title="GTA VI", creator="Автор", author="11", directory_followup=True,
                         key="twitch:stream:15", archive_id="125", observed=observed, popularity=8, provider="Twitch")
        settings = {"STREAMCUT_TWITCH_STRICT_RU": "1", "STREAMCUT_YOUTUBE_AUTO_TWITCH_BROADCASTERS": "11",
                    "TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"}
        with patch.dict(os.environ, settings, clear=True), patch("twitch_directory.search", return_value={
            "RU": [candidate], "EN": [], "ESP": [],
        }):
            service.search(force=True)
            with service.db() as db:
                self.assertIsNone(db.execute("SELECT id FROM sources").fetchone())
            local = service.input / "RU" / "twitch-vod-125.mp4"
            local.write_bytes(b"approved fixture")
            earlier = time.time() - 6
            os.utime(local, times=(earlier, earlier))
            service.search(force=True)
            service.scan()
            state = service.snapshot()
            self.assertTrue(state["strict_ru_sources"])
            self.assertEqual([row["url"] for row in state["sources"]], [candidate["url"]])

    def test_full_inventory_does_not_stop_discovery_or_a_late_authorized_drop(self):
        service = self.service()
        instant = studio.now().replace(microsecond=0)
        candidate = dict(lang="RU", kind="vod", url="https://www.twitch.tv/videos/125", twitch_user="12",
                         title="GTA VI", creator="Автор", author="12", directory_followup=True,
                         key="twitch:stream:15", archive_id="125", observed=studio.iso(instant),
                         popularity=8, provider="Twitch")
        settings = {"STREAMCUT_TWITCH_STRICT_RU": "1", "STREAMCUT_YOUTUBE_AUTO_TWITCH_BROADCASTERS": "11",
                    "TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"}
        with service.db() as db:
            db.executemany("""INSERT INTO clips(lang,title,relative_path,verified) VALUES
                           ('RU','Готовый',?,1)""", [(f"{index}.mp4",) for index in range(30)])
        with patch.dict(os.environ, settings, clear=True), patch("twitch_directory.search", return_value={
            "RU": [candidate], "EN": [], "ESP": [],
        }) as directory:
            for clock in (instant, instant + studio.timedelta(minutes=30)):
                with patch("studio.now", return_value=clock):
                    service.search()
            self.assertEqual(directory.call_count, 2)
            self.assertTrue(all(not call.kwargs["rapid"] for call in directory.call_args_list))
            state = service.snapshot()
            self.assertEqual(state["source_health"], {"archives": 1, "clips": 0, "accessible": 0, "active": 0})
            self.assertEqual(state["inventory_limit"], 30)
            self.assertEqual(state["queue_counts"]["RU"], 30)
            media = service.input / "RU" / "twitch-vod-125.mp4"
            media.write_bytes(b"owner-provided fixture")
            earlier = time.time() - 6
            os.utime(media, times=(earlier, earlier))
            service.scan()
            with patch("autocut.prepare") as cutter:
                service.edit_one()
            cutter.assert_not_called()
            state = service.snapshot()
            self.assertEqual(state["source_health"], {"archives": 0, "clips": 0, "accessible": 0, "active": 1})
            self.assertEqual([item["status"] for item in state["sources"]], ["queued"])

    def test_empty_production_scans_every_five_minutes_and_only_registers_downloads(self):
        service = self.service()
        instant = studio.now().replace(microsecond=0)
        candidate = dict(lang="RU", kind="vod", url="https://www.twitch.tv/videos/125", twitch_user="11",
                         title="GTA VI", creator="Автор", author="11", directory_followup=True,
                         directory_priority=1, key="twitch:stream:15", archive_id="125",
                         observed=studio.iso(instant), popularity=8, provider="Twitch")
        settings = {"STREAMCUT_TWITCH_STRICT_RU": "1", "STREAMCUT_YOUTUBE_AUTO_TWITCH_BROADCASTERS": "11",
                    "TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"}
        with patch.dict(os.environ, settings, clear=True), patch("twitch_directory.search", return_value={
            "RU": [candidate], "EN": [], "ESP": [],
        }) as directory:
            for elapsed, expected in ((0, 1), (4, 1), (5, 2), (9, 2), (10, 3)):
                with patch("studio.now", return_value=instant + studio.timedelta(minutes=elapsed)):
                    service.search()
                self.assertEqual(directory.call_count, expected)
            self.assertTrue(all(call.kwargs["rapid"] for call in directory.call_args_list))
            with patch("studio.automatic_editing", return_value=True), patch(
                    "twitch_media.configured", return_value=True), patch("twitch_media.fetch", return_value=None) as fetch:
                service.fetch_twitch_media()
                fetch.assert_called_once()
                with service.db() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone()[0], 0)
                service.media_checks.clear()

                def download(_candidate, destination, *, current):
                    destination.write_bytes(b"official file")
                    earlier = time.time() - 6
                    os.utime(destination, times=(earlier, earlier))
                    return destination

                fetch.side_effect = download
                service.fetch_twitch_media()
            self.assertEqual([row["status"] for row in service.snapshot()["sources"]], ["queued"])

    def test_ru_acquisition_prefetches_three_distinct_files_and_refills_after_claim(self):
        service = self.service()
        observed = studio.iso(studio.now())
        candidates = [
            dict(lang="RU", kind="video", url=f"https://clips.twitch.tv/Clip{i}", twitch_user="11",
                 title=f"GTA VI Clip {i}", creator="Автор", author="11", directory_followup=True,
                 key=f"twitch:clip:Clip{i}", clip_id=f"Clip{i}", observed=observed, popularity=10 - i,
                 provider="Twitch") for i in range(4)
        ]
        settings = {"STREAMCUT_TWITCH_STRICT_RU": "1", "TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"}

        def download(candidate, destination, *, current):
            destination.write_text(candidate["clip_id"])
            return destination

        with patch.dict(os.environ, settings, clear=True), patch("twitch_directory.search", return_value={
                "RU": candidates, "EN": [], "ESP": [],
        }) as directory, patch("twitch_media.configured", return_value=True), patch(
                "twitch_media.fetch", side_effect=download) as fetch:
            service.search(force=True)
            for _ in candidates:
                service.fetch_twitch_media()
            self.assertEqual(fetch.call_count, 3)
            with patch("studio.now", return_value=studio.datetime.fromisoformat(observed) + studio.timedelta(minutes=5)):
                service.search()
            directory.assert_called_once()
            with service.db() as db:
                db.execute("UPDATE sources SET status='editing' WHERE source_key='twitch:clip:Clip0'")
            service.fetch_twitch_media()
            self.assertEqual([call.args[0]["clip_id"] for call in fetch.call_args_list],
                             ["Clip0", "Clip1", "Clip2", "Clip3"])

    def test_strict_media_skips_watch_only_and_denied_items_then_queues_the_available_clip(self):
        service = self.service()
        observed = studio.iso(studio.now())
        candidates = [
            dict(lang="RU", kind="video", url=f"https://clips.twitch.tv/{clip}",
                 twitch_user=str(rank), title="GTA VI", creator=f"Канал {rank}", author=str(rank),
                 key=f"twitch:clip:{clip}", clip_id=clip, directory_followup=True, directory_priority=rank,
                 observed=observed, popularity=10, provider="Twitch")
            for rank, clip in enumerate(("WatchOnly", "Denied", "Downloadable"), 1)
        ]
        settings = {"STREAMCUT_TWITCH_STRICT_RU": "1", "TWITCH_CLIENT_ID": "id", "TWITCH_CLIENT_SECRET": "secret"}

        def download(candidate, destination, *, current):
            if candidate["clip_id"] == "Denied":
                raise studio.transport.ConnectionProblem("HTTP 403")
            destination.write_bytes(b"media returned from official download")
            return destination

        with patch.dict(os.environ, settings, clear=True), patch("twitch_directory.search", return_value={
                "RU": candidates, "EN": [], "ESP": [],
        }), patch("twitch_media.configured", side_effect=lambda item: item["clip_id"] != "WatchOnly"), patch(
                "twitch_media.fetch", side_effect=download) as fetch:
            service.search(force=True)
            self.assertEqual(service.snapshot()["sources"], [])
            service.fetch_twitch_media()
            self.assertEqual([call.args[0]["clip_id"] for call in fetch.call_args_list], ["Denied", "Downloadable"])
            state = service.snapshot()
            self.assertEqual([(item["url"], item["status"], item["rel_file"]) for item in state["sources"]],
                             [(candidates[2]["url"], "queued", "twitch-clip-Downloadable.mp4")])
            with service.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM sources").fetchone()[0], 1)

    def test_old_database_can_store_profile_without_discarding_sources(self):
        with sqlite3.connect(self.root / "studio.db") as db:
            db.executescript(studio.SCHEMA.replace(", edit_profile TEXT", ""))
            db.execute("""INSERT INTO sources(id,lang,title,url,source_key,status,created)
                          VALUES (42,'EN','Reaction',?,'youtube:abcdefghijk','waiting','2026-01-01')""", (URL,))
        db.close()
        service = self.service()
        service.add_source({"id": 42, "edit": PROFILE})
        self.assertEqual(service.get_source(42)["url"], URL)
        self.assertEqual(service.source_edit(42)["edit"]["facecam"], PROFILE["facecam"])

    def test_claim_closes_cursor_before_import_with_two_pending_sources(self):
        service = self.service()
        identifiers = []
        for language in ("RU", "EN"):
            media = service.input / language / "sample.mp4"
            media.write_bytes(language.encode())
            identifiers.append(service.add_source({"lang": language, "file": media.name}, return_id=True))

        def fake_editor(command, **_):
            folder = Path(command[command.index("--output") + 1])
            folder.mkdir()
            (folder / "clip.mp4").write_bytes(b"fixture")
            (folder / "manifest.json").write_text(json.dumps({
                "clips": [{"video": "clip.mp4", "title": "Test clip"}],
            }))
            return subprocess.CompletedProcess(command, 0)

        with patch("studio.subprocess.run", side_effect=fake_editor), patch.dict(os.environ, {
            "STREAMCUT_REACTION_ONLY": "0",
        }):
            service.edit_one()
            self.assertEqual([service.get_source(i)["status"] for i in identifiers], ["ready", "queued"])
            service.edit_one()
        self.assertEqual([service.get_source(i)["status"] for i in identifiers], ["ready", "ready"])
        self.assertEqual({clip["source_id"] for clip in service.snapshot()["clips"]}, set(identifiers))

    def test_ready_inventory_stops_at_ten_and_reopens_after_youtube_accepts(self):
        service = self.service()
        with service.db() as db:
            db.executemany("INSERT INTO clips(lang,title,relative_path) VALUES ('RU',?,?)",
                           [(str(i), f"clip-{i}.mp4") for i in range(10)])
        media = service.input / "RU" / "next.mp4"
        media.write_bytes(b"fixture")
        identifier = service.add_source({"lang": "RU", "file": media.name}, return_id=True)
        with patch("studio.subprocess.run") as editor:
            service.edit_one()
            editor.assert_not_called()
        self.assertEqual(service.get_source(identifier)["status"], "queued")
        self.assertEqual(service.snapshot()["queue_counts"]["RU"], 10)
        service.add_account(dict(lang="RU", platform="YouTube", name="Pilot", route="server", target="UC" + "0" * 22))
        with service.db() as db:
            db.execute("UPDATE queue SET status='preloaded' WHERE clip_id=1")
        self.assertEqual(service.snapshot()["queue_counts"]["RU"], 9)

        def fake_editor(command, **_):
            self.assertEqual(command[command.index("--count") + 1], "1")
            folder = Path(command[command.index("--output") + 1])
            folder.mkdir()
            (folder / "clip.mp4").write_bytes(b"fixture")
            (folder / "manifest.json").write_text(json.dumps({
                "clips": [{"video": "clip.mp4", "title": "Готовый клип"}],
            }))
            return subprocess.CompletedProcess(command, 0)

        with patch("autocut.enabled", return_value=False), patch("studio.subprocess.run", side_effect=fake_editor):
            service.edit_one()
        result = service.snapshot()
        self.assertEqual(result["queue_counts"]["RU"], 10)
        ready = next(row for row in result["clips"] if row["source_id"] == identifier)
        self.assertIn("#Shorts", ready["hashtags"])
        self.assertIn("Готовый клип", ready["description"])

    def test_strict_ru_counts_thirty_verified_clips_and_reopens_after_preload(self):
        with patch.dict(os.environ, {"STREAMCUT_DISCOVERY_MODE": "twitch", "STREAMCUT_TWITCH_STRICT_RU": "1"}):
            service = self.service()
            service.add_account(dict(lang="RU", platform="YouTube", name="RU", route="server",
                                     target="UC" + "r" * 22))
            with service.db() as db:
                db.executemany("INSERT INTO clips(lang,title,relative_path,verified) VALUES (?,?,?,?)", [
                    ("RU", str(i), f"clip-{i}.mp4", 1) for i in range(30)
                ] + [("RU", "Unverified", "unverified.mp4", 0), ("EN", "Historical", "historical.mp4", 1)])
                service.populate(db)
            path = service.input / "RU" / "next.mp4"
            path.write_bytes(b"fixture")
            identifier = service.add_source({"lang": "RU", "file": path.name}, return_id=True)
            with patch("studio.subprocess.run") as editor:
                service.edit_one()
                editor.assert_not_called()
            self.assertEqual(service.get_source(identifier)["status"], "queued")
            self.assertEqual(service.snapshot()["queue_counts"], {"RU": 30, "EN": 0, "ESP": 0})
            with service.db() as db:
                db.execute("UPDATE queue SET status='preloaded' WHERE clip_id=1")
                self.assertEqual(service.inventory_capacity(service.ready_counts(db), "RU"), 1)

    @patch.dict(os.environ, {"STREAMCUT_DISCOVERY_MODE": "twitch", "STREAMCUT_TWITCH_STRICT_RU": "1"})
    def test_strict_automatic_sources_discard_missing_camera_and_wrong_language(self):
        service = self.service()
        ids = []
        for archive in ("100", "101"):
            path = service.input / "RU" / f"twitch-vod-{archive}.mp4"
            path.write_bytes(archive.encode())
            ids.append(service.add_source({
                "lang": "RU", "url": f"https://www.twitch.tv/videos/{archive}", "provider": "Twitch",
                "kind": "vod", "twitch_user": "300", "file": path.name,
            }, discovered_at=studio.iso(studio.now()), return_id=True))
        profile = {
            **PROFILE, "mode": "manual", "moments": [{"start_seconds": 0, "end_seconds": 30}],
            "speech_languages": [{"code": "en", "confidence": .99}],
        }
        with patch("studio.automatic_editing", return_value=True), patch("studio.autocut.prepare", side_effect=[
            studio.reaction.MissingReactionProfile("Камера неоднозначна."), profile,
        ]), patch("studio.subprocess.run") as editor:
            service.edit_one()
            service.edit_one()
            editor.assert_not_called()
        self.assertEqual([service.get_source(i)["status"] for i in ids], ["dismissed", "dismissed"])
        self.assertFalse(service.snapshot()["clips"])

    @patch.dict(os.environ, {"STREAMCUT_DISCOVERY_MODE": "twitch", "STREAMCUT_TWITCH_STRICT_RU": "1"})
    def test_interrupted_automatic_export_reuses_plan_and_retries_timeout_once(self):
        service = self.service()
        path = service.input / "RU" / "vod-valid.mp4"
        path.write_bytes(b"long source")
        identifier = service.add_source({
            "lang": "RU", "url": "https://www.twitch.tv/videos/100", "provider": "Twitch",
            "kind": "vod", "twitch_user": "300", "file": path.name,
        }, discovered_at=studio.iso(studio.now()), return_id=True)
        profile = {
            **PROFILE, "mode": "manual", "moments": [{"start_seconds": 0, "end_seconds": 30}],
            "speech_languages": [{"code": "ru", "confidence": .99}],
        }
        with patch("studio.automatic_editing", return_value=True), \
             patch("studio.autocut.prepare", return_value=profile) as prepare, \
             patch("studio.subprocess.run", side_effect=subprocess.TimeoutExpired(["editor"], 1)) as editor:
            service.edit_one()
            pending = service.get_source(identifier)
            self.assertEqual(pending["status"], "queued")
            self.assertEqual(json.loads(pending["auto_plan"])["batch_end"], 1)
            with service.db() as db:
                db.execute("UPDATE sources SET status='editing' WHERE id=?", (identifier,))
            service.close()
            service = self.service()
            service.edit_one()
            prepare.assert_called_once()
            self.assertEqual(editor.call_count, 2)
        self.assertEqual(service.get_source(identifier)["status"], "failed")

    def test_archive_batches_resume_after_inventory_space_and_keep_per_moment_language(self):
        service = self.service()
        service.add_account(dict(lang="RU", platform="YouTube", name="RU", route="server",
                                 target="UC" + "r" * 22))
        path = service.input / "RU" / "full.mp4"
        path.write_bytes(b"long source")
        source_id = service.add_source({"lang": "RU", "file": path.name}, return_id=True)
        moments = [{"start_seconds": index * 30, "end_seconds": index * 30 + 25,
                    "headline": "Спокойная поездка по городу"} for index in range(12)]
        transcript = "\n\n".join(
            f"{index + 1}\n00:{index // 2:02d}:{index % 2 * 30:02d},000 --> "
            f"00:{index // 2:02d}:{index % 2 * 30 + 20:02d},000\nМомент начинается здесь."
            for index in range(len(moments))
        )
        fixture = {"headline": "Спокойная поездка по городу", "facecam": "0%,0%,20%,20%",
                   "transcript": transcript, "mode": "manual", "moments": moments,
                   "speech_languages": [{"code": "en", "confidence": .99}] +
                                       [{"code": "ru", "confidence": .99}] * 11}
        ranges_used = []

        def editor(command, **_):
            target = Path(command[command.index("--output") + 1])
            selected = json.loads(Path(command[command.index("--ranges") + 1]).read_text())["moments"]
            ranges_used.append([item["start_seconds"] for item in selected])
            clips = []
            target.mkdir()
            for index, item in enumerate(selected):
                if item["start_seconds"] == 0:  # One input interval produced no clip.
                    continue
                relative = f"{index}.mp4"
                (target / relative).write_bytes(b"rendered")
                clips.append(item | {"video": relative, "title": item["headline"]})
            (target / "manifest.json").write_text(json.dumps({"clips": clips}))
            return subprocess.CompletedProcess(command, 0)

        with patch("studio.automatic_editing", return_value=True), patch(
                "studio.autocut.prepare", return_value=fixture) as model, patch(
                "studio.subprocess.run", side_effect=editor):
            service.edit_one()
            self.assertEqual(service.get_source(source_id)["auto_offset"], 10)
            service.edit_one()
            service.edit_one()
            self.assertEqual(len(ranges_used), 2)
            with service.db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM clips WHERE verified=1").fetchone()[0], 10)
                db.execute("UPDATE queue SET status='preloaded' WHERE id=(SELECT min(id) FROM queue)")
            service.edit_one()
            model.assert_called_once()
        self.assertEqual(ranges_used, [list(range(0, 300, 30)), [300], [330]])
        source = service.get_source(source_id)
        self.assertEqual((source["status"], source["auto_offset"], source["auto_plan"]), ("ready", 12, None))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg and ffprobe")
    def test_discovered_source_with_local_recording_reaches_playable_mp4(self):
        service = self.service()
        source_id = service.add_source(
            {"lang": "EN", "title": "GTA 6 Reaction", "url": URL, "kind": "video"},
            discovered_at=studio.iso(studio.now()), return_id=True)
        service.add_source({"id": source_id, "edit": {**PROFILE, "mode": "model"}, "mark": 3})
        filename = service.input / "EN" / "youtube-abcdefghijk.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc2=s=320x180:r=30:d=5", "-f", "lavfi", "-i", "sine=f=440:d=5",
            "-c:v", "libx264", "-c:a", "aac", str(filename),
        ], check=True)
        earlier = time.time() - 10
        os.utime(filename, (earlier, earlier))
        service.scan()
        self.assertEqual(service.get_source(source_id)["rel_file"], filename.name)

        with patch("moment_model.select_moments", return_value=[{
            "start_seconds": 1.25, "end_seconds": 3.0, "headline": "Everyone saw that coming",
        }]) as select, patch("youtube_release.upload") as upload:
            service.edit_one()
            select.assert_called_once_with(TRANSCRIPT, "EN")
            upload.assert_not_called()

        data = service.snapshot()
        self.assertEqual(data["sources"][0]["status"], "ready", data["sources"][0]["detail"])
        self.assertEqual(data["queue"], [])
        clip = data["clips"][0]
        self.assertEqual((clip["source_id"], clip["title"]), (source_id, "Everyone saw that coming"))
        self.assertEqual(data["sources"][0]["kind"], "video")
        manifest_path, = service.output.glob("source-*/manifest.json")
        entry, = json.loads(manifest_path.read_text())["clips"]
        self.assertEqual((entry["start_seconds"], entry["end_seconds"], entry["layout"]), (1.25, 3.0, "split"))
        cues = read_srt(manifest_path.parent / entry["captions"])
        self.assertEqual(" ".join(cue.text for cue in cues), "The whole city sees it.")
        self.assertEqual((cues[0].start, cues[-1].end), (0, 1.75))
        rendered = manifest_path.parent / entry["video"]
        probe = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
            "-of", "json", str(rendered),
        ], text=True)
        video, audio = json.loads(probe)["streams"]
        self.assertEqual((video["width"], video["height"], audio["codec_type"]), (1080, 1920, "audio"))


if __name__ == "__main__":
    unittest.main()
