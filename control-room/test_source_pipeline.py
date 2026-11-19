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
        }]) as select, patch("transport.upload_private") as upload:
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
