"""Single-owner Streamcut service. Run behind SSH forwarding or on the local Mac."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
from tempfile import NamedTemporaryFile
from threading import Event, Lock, Thread
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import connections
import autocut
import discovery
import metrics
import reaction
import moment_model
import regional
import schedule
import transport
import twitch_directory
import twitch_media
import youtube_release

HERE = Path(__file__).resolve().parent
LANGS = {"RU": "ru", "EN": "en", "ESP": "es"}
UTC = timezone.utc
MEDIA = {".mp4", ".mkv", ".mov", ".webm", ".ts"}
CHANNEL_RE = re.compile(r"UC[A-Za-z0-9_-]{22}")
INVENTORY_LIMIT = 10
RU_INVENTORY_LIMIT = 30
AUTO_RENDER_BATCH = 3
MIN_SPEECH_CONFIDENCE = 0.90
PUBLISH_LEAD = timedelta(minutes=15)
TWITCH_DISCOVERY_INTERVAL = timedelta(minutes=30)
RESERVING_QUEUE_STATES = ("scheduled", "route_wait", "checking", "sending", "unknown",
                          "remote_check", "preloaded", "uploaded")
TWITCH_FILE_PENDING = "Автоматическое получение пока не вернуло файл. Сервис проверит запись снова."
TWITCH_FILE_ERROR = "Проверьте доступ к записи и соединение. Сервис повторит попытку."


def now():
    return datetime.now(UTC)


def iso(date):
    return date.astimezone(UTC).isoformat(timespec="seconds")


def directory_mode():
    return os.environ.get("STREAMCUT_DISCOVERY_MODE", "twitch") == "twitch"


def search_configured():
    if directory_mode():
        return bool(os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"))
    return regional.configured()


def automatic_editing(lang):
    markets = os.environ.get("STREAMCUT_AUTOCUT_LANGS", "RU").split(",")
    unattended = directory_mode() and os.environ.get(twitch_directory.STRICT_RU) == "1"
    return autocut.enabled(default=unattended) and lang in {item.strip().upper() for item in markets}


def youtube_auto_broadcasters():
    raw = os.environ.get("STREAMCUT_YOUTUBE_AUTO_TWITCH_BROADCASTERS")
    if raw is None:
        return None
    ids = {part.strip() for part in raw.split(",") if part.strip()}
    return ids if all(discovery.TWITCH_ID.fullmatch(value) for value in ids) else set()


def clip_metadata(lang, title, url=""):
    labels = {
        "RU": ("Момент из эфира по GTA VI", "Исходная запись", "#ГТА6"),
        "EN": ("A moment from a GTA VI broadcast", "Source recording", "#GTA6Clips"),
        "ESP": ("Un momento del directo de GTA VI", "Grabación original", "#GTA6Español"),
    }
    intro, source, tag = labels[lang]
    description = f"{title.rstrip('. ')}. {intro}."
    if url:
        description += f"\n{source} — {url}"
    return description, ("#GTA6", "#Shorts", tag)


def text_field(data, name, limit):
    value = data.get(name, "")
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("Проверьте заполнение полей.")
    return value.strip()


def language(data):
    lang = text_field(data, "lang", 3)
    if lang not in LANGS:
        raise ValueError("Выберите язык RU, EN или ESP.")
    return lang


def source_url(raw):
    if not raw:
        return "", ""
    url = urlsplit(raw)
    host = (url.hostname or "").removeprefix("www.")
    if url.scheme != "https" or url.username or url.password or url.port:
        raise ValueError("Нужна обычная HTTPS-ссылка без данных входа.")
    if host in ("youtube.com", "m.youtube.com", "youtu.be"):
        video = parse_qs(url.query).get("v", [""])[0] if url.path == "/watch" else ""
        if host == "youtu.be":
            video = url.path.strip("/")
        elif m := re.fullmatch(r"/(?:shorts|live|embed)/([\w-]+)/?", url.path, re.ASCII):
            video = m[1]
        if re.fullmatch(r"[a-zA-Z0-9_-]{11}", video):
            return f"https://www.youtube.com/watch?v={video}", f"youtube:{video}"
    elif host == "twitch.tv":
        if m := re.fullmatch(r"/[a-zA-Z0-9_]{3,25}/clip/([a-zA-Z0-9_-]{1,160})/?", url.path):
            return f"https://clips.twitch.tv/{m[1]}", f"twitch:clip:{m[1]}"
        if m := re.fullmatch(r"/videos/([0-9]+)/?", url.path):
            return f"https://www.twitch.tv/videos/{m[1]}", f"twitch:vod:{m[1]}"
        if m := re.fullmatch(r"/([a-zA-Z0-9_]{3,25})/?", url.path):
            login = m[1].lower()
            if login not in {"directory", "settings", "downloads", "subscriptions", "inventory",
                             "wallet", "search", "videos"}:
                return f"https://www.twitch.tv/{login}", f"twitch:live:{login}"
    elif host == "clips.twitch.tv":
        if m := re.fullmatch(r"/([a-zA-Z0-9_-]{1,160})/?", url.path):
            return f"https://clips.twitch.tv/{m[1]}", f"twitch:clip:{m[1]}"
    raise ValueError("Укажите конкретное видео YouTube, запись, клип или канал Twitch.")


ACCOUNTS_SCHEMA = """(
    id INTEGER PRIMARY KEY, lang TEXT NOT NULL, platform TEXT NOT NULL, name TEXT NOT NULL,
    target TEXT NOT NULL, route TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 0,
    refresh_token TEXT, UNIQUE(platform, target)
)"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO settings VALUES ('scan', 1), ('paused', 0);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY, lang TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
    source_key TEXT UNIQUE, rel_file TEXT, fingerprint TEXT UNIQUE, mark INTEGER,
    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, kind TEXT, queued_at TEXT,
    last_seen TEXT, edit_profile TEXT, auto_attempt INTEGER NOT NULL DEFAULT 0, speech_language TEXT,
    twitch_broadcaster TEXT, auto_plan TEXT, auto_offset INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS discovery_feeds (
    lang TEXT NOT NULL, provider TEXT NOT NULL, checked_at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(lang, provider)
);
CREATE TABLE IF NOT EXISTS discovery_runs (
    name TEXT PRIMARY KEY, started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY, source_id INTEGER REFERENCES sources(id), lang TEXT NOT NULL,
    title TEXT NOT NULL, relative_path TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '', hashtags TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS accounts """ + ACCOUNTS_SCHEMA + """;
CREATE TABLE IF NOT EXISTS account_metrics (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id), payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS route_health (
    route TEXT NOT NULL, platform TEXT NOT NULL, state TEXT NOT NULL, country TEXT,
    detail TEXT NOT NULL, checked_at TEXT NOT NULL, PRIMARY KEY(route, platform)
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY, clip_id INTEGER NOT NULL REFERENCES clips(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id), at TEXT NOT NULL,
    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', external_id TEXT, sent_at TEXT, retry_at TEXT,
    auto_scheduled INTEGER,
    UNIQUE(clip_id, account_id)
);
"""


class Studio:
    def __init__(self, root, stop=None):
        self.root = root.resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise ValueError("Папка данных должна быть доступна только владельцу (chmod 700).")
        self.input = self.root / "inbox"
        self.output = self.root / "exports"
        self.output.mkdir(exist_ok=True)
        for lang in LANGS:
            (self.input / lang).mkdir(parents=True, exist_ok=True)
        self.lease = (self.root / "service.lock").open("a")
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lease.close()
            raise ValueError("Сервис с этой папкой данных уже запущен.") from None
        self.stop = stop or Event()
        self.pending_auth = {}
        self.scanned = {}
        self.inbox_lock = Lock()
        self.search_lock = Lock()
        self.edit_lock = Lock()
        self.media_checks = {}
        self.search_status = ""
        self.dbfile = self.root / "studio.db"
        with self.db() as db:
            db.executescript(SCHEMA)
            self.migrate_accounts(db)
            source_columns = {row["name"] for row in db.execute("PRAGMA table_info(sources)")}
            for column in ("kind", "queued_at", "last_seen", "edit_profile"):
                if column not in source_columns:
                    db.execute(f"ALTER TABLE sources ADD COLUMN {column} TEXT")
            if "queued_at" not in source_columns:
                db.execute("UPDATE sources SET queued_at=created WHERE rel_file IS NOT NULL")
            for table, columns in (
                ("sources", {"auto_attempt": "INTEGER NOT NULL DEFAULT 0", "speech_language": "TEXT",
                             "twitch_broadcaster": "TEXT", "auto_plan": "TEXT",
                             "auto_offset": "INTEGER NOT NULL DEFAULT 0"}),
                ("clips", {"verified": "INTEGER NOT NULL DEFAULT 0", "description": "TEXT NOT NULL DEFAULT ''",
                           "hashtags": "TEXT NOT NULL DEFAULT '[]'"}),
                ("queue", {"retry_at": "TEXT", "auto_scheduled": "INTEGER"}),
            ):
                existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            db.execute("UPDATE sources SET status='queued' WHERE status='editing'")
            db.execute("UPDATE queue SET status='scheduled', detail='' WHERE status='checking'")
            db.execute("""UPDATE queue SET status='unknown', detail='Проверьте YouTube Studio перед повтором.'
                          WHERE status='sending'""")

    @staticmethod
    def migrate_accounts(db):
        for entry in db.execute("PRAGMA index_list(accounts)").fetchall():
            columns = [r["name"] for r in db.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (entry["name"],))]
            if columns == ["lang", "platform"] and entry["unique"]:
                break
        else:
            return
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE accounts_new " + ACCOUNTS_SCHEMA)
            db.execute("INSERT INTO accounts_new SELECT id,lang,platform,name,target,route,auto,refresh_token FROM accounts")
            db.execute("DROP TABLE accounts")
            db.execute("ALTER TABLE accounts_new RENAME TO accounts")
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Ошибка переноса аккаунтов. Данные сохранены без изменений.")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")

    def close(self):
        self.lease.close()

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.dbfile, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db

    def media_file(self, lang, filename):
        if not filename or Path(filename).name != filename or Path(filename).suffix.lower() not in MEDIA:
            raise ValueError("Выберите видео из папки входящих файлов.")
        path = self.input / lang / filename
        if path.is_symlink() or not path.is_file() or path.resolve().parent != self.input / lang:
            raise ValueError("Файл не найден в папке выбранного языка.")
        return path

    def available_files(self, limit=None):
        result = {}
        for lang in LANGS:
            result[lang] = sorted(path.name for path in (self.input / lang).iterdir()
                                  if path.suffix.lower() in MEDIA and path.is_file() and not path.is_symlink())[:limit]
        return result

    def snapshot(self):
        with self.db() as db:
            settings = {r["name"]: bool(r["value"]) for r in db.execute(
                "SELECT * FROM settings WHERE name IN ('scan','paused')")}
            strict_sources = directory_mode() and os.environ.get(twitch_directory.STRICT_RU) == "1"
            sources = [dict(row) for row in db.execute("""SELECT id, lang, title, url, rel_file, mark,
                        status, detail, kind, last_seen, twitch_broadcaster FROM sources
                        WHERE status NOT IN ('dismissed','expired','archived') ORDER BY id DESC LIMIT 300""")]
            instant = now()
            roster, ranking_updated, ranked = self.current_discovery(db, instant)
            queue_counts = self.ready_counts(db)
            recordings = {(lang, item["url"]): item for lang, (_, entries) in ranked.items()
                          for item in entries if item["provider"] == "Twitch" and item["kind"] == "vod"}
            source_health = self.twitch_source_health(db, recordings.values()) if strict_sources else None
            visible_sources = []
            for source in sources:
                seen = source.pop("last_seen")
                broadcaster = source.pop("twitch_broadcaster")
                discovered = seen is not None or broadcaster is not None
                source["manual"] = seen is None
                is_twitch = source["url"].startswith(("https://www.twitch.tv/", "https://clips.twitch.tv/"))
                if strict_sources:
                    if source["lang"] != "RU":
                        continue
                    if is_twitch and discovered and source["status"] in (
                            "waiting", "waiting_layout", "failed", "empty", "ready"):
                        continue
                if discovered and source["status"] == "waiting" and source["url"].startswith("https://www.twitch.tv/"):
                    candidate = recordings.get((source["lang"], source["url"]))
                    source["media_state"], source["detail"] = self.twitch_source_status(
                        source, candidate, settings["scan"], queue_counts[source["lang"]])
                visible_sources.append(source)
            accounts = []
            for row in db.execute("""SELECT a.id, a.lang, a.platform, a.name, a.target,
                          a.route, a.auto, a.refresh_token IS NOT NULL AS connected, m.payload
                          FROM accounts a LEFT JOIN account_metrics m ON m.account_id=a.id ORDER BY a.id"""):
                item = dict(row)
                payload = item.pop("payload")
                item["stats"] = json.loads(payload) if payload else {}
                accounts.append(item)
            checks = [dict(row) for row in db.execute("SELECT * FROM route_health")]
            clips = [dict(row) for row in db.execute(
                "SELECT id, source_id, lang, title,description,hashtags FROM clips ORDER BY id DESC LIMIT 100")]
            for item in clips:
                description, tags = clip_metadata(item["lang"], item["title"])
                item["description"] = item["description"] or description
                item["hashtags"] = json.loads(item["hashtags"]) or list(tags)
            queue = [dict(row) for row in db.execute("""SELECT q.id, q.clip_id, q.at, q.status, q.detail,
                       q.external_id, q.account_id, c.title, c.lang, a.name AS account, a.platform
                       FROM queue q JOIN clips c ON c.id=q.clip_id JOIN accounts a ON a.id=q.account_id
                       ORDER BY CASE WHEN q.status IN ('uploaded','cancelled') THEN 1 ELSE 0 END, q.at LIMIT 500""")]
        return {"live": True, "settings": settings, "sources": visible_sources, "accounts": accounts,
                "connections": self.connection_snapshot(checks), "queue_counts": queue_counts,
                "inventory_limit": self.inventory_limit(),
                "roster": roster, "ranking_updated": ranking_updated,
                "source_health": source_health,
                "clips": clips, "queue": queue, "files": self.available_files(300),
                "routes": {r: transport.route_available(r) for r in transport.REGIONS},
                "oauth": transport.youtube_configured(), "uploads": self.uploads_enabled(),
                "youtube_gateway_required": transport.youtube_gateway_required(),
                "strict_ru_sources": strict_sources,
                "search_configured": search_configured(), "search_status": self.search_status}

    @staticmethod
    def inventory_limit():
        return RU_INVENTORY_LIMIT if directory_mode() and os.environ.get(twitch_directory.STRICT_RU) == "1" else INVENTORY_LIMIT

    @classmethod
    def inventory_capacity(cls, counts, lang):
        limit = cls.inventory_limit()
        if limit == RU_INVENTORY_LIMIT and lang != "RU":
            return 0
        return max(0, limit - counts[lang])

    @classmethod
    def unattended_source(cls, source):
        return (cls.inventory_limit() == RU_INVENTORY_LIMIT and source["lang"] == "RU"
                and source["last_seen"] is not None and not source["edit_profile"])

    @classmethod
    def ready_counts(cls, db):
        result = dict.fromkeys(LANGS, 0)
        verified = "AND c.lang='RU' AND c.verified=1" if cls.inventory_limit() == RU_INVENTORY_LIMIT else ""
        result.update(db.execute(f"""SELECT c.lang,count(*) FROM clips c WHERE NOT EXISTS (
            SELECT 1 FROM queue q JOIN accounts a ON a.id=q.account_id WHERE q.clip_id=c.id
            AND a.platform='YouTube' AND q.status IN ('preloaded','uploaded','cancelled')
        ) {verified} GROUP BY c.lang""").fetchall())
        return result

    @staticmethod
    def twitch_source_status(source, candidate, scanning, ready):
        if source["kind"] == "live":
            return "twitch_wait_archive", "Запись эфира ещё не найдена. Сервис проверит её при обновлении каталога."
        if candidate is None:
            return "twitch_wait_check", "Автополучение проверяется после обновления поиска."
        if not twitch_media.configured(candidate):
            return "twitch_wait_access", "Для автополучения нужна ссылка на файл от автора или доступ автора или редактора Twitch."
        if not scanning or not automatic_editing(source["lang"]):
            return "twitch_paused", "Включите автопоиск и автомонтаж для этого языка."
        if ready >= Studio.inventory_limit():
            return "twitch_paused", "Сервис получит запись, когда освободится место в очереди."
        if source["detail"] == TWITCH_FILE_PENDING:
            return "twitch_media_missing", TWITCH_FILE_PENDING
        if source["detail"]:
            return "twitch_media_error", TWITCH_FILE_ERROR
        return "twitch_wait_media", ""

    @staticmethod
    def uploads_enabled():
        return os.environ.get("STREAMCUT_YOUTUBE_UPLOAD") == "1"

    def get_source(self, identifier):
        if type(identifier) is not int or not 1 <= identifier <= 2**63 - 1:
            raise ValueError("Неверный источник.")
        with self.db() as db:
            source = db.execute("SELECT * FROM sources WHERE id=?", (identifier,)).fetchone()
        if source is None:
            raise ValueError("Источник не найден.")
        return source

    def source_edit(self, identifier):
        profile = self.get_source(identifier)["edit_profile"]
        return {"edit": json.loads(profile) if profile else None}

    def add_source(self, data, key_override=None, file_digest=None, discovered_at=None, *, return_id=False):
        identifier = data.get("id")
        if identifier is not None:
            current = self.get_source(identifier)
            data = {"lang": current["lang"], "url": current["url"], "title": current["title"],
                    "mark": current["mark"], **data}
        lang = language(data)
        url, key = source_url(text_field(data, "url", 2048))
        kind = data.get("kind") if data.get("kind") in ("live", "vod", "video") else None
        if kind is None and key.startswith(("twitch:vod:", "twitch:live:")):
            kind = key.split(":", 2)[1]
        if key_override:
            key = key_override
        twitch_broadcaster = data.get("twitch_user") if discovered_at and data.get("provider") == "Twitch" else None
        if not isinstance(twitch_broadcaster, str) or not discovery.TWITCH_ID.fullmatch(twitch_broadcaster):
            twitch_broadcaster = None
        file = text_field(data, "file", 255)
        title = text_field(data, "title", 140)
        mark = data.get("mark")
        if mark is not None and (type(mark) is not int or not 0 <= mark <= 86400):
            raise ValueError("Таймкод задаётся в секундах, от 0 до 86400.")
        prepare = data.get("prepare_upload", False)
        if type(prepare) is not bool or (prepare and file):
            raise ValueError("Некорректная подготовка загрузки.")
        profile = reaction.normalize_profile(data["edit"]) if data.get("edit") is not None else None
        encoded_profile = json.dumps(profile, ensure_ascii=False, sort_keys=True) if profile is not None else None
        if profile and profile["mode"] != "auto":
            mark = None
        if not (url or file or prepare or identifier is not None):
            raise ValueError("Укажите ссылку или выберите входящий файл.")
        fingerprint = None
        if file:
            path = self.media_file(lang, file)
            if file_digest is not None:
                fingerprint = file_digest
            else:
                with path.open("rb") as stream:
                    fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        status = "queued" if file else "waiting"
        entered = now().isoformat(timespec="microseconds")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if identifier is not None:
                existing = db.execute("SELECT * FROM sources WHERE id=?", (identifier,)).fetchone()
                if existing is None or existing["url"] != url or existing["lang"] != lang:
                    raise ValueError("Источник уже изменился. Откройте его снова.")
            else:
                existing = db.execute("SELECT * FROM sources WHERE source_key=?", (key,)).fetchone()
                if not existing and discovered_at and kind == "vod":
                    existing = db.execute("SELECT * FROM sources WHERE url=? AND lang=? ORDER BY id LIMIT 1",
                                          (url, lang)).fetchone()
            if existing and discovered_at and not file:
                if existing["lang"] != lang or existing["status"] == "dismissed":
                    return False
                transition = existing["kind"] in (None, "live") and kind in ("vod", "video")
                refreshed = existing["status"] == "expired"
                db.execute("""UPDATE sources SET
                              last_seen=CASE WHEN last_seen IS NOT NULL THEN ? ELSE NULL END,
                              status=CASE WHEN status='expired' THEN 'waiting' ELSE status END, url=CASE WHEN ? THEN ? ELSE url END,
                              title=CASE WHEN ? THEN ? ELSE title END, kind=CASE WHEN ? THEN ? ELSE kind END,
                              twitch_broadcaster=COALESCE(twitch_broadcaster, ?)
                              WHERE id=?""", (discovered_at, transition, url, transition,
                                              title or existing["title"], transition, kind,
                                              twitch_broadcaster, existing["id"]))
                return refreshed
            if existing and existing["status"] == "waiting" and existing["kind"] == "live" and kind == "vod" and not file:
                db.execute("UPDATE sources SET url=?,title=?,kind=? WHERE id=?",
                           (url, title or existing["title"], kind, existing["id"]))
                return False
            if existing and not file and (prepare or profile is not None):
                if (existing["lang"] != lang or existing["status"] not in
                        ("waiting", "waiting_layout", "failed", "empty") or (prepare and existing["rel_file"])):
                    raise ValueError("Редактирование этого источника сейчас недоступно.")
                status = "queued" if existing["rel_file"] else "waiting"
                db.execute("""UPDATE sources SET title=?,mark=?,status=?,detail='',edit_profile=COALESCE(?,edit_profile),
                              queued_at=CASE WHEN rel_file IS NOT NULL THEN ? ELSE NULL END WHERE id=?""",
                           (title or existing["title"], mark, status, encoded_profile, entered, existing["id"]))
                return existing["id"] if return_id else True
            attachable = ("waiting", "expired") if discovered_at else ("waiting",)
            if existing and (existing["status"] not in attachable or not file or existing["lang"] != lang):
                raise ValueError("Этот источник уже есть или указан другой язык.")
            if fingerprint and db.execute("SELECT 1 FROM sources WHERE fingerprint=?", (fingerprint,)).fetchone():
                raise ValueError("Этот файл уже был добавлен.")
            try:
                if existing:
                    db.execute("""UPDATE sources SET rel_file=?,fingerprint=?,status=?,mark=?,title=?,queued_at=?,detail='',
                                  edit_profile=COALESCE(?,edit_profile),twitch_broadcaster=COALESCE(?,twitch_broadcaster),
                                  last_seen=COALESCE(?,last_seen),url=?,kind=COALESCE(?,kind) WHERE id=?""",
                               (file, fingerprint, status, mark, title or Path(file).stem, entered, encoded_profile,
                                twitch_broadcaster, discovered_at, url, kind, existing["id"]))
                    source_id = existing["id"]
                else:
                    row = db.execute("""INSERT INTO sources(lang,title,url,source_key,rel_file,fingerprint,mark,
                        status,created,kind,queued_at,last_seen,edit_profile,twitch_broadcaster)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (lang, title or (Path(file).stem if file else urlsplit(url).netloc or "Новый источник"),
                         url, key or None, file or None, fingerprint, mark, status, entered, kind,
                         entered if file else None, discovered_at, encoded_profile, twitch_broadcaster))
                    source_id = row.lastrowid
            except sqlite3.IntegrityError:
                raise ValueError("Этот файл или источник уже был добавлен.") from None
        return source_id if return_id else True

    def receive_video(self, stream, size, metadata):
        lang = language(metadata)
        name = text_field(metadata, "name", 240)
        if not name or Path(name).name != name or Path(name).suffix.lower() not in MEDIA:
            raise ValueError("Допустимые форматы: MP4, MOV, MKV, WebM и TS.")
        if not 0 < size <= 2 * 1024 ** 3:
            raise ValueError("Допустимый размер видео — до 2 ГиБ.")
        folder = self.input / lang
        with NamedTemporaryFile(dir=folder, prefix=".upload-", suffix=".part", delete=False) as writer:
            temporary = Path(writer.name)
            digest = hashlib.sha256()
            try:
                remaining = size
                while remaining:
                    chunk = stream.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError("Загрузка прервалась. Повторите её.")
                    writer.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
                # Preserve the original name when possible. Never overwrite an existing video.
                with self.inbox_lock:
                    destination = folder / name
                    if destination.exists() or destination.is_symlink():
                        destination = folder / (secrets.token_hex(5) + Path(name).suffix.lower())
                    os.utime(temporary, None)
                    os.link(temporary, destination)
                    try:
                        source_id = self.add_source({**metadata, "file": destination.name},
                                                    file_digest=digest.hexdigest(), return_id=True)
                    except Exception:
                        destination.unlink()
                        raise
            finally:
                temporary.unlink(missing_ok=True)
        return {"id": source_id, "name": destination.name}

    def add_account(self, data):
        lang = language(data)
        platform = text_field(data, "platform", 15)
        name, target = text_field(data, "name", 100), text_field(data, "target", 100)
        route = text_field(data, "route", 10)
        identifier = data.get("id")
        if identifier is not None and type(identifier) is not int:
            raise ValueError("Неверный ID аккаунта.")
        if platform not in transport.API_HOSTS or route not in transport.REGIONS or not name:
            raise ValueError("Заполните название, площадку и маршрут.")
        if platform == "YouTube" and not CHANNEL_RE.fullmatch(target):
            raise ValueError("Для YouTube нужен ID канала, начинающийся с UC.")
        if platform != "YouTube":
            if not re.fullmatch(r"@?[a-zA-Z0-9_.]{1,80}", target):
                raise ValueError("Укажите имя аккаунта.")
            target = "@" + target.lstrip("@").lower()
        with self.db() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                if identifier is None:
                    duplicate = db.execute("""SELECT 1 FROM accounts WHERE platform=? AND
                        (target=? OR (? <> 'YouTube' AND lower(ltrim(target, '@'))=?))""",
                                           (platform, target, platform, target.lstrip("@"))).fetchone()
                    if duplicate:
                        raise ValueError("Этот канал уже добавлен. Откройте его карточку.")
                    db.execute("INSERT INTO accounts(lang,platform,name,target,route) VALUES (?,?,?,?,?)",
                               (lang, platform, name, target, route))
                else:
                    old = db.execute("SELECT * FROM accounts WHERE id=?", (identifier,)).fetchone()
                    if not old or old["lang"] != lang or old["platform"] != platform:
                        raise ValueError("Аккаунт не найден.")
                    same_target = (old["target"] == target or platform != "YouTube" and
                                   old["target"].lstrip("@").casefold() == target.lstrip("@"))
                    if not same_target:
                        raise ValueError("Чтобы подключить другой канал, добавьте новую карточку.")
                    if old["route"] != route:
                        busy = db.execute("""SELECT 1 FROM queue WHERE account_id=? AND
                                          status IN ('checking','sending','unknown')""", (identifier,)).fetchone()
                        if busy:
                            raise ValueError("Сначала разберите активные задания аккаунта.")
                        db.execute("UPDATE accounts SET name=?,route=?,auto=0 WHERE id=?", (name, route, identifier))
                        db.execute("""UPDATE queue SET status='review',detail='' WHERE account_id=?
                                   AND status IN ('scheduled','route_wait')""", (identifier,))
                        db.execute("DELETE FROM account_metrics WHERE account_id=?", (identifier,))
                    else:
                        db.execute("UPDATE accounts SET name=? WHERE id=?", (name, identifier))
                self.populate(db)
            except sqlite3.IntegrityError:
                raise ValueError("Этот канал уже добавлен.") from None

    def get_account(self, identifier):
        with self.db() as db:
            account = db.execute("SELECT * FROM accounts WHERE id=?", (identifier,)).fetchone()
        if not account:
            raise ValueError("Аккаунт не найден.")
        return dict(account)

    def change_setting(self, name, value):
        if name not in ("scan", "paused") or type(value) is not bool:
            raise ValueError("Некорректная настройка.")
        with self.db() as db:
            db.execute("UPDATE settings SET value=? WHERE name=?", (int(value), name))

    def auto_account(self, identifier, enabled):
        account = self.get_account(identifier)
        if type(enabled) is not bool:
            raise ValueError("Некорректная настройка.")
        if enabled:
            if (account["platform"] != "YouTube" or account["lang"] != "RU" or
                    not account["refresh_token"] or not self.uploads_enabled()):
                raise ValueError("Подключите RU-канал YouTube и включите планирование на сервере.")
            transport.require_youtube_write_route(account["route"])
            transport.prepare_upload(account)
        with self.db() as db:
            current = db.execute("""UPDATE accounts SET auto=? WHERE id=? AND target=? AND route=?
                AND refresh_token IS ?""",
                                 (int(enabled), identifier, account["target"], account["route"], account["refresh_token"]))
            if not current.rowcount:
                raise ValueError("Настройки аккаунта изменились. Повторите действие.")
            if enabled:
                self.promote_automatic_youtube(db, youtube_auto_broadcasters(), identifier)
            else:
                db.execute("UPDATE queue SET status='review',detail='' WHERE account_id=? AND status IN ('scheduled','route_wait')", (identifier,))

    @staticmethod
    def inbox_key(filename):
        stem = Path(filename).stem
        if matched := re.fullmatch(r"youtube-([A-Za-z0-9_-]{11})", stem):
            return f"youtube:{matched[1]}", f"https://www.youtube.com/watch?v={matched[1]}"
        if matched := re.fullmatch(r"twitch-vod-([0-9]{1,30})", stem):
            return f"twitch:vod:{matched[1]}", f"https://www.twitch.tv/videos/{matched[1]}"
        if matched := re.fullmatch(r"twitch-clip-([a-zA-Z0-9_-]{1,160})", stem):
            return f"twitch:clip:{matched[1]}", f"https://clips.twitch.tv/{matched[1]}"
        if matched := re.fullmatch(r"twitch-stream-([0-9]{1,30})", stem):
            return f"twitch:stream:{matched[1]}", ""
        return None

    def scan(self):
        with self.db() as db:
            if not db.execute("SELECT value FROM settings WHERE name='scan'").fetchone()[0]:
                return
            known = {(r["lang"], r["rel_file"]) for r in db.execute(
                "SELECT lang,rel_file FROM sources WHERE rel_file IS NOT NULL")}
            _, _, ranked = self.current_discovery(db, now())
            twitch_records = {(lang, self.twitch_archive_filename(item)): item for lang, (_, items) in ranked.items()
                              for item in items if item["provider"] == "Twitch" and item["kind"] == "vod"}
        for lang, files in self.available_files().items():
            for file in files:
                observed = None
                if (lang, file) in known:
                    continue
                try:
                    path = self.media_file(lang, file)
                    stat = path.stat()
                    observed = (stat.st_size, stat.st_mtime_ns)
                    if time.time() - stat.st_mtime < 5 or self.scanned.get((lang, file)) == observed:
                        continue
                    source = {"lang": lang, "file": file}
                    discovered = None
                    if identity := self.inbox_key(file):
                        with self.db() as db:
                            record = db.execute("""SELECT id,url,title FROM sources WHERE lang=? AND status='waiting'
                                                AND (source_key=? OR url=?) ORDER BY id DESC LIMIT 1""",
                                                (lang, *identity)).fetchone()
                        discovered = twitch_records.get((lang, f"{Path(file).stem}.mp4"))
                        if not record and not discovered:
                            continue
                        source.update(discovered if discovered else dict(record))
                    with self.inbox_lock:
                        self.add_source(source, key_override=discovered["key"] if discovered else None,
                                        discovered_at=discovered["observed"] if discovered else None)
                    self.scanned[lang, file] = observed
                except (OSError, ValueError):
                    if self.inbox_key(file) is None and observed is not None:
                        self.scanned[lang, file] = observed

    @staticmethod
    def twitch_archive_filename(item):
        link = re.fullmatch(r"https://www\.twitch\.tv/videos/([0-9]{1,30})", item["url"])
        return f"twitch-vod-{link[1]}.mp4" if link else None

    def twitch_source_health(self, db, recordings):
        handled = {(row["lang"], row["url"]) for row in db.execute("""SELECT lang,url FROM sources
            WHERE rel_file IS NOT NULL OR status NOT IN ('waiting','expired')""")}
        items = [item for item in recordings if (item["lang"], item["url"]) not in handled]
        with_access = 0
        for item in items:
            filename = self.twitch_archive_filename(item)
            local = self.input / item["lang"] / filename if filename else None
            if (local and not local.is_symlink() and local.is_file()) or twitch_media.configured(item):
                with_access += 1
        active = db.execute("""SELECT count(*) FROM sources
                               WHERE lang='RU' AND status IN ('queued','editing')""").fetchone()[0]
        return {"archives": len(items), "accessible": with_access, "active": active}

    def fetch_twitch_media(self):
        strict = directory_mode() and os.environ.get(twitch_directory.STRICT_RU) == "1"
        instant = now()
        self.media_checks = {url: retry for url, retry in self.media_checks.items() if retry > instant}
        with self.db() as db:
            if not db.execute("SELECT value FROM settings WHERE name='scan'").fetchone()[0]:
                return
            counts = self.ready_counts(db)
            _, _, ranked = self.current_discovery(db, instant)
            waiting = {(row["lang"], row["url"]): row["id"] for row in db.execute(
                "SELECT id,lang,url FROM sources WHERE status='waiting' AND rel_file IS NULL AND kind='vod'")}
            handled = {(row["lang"], row["url"]) for row in db.execute(
                """SELECT lang,url FROM sources
                   WHERE kind='vod' AND (rel_file IS NOT NULL OR status NOT IN ('waiting','expired'))""")} if strict else set()
            pending_langs = {row["lang"] for row in db.execute("""SELECT DISTINCT lang FROM sources
                WHERE rel_file IS NOT NULL AND status='queued' AND auto_plan IS NULL""")}
        for lang, (_, rows) in ranked.items():
            if not automatic_editing(lang) or self.inventory_capacity(counts, lang) <= 0 or lang in pending_langs:
                continue
            candidates = sorted(rows, key=lambda item: (
                item.get("directory_priority", float("inf")), item.get("directory_rank", float("inf")), -item["popularity"],
                -datetime.fromisoformat(item["observed"]).timestamp()))
            for item in candidates:
                url = item["url"]
                registered = (lang, url) in waiting
                if (item["provider"] != "Twitch" or item["kind"] != "vod" or url in self.media_checks
                        or (strict and lang != "RU")
                        or (strict and (lang, url) in handled)
                        or (not strict and not registered) or not twitch_media.configured(item)):
                    continue
                filename = self.twitch_archive_filename(item)
                if not filename:
                    continue
                destination = self.input / lang / filename
                if destination.exists() or destination.is_symlink():
                    continue
                self.media_checks[url] = instant + timedelta(minutes=30)
                try:
                    received = twitch_media.fetch(item, destination, current=instant)
                    detail = "" if received is not None else TWITCH_FILE_PENDING
                except (transport.ConnectionProblem, OSError, ValueError):
                    received = None
                    detail = TWITCH_FILE_ERROR
                if strict:
                    if received is None:
                        continue
                    self.add_source(item, key_override=item["key"], discovered_at=item["observed"])
                elif registered:
                    with self.db() as db:
                        db.execute("UPDATE sources SET detail=? WHERE id=? AND status='waiting'",
                                   (detail, waiting[lang, url]))
                break

    @staticmethod
    def current_discovery(db, instant):
        fallback = discovery.roster()
        roster, updated, candidates = {}, {}, {}
        directory = directory_mode()
        strict = directory and os.environ.get(twitch_directory.STRICT_RU) == "1"
        rows = db.execute("SELECT lang,provider,checked_at,payload FROM discovery_feeds").fetchall()
        for lang in LANGS:
            feeds = [row for row in rows if row["lang"] == lang and
                     datetime.fromisoformat(row["checked_at"]) >= instant - discovery.WINDOW and
                     (not directory or row["provider"] == "Twitch")]
            entries = [item for row in feeds for item in json.loads(row["payload"])]
            if directory:
                if strict:
                    entries = entries if lang == "RU" else []
                data = regional.current_candidates((item for item in entries if "directory_rank" in item or
                    item.get("directory_followup") and item["kind"] == "vod"), instant)
                board = []
                if feeds and datetime.fromisoformat(feeds[0]["checked_at"]) >= instant - timedelta(hours=6):
                    ranked = regional.current_candidates((item for item in entries if "directory_rank" in item), instant)
                    by_broadcaster = {}
                    for item in sorted(ranked, key=lambda entry: entry["directory_rank"]):
                        by_broadcaster.setdefault(item["twitch_user"], item)
                    board = list(by_broadcaster.values())
            else:
                data = regional.current_candidates(entries, instant)
                board = regional.leaderboard(data)
            candidates[lang] = (board, data)
            if feeds:
                updated[lang] = max(row["checked_at"] for row in feeds)
                roster[lang] = [row["creator"] for row in board]
            else:
                roster[lang] = [] if directory else fallback.get(lang, [])[:regional.BOARD_SIZE]
        return roster, updated, candidates

    def search(self, *, force=False, live_only=False):
        if not search_configured():
            return
        if not self.search_lock.acquire(blocking=False):
            return
        try:
            self.search_cycle(force, live_only)
        finally:
            self.search_lock.release()

    def search_cycle(self, force, live_only):
        instant = now()
        checked = iso(instant)
        directory = directory_mode()
        run_name = "twitch-directory" if directory else "regional"
        retry_name = "twitch-directory-retry"
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT value FROM settings WHERE name='scan'").fetchone()[0]:
                return
            retry = directory and db.execute("SELECT started_at FROM discovery_runs WHERE name=?",
                                             (retry_name,)).fetchone()
            previous = retry or db.execute("SELECT started_at FROM discovery_runs WHERE name=?", (run_name,)).fetchone()
            interval = timedelta(minutes=5) if retry else (
                TWITCH_DISCOVERY_INTERVAL if directory else timedelta(seconds=regional.REGIONAL_INTERVAL))
            if not force and previous and datetime.fromisoformat(previous[0]) + interval > instant:
                return
            if not directory and not live_only:
                db.execute("INSERT OR REPLACE INTO discovery_runs VALUES (?,?)", (run_name, checked))
            past_live = {(row["lang"], row["source_key"].rsplit(":", 1)[1]) for row in db.execute(
                """SELECT lang,source_key FROM sources WHERE kind='live' AND source_key LIKE 'twitch:stream:%'
                   AND COALESCE(last_seen,created)>=?""", (iso(instant - discovery.WINDOW),))}
            if directory:
                previous_selection = {row["lang"]: json.loads(row["payload"]) for row in db.execute(
                    "SELECT lang,payload FROM discovery_feeds WHERE provider='Twitch' AND checked_at>=?",
                    (iso(instant - discovery.WINDOW),))}
            else:
                _, _, old = self.current_discovery(db, instant)
                old_twitch = [item for _, entries in old.values() for item in entries
                              if item["provider"] == "Twitch" and item.get("twitch_login")]
                active_watch = {item["twitch_user"]: (item["twitch_login"], item["lang"])
                                for item in old_twitch if item["kind"] == "live"}
                past_live.update((item["lang"], item["key"].rsplit(":", 1)[1])
                                 for item in old_twitch if item["kind"] == "live")
        updates, errors = [], []
        if directory:
            try:
                twitch = twitch_directory.search(LANGS, instant, known_live=past_live, previous=previous_selection)
                updates.extend((lang, "Twitch", rows) for lang, rows in (twitch or {}).items())
            except transport.ConnectionProblem as error:
                with self.db() as db:
                    db.execute("INSERT OR REPLACE INTO discovery_runs VALUES (?,?)", (retry_name, checked))
                self.search_status = f"{error} · повтор через 5 минут"
                return
        else:
            for lang, code in LANGS.items():
                try:
                    rows = regional.youtube_market(lang, code, instant, live_only=live_only)
                    if rows is not None:
                        updates.append((lang, "YouTube", rows))
                except transport.ConnectionProblem as error:
                    errors.append(f"{lang} · {error}")
            try:
                twitch = regional.twitch_markets(LANGS, instant, past_live, active_watch, live_only=live_only)
                updates.extend((lang, "Twitch", rows) for lang, rows in (twitch or {}).items())
            except transport.ConnectionProblem as error:
                errors.append(str(error))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if directory and updates:
                db.execute("DELETE FROM discovery_runs WHERE name=?", (retry_name,))
                db.execute("INSERT OR REPLACE INTO discovery_runs VALUES (?,?)", (run_name, checked))
            for lang, provider, items in updates:
                if not directory:
                    if live_only:
                        cached = [item for item in old[lang][1]
                                  if item["provider"] == provider and item["kind"] != "live"]
                        items = [*cached, *items]
                    items = regional.current_candidates(items, instant)
                db.execute("INSERT OR REPLACE INTO discovery_feeds VALUES (?,?,?,?)",
                           (lang, provider, checked, json.dumps(items, ensure_ascii=False)))
            _, _, candidates = self.current_discovery(db, instant)
            known_keys = {row["source_key"] for row in db.execute(
                """SELECT source_key FROM sources WHERE status!='dismissed' AND kind='live'
                   AND COALESCE(last_seen,created)>=?""", (iso(instant - discovery.WINDOW),))}
        saved = 0
        strict = directory and os.environ.get(twitch_directory.STRICT_RU) == "1"
        for board, rows in candidates.values():
            eligible = {item["author"] for item in board}
            per_author = {}
            for row in sorted(rows, key=lambda item: (item["popularity"], item["observed"]), reverse=True):
                author, key = row["author"], row["key"]
                if strict:
                    filename = self.twitch_archive_filename(row)
                    if row["kind"] != "vod" or not filename:
                        continue
                    inbox = self.input / row["lang"] / filename
                    if not inbox.is_file() or inbox.is_symlink():
                        continue
                if not directory and key not in known_keys and (
                        author not in eligible or per_author.get(author, 0) >= 2):
                    continue
                per_author[author] = per_author.get(author, 0) + 1
                try:
                    saved += self.add_source(row, key_override=key, discovered_at=row["observed"])
                except ValueError:
                    continue
        with self.db() as db:
            db.execute("""UPDATE sources SET status='expired' WHERE status='waiting' AND last_seen IS NOT NULL
                          AND (last_seen < ? OR (kind='live' AND last_seen < ?))""",
                       (iso(instant - discovery.WINDOW), iso(instant - timedelta(hours=6))))
            if directory:
                if updates:
                    current_keys = {row["key"] for _, _, items in updates for row in items}
                    stale = [row["id"] for row in db.execute("""SELECT id,source_key FROM sources
                        WHERE status='waiting' AND last_seen IS NOT NULL""") if row["source_key"] not in current_keys]
                    db.executemany("UPDATE sources SET status='expired' WHERE id=? AND status='waiting'",
                                   [(identifier,) for identifier in stale])
            else:
                for lang in LANGS:
                    db.execute("""UPDATE sources SET status='expired' WHERE id IN (
                        SELECT id FROM sources WHERE lang=? AND status='waiting' AND last_seen IS NOT NULL
                        ORDER BY last_seen DESC,id DESC LIMIT -1 OFFSET 30)""", (lang,))
        cadence = "проверка каждые 30 минут" if directory else "проверка раз в 5 часов"
        description = "Каталог Twitch обновлён" if strict else f"Новые источники: {saved}"
        self.search_status = " · ".join(errors) if errors else f"{description} · {cadence}"

    def dismiss_source(self, identifier):
        with self.db() as db:
            db.execute("UPDATE sources SET status='dismissed' WHERE id=? AND status='waiting'", (identifier,))

    def edit_one(self):
        if not self.edit_lock.acquire(blocking=False):
            return
        try:
            self.process_edit()
        finally:
            self.edit_lock.release()

    def can_edit(self, row, counts):
        capacity = self.inventory_capacity(counts, row["lang"])
        if capacity <= 0 or (self.unattended_source(row) and not automatic_editing(row["lang"])):
            return False
        if row["status"] == "waiting_layout" and not (
            reaction.has_profile(self.input / row["lang"] / row["rel_file"])
            or automatic_editing(row["lang"]) and not row["auto_attempt"]
        ):
            return False
        moments = json.loads(row["edit_profile"]).get("moments") if row["edit_profile"] else None
        return not moments or len(moments) <= capacity

    def process_edit(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.inventory_limit() == RU_INVENTORY_LIMIT:
                db.execute("""UPDATE sources SET status='archived'
                    WHERE status='ready' AND lang='RU' AND last_seen IS NOT NULL AND edit_profile IS NULL""")
                if automatic_editing("RU"):
                    db.execute("""UPDATE sources SET status='dismissed'
                        WHERE status='waiting_layout' AND lang='RU' AND last_seen IS NOT NULL
                        AND edit_profile IS NULL AND auto_attempt=1""")
            counts = self.ready_counts(db)
            with closing(db.execute("""
                SELECT * FROM sources WHERE status IN ('queued','waiting_layout')
                AND rel_file IS NOT NULL ORDER BY queued_at,id
            """)) as pending:
                source = next((row for row in pending if self.can_edit(row, counts)), None)
            if source is None:
                return
            capacity = self.inventory_capacity(counts, source["lang"])
            db.execute("UPDATE sources SET status='editing', detail='' WHERE id=?", (source["id"],))
        unattended = self.unattended_source(source)
        try:
            saved = source["edit_profile"]
            plan = json.loads(source["auto_plan"]) if source["auto_plan"] and not saved else None
            offset = source["auto_offset"] if plan else 0
            dest = self.output / f"source-{source['id']}"
            if saved:
                revision = hashlib.sha256(f"{source['mark']}\n{saved}".encode("utf-8")).hexdigest()[:16]
                dest = dest.with_name(f"{dest.name}-{revision}")
            elif offset:
                dest = dest.with_name(f"{dest.name}-part-{offset + 1}")
            path = self.media_file(source["lang"], source["rel_file"])
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != source["fingerprint"]:
                    raise ValueError("Файл изменился после добавления.")
            cmd = [sys.executable, str(HERE.parent / "clips.py"), str(path), "--output", str(dest),
                   "--topic", "GTA 6", "--count", str(min(3, capacity)), "--seconds", "35"]
            if source["url"]:
                cmd.extend(("--source-url", source["url"]))
            manifest = dest / "manifest.json"
            speech = json.loads(source["speech_language"]) if source["speech_language"] and not saved else {}
            if not manifest.exists():
                profile = json.loads(saved) if saved else None
                if (profile is None and plan is None and automatic_editing(source["lang"])
                        and not reaction.has_profile(path)):
                    with self.db() as db:
                        db.execute("UPDATE sources SET auto_attempt=MAX(auto_attempt, 1) WHERE id=?", (source["id"],))
                    raw = autocut.prepare(path, source["lang"], source["title"], reaction.MAX_AUTOMATIC_MOMENTS)
                    speech = raw.pop("speech_language", None) or {}
                    speech_languages = raw.pop("speech_languages", None) or []
                    profile = reaction.normalize_profile(raw, automatic=True)
                    plan = {"profile": profile, "speech_language": speech, "speech_languages": speech_languages}
                if plan:
                    profile = plan["profile"]
                moments = profile.get("moments") if profile else None
                if plan and moments:
                    if "batch_end" not in plan:
                        batch_size = min(AUTO_RENDER_BATCH, capacity) if unattended else capacity
                        plan["batch_end"] = min(offset + batch_size, len(moments))
                        with self.db() as db:
                            db.execute("UPDATE sources SET speech_language=?,auto_plan=? WHERE id=?",
                                       (json.dumps(speech), json.dumps(plan, ensure_ascii=False), source["id"]))
                    moments = moments[offset:plan["batch_end"]]
                    if unattended:
                        evidence = plan["speech_languages"][offset:plan["batch_end"]]
                        moments = [moment for moment, detected in zip(moments, evidence)
                                   if self.confirmed_speech(detected, source["lang"])]
                if profile and profile["mode"] == "model":
                    moments = moment_model.select_moments(profile["transcript"], source["lang"])[:min(3, capacity)]
                if moments is None and source["mark"] is not None:
                    cmd.extend(("--at", str(source["mark"])))
                if moments != []:
                    with reaction.editor_arguments(
                        path, required=bool(source["url"]) or os.environ.get("STREAMCUT_REACTION_ONLY") == "1",
                        transcribe=os.environ.get("STREAMCUT_TRANSCRIBE") == "1", profile=profile, moments=moments,
                    ) as args:
                        run = subprocess.run(cmd + args, cwd=HERE.parent, capture_output=True, text=True, timeout=7200)
                    if run.returncode:
                        raise ValueError(run.stderr.strip()[-650:] or "Ошибка монтажа.")
                    if not manifest.is_file():
                        raise ValueError("В экспорте отсутствует манифест.")
            clips = json.loads(manifest.read_text())["clips"] if manifest.is_file() else []
            speech_by_range = {}
            if plan:
                for moment, evidence in zip(plan["profile"]["moments"], plan["speech_languages"]):
                    key = (round(moment["start_seconds"], 3), round(moment["end_seconds"], 3))
                    speech_by_range[key] = evidence
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                if len(clips) > self.inventory_capacity(self.ready_counts(db), source["lang"]):
                    db.execute("UPDATE sources SET status='queued',detail=? WHERE id=?",
                               ("Экспорт ждёт свободного места.", source["id"]))
                    return
                for index, clip in enumerate(clips, 1):
                    filename = (dest / clip["video"]).resolve()
                    if not filename.is_relative_to(dest) or not filename.is_file():
                        raise ValueError("В экспорте отсутствует файл клипа.")
                    key = (clip.get("start_seconds"), clip.get("end_seconds"))
                    source_speech = speech_by_range.get(key, speech)
                    detected = clip.get("speech_language") or source_speech or {}
                    lang = source["lang"]
                    verified = self.confirmed_speech(detected, lang)
                    if unattended and not verified:
                        continue
                    title = clip.get("headline") or {"EN": f"GTA 6 • Stream Highlight #{index}",
                             "ESP": f"GTA 6 • Momento del directo #{index}"}.get(lang, clip["title"])
                    description, tags = clip_metadata(lang, title, source["url"])
                    db.execute("""INSERT INTO clips(source_id,lang,title,relative_path,verified,description,hashtags)
                                  VALUES (?,?,?,?,?,?,?)""", (source["id"], lang, title,
                               str(filename.relative_to(self.output)), int(verified), description,
                               json.dumps(tags, ensure_ascii=False)))
                next_offset = plan.pop("batch_end") if plan else 0
                remaining = len(plan["profile"]["moments"]) - next_offset if plan else 0
                if remaining > 0:
                    db.execute("""UPDATE sources SET status='queued',detail=?,auto_offset=?,auto_plan=?,
                                  queued_at=? WHERE id=?""",
                               (f"Осталось смонтировать моментов: {remaining}.", next_offset,
                                json.dumps(plan, ensure_ascii=False),
                                now().isoformat(timespec="microseconds"), source["id"]))
                else:
                    rendered = db.execute("SELECT 1 FROM clips WHERE source_id=? LIMIT 1", (source["id"],)).fetchone()
                    if unattended:
                        state = "archived" if rendered else "dismissed"
                        detail = "" if rendered else "Нет утверждённых интервалов с речью на языке канала."
                    else:
                        state = "ready" if rendered else "empty"
                        detail = "" if rendered else "Добавьте источник с таймкодом."
                    db.execute("UPDATE sources SET status=?, detail=?,auto_offset=?,auto_plan=NULL WHERE id=?",
                               (state, detail, next_offset, source["id"]))
                self.populate(db)
        except reaction.MissingReactionProfile as error:
            with self.db() as db:
                db.execute("UPDATE sources SET status=?, detail=? WHERE id=?",
                           ("dismissed" if unattended else "waiting_layout", str(error), source["id"]))
        except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired, KeyError) as error:
            with self.db() as db:
                if unattended and isinstance(error, subprocess.TimeoutExpired) and source["auto_attempt"] < 2:
                    db.execute("""UPDATE sources SET status='queued', detail=?, auto_attempt=2, queued_at=?
                        WHERE id=?""", ("Монтаж не успел завершиться. Сервис повторит попытку.",
                                        now().isoformat(timespec="microseconds"), source["id"]))
                else:
                    detail = "Монтаж превысил допустимое время." if isinstance(error, subprocess.TimeoutExpired) else str(error)
                    db.execute("UPDATE sources SET status='failed', detail=? WHERE id=?", (detail[:650], source["id"]))

    @staticmethod
    def confirmed_speech(evidence, lang):
        return bool(evidence and evidence.get("code") == LANGS[lang]
                    and evidence.get("confidence", 0) >= MIN_SPEECH_CONFIDENCE)

    def populate(self, db):
        allowed = youtube_auto_broadcasters()
        choices = {}
        accounts = db.execute("""SELECT c.id AS clip_id, c.lang, c.verified, a.id AS account_id,
            a.auto, a.platform, a.refresh_token IS NOT NULL AS connected, h.state AS route_state, s.twitch_broadcaster
            FROM clips c JOIN accounts a ON a.lang=c.lang
            LEFT JOIN sources s ON s.id=c.source_id
            LEFT JOIN route_health h ON h.route=a.route AND h.platform=a.platform
            WHERE NOT EXISTS (SELECT 1 FROM queue q JOIN accounts existing ON existing.id=q.account_id
                               WHERE q.clip_id=c.id AND existing.platform=a.platform)
            ORDER BY c.id, a.platform, a.id""")
        for account in accounts:
            choices.setdefault((account["clip_id"], account["platform"]), []).append(account)
        instant = now() + PUBLISH_LEAD
        for (clip_id, platform), options in choices.items():
            ranked = []
            for account in options:
                automatic = (platform == "YouTube" and account["lang"] == "RU" and account["auto"] and account["connected"]
                             and account["verified"] and self.uploads_enabled()
                             and (allowed is None or account["twitch_broadcaster"] in allowed))
                slot = self.next_slot(db, account["account_id"], account["lang"], instant)
                on_hold = account["route_state"] in ("failed", "unconfigured")
                day = datetime.fromisoformat(slot).astimezone(schedule.ZONES[account["lang"]]).date()
                ranked.append((not automatic, on_hold if automatic else False, day, account["account_id"], slot))
            review, held, _, account_id, slot = min(ranked)
            state = "review" if review else ("route_wait" if held else "scheduled")
            db.execute("INSERT INTO queue(clip_id,account_id,at,status,auto_scheduled) VALUES (?,?,?,?,?)",
                       (clip_id, account_id, slot, state, None if review else 1))

    @staticmethod
    def reserved_dates(db, account, exclude=None):
        placeholders = ",".join("?" for _ in RESERVING_QUEUE_STATES)
        return [datetime.fromisoformat(row["at"]) for row in db.execute(
            f"SELECT at FROM queue WHERE account_id=? AND id IS NOT ? AND status IN ({placeholders})",
            (account, exclude, *RESERVING_QUEUE_STATES))]

    @staticmethod
    def next_slot(db, account, lang, after, exclude=None):
        return iso(schedule.available_slot(after, lang, Studio.reserved_dates(db, account, exclude)))

    def queue_action(self, data):
        identifier, action = data.get("id"), data.get("action")
        if action == "save":
            with self.db() as db:
                existing = db.execute("""SELECT q.*,a.lang FROM queue q JOIN accounts a ON a.id=q.account_id
                    WHERE q.id=? AND q.status='preloaded'""",(identifier,)).fetchone()
            if existing:
                self.move_youtube(dict(existing), text_field(data, "at", 16))
                return
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("""SELECT q.*, a.lang, a.platform, a.refresh_token IS NOT NULL AS connected
                              FROM queue q JOIN accounts a ON a.id=q.account_id WHERE q.id=?""",
                             (identifier,)).fetchone()
            if not job or job["status"] in ("sending", "checking", "remote_check", "preloaded", "unknown",
                                            "uploaded", "cancelled") or job["external_id"]:
                raise ValueError("Это задание нельзя изменить.")
            if action == "cancel":
                db.execute("UPDATE queue SET status='cancelled' WHERE id=?", (identifier,))
                return
            planned = schedule.local_time(text_field(data, "at", 16), job["lang"])
            if planned <= now():
                raise ValueError("Выберите время в будущем.")
            if action == "send":
                if (job["platform"] != "YouTube" or job["lang"] != "RU" or
                        not job["connected"] or not self.uploads_enabled()):
                    raise ValueError("Подключите RU-канал YouTube через OAuth и включите планирование.")
                state = "scheduled"
            elif action == "save":
                state = job["status"] if job["status"] in ("scheduled", "route_wait") else "review"
            else:
                raise ValueError("Неизвестное действие.")
            if state in RESERVING_QUEUE_STATES:
                start, end = schedule.civil_day(planned, job["lang"])
                total = sum(start <= date < end for date in self.reserved_dates(db, job["account_id"], identifier))
                limit = schedule.daily_limit(job["lang"])
                if total >= limit:
                    raise ValueError(f"На эти сутки уже достигнут лимит канала ({limit}).")
            automatic = 0 if action == "send" else job["auto_scheduled"]
            db.execute("UPDATE queue SET at=?,status=?,detail='',retry_at=NULL,auto_scheduled=? WHERE id=?",
                       (iso(planned), state, automatic, identifier))

    @staticmethod
    def connection_snapshot(rows):
        collected = {(row["route"], row["platform"]): row for row in rows}
        threshold = iso(now() - timedelta(minutes=3))
        results = []
        for route in (transport.GATEWAY, "RU", "US", "ES", "server"):
            for platform in transport.API_HOSTS:
                row = collected.get((route, platform))
                if not transport.route_available(route):
                    row = dict(route=route, platform=platform, state="unconfigured",
                               country=None, detail="Маршрут не настроен.", checked_at=None)
                elif not row:
                    row = dict(route=route, platform=platform, state="unchecked",
                               country=None, detail="Проверки ещё не было.", checked_at=None)
                elif row["checked_at"] < threshold:
                    row = {**row, "state": "stale"}
                results.append(row)
        return results

    def record_connection(self, result, checking_id=None):
        route, platform = result["route"], result["platform"]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("""INSERT INTO route_health(route,platform,state,country,detail,checked_at)
                VALUES (:route,:platform,:state,:country,:detail,:checked_at)
                ON CONFLICT(route,platform) DO UPDATE SET state=excluded.state,country=excluded.country,
                detail=excluded.detail,checked_at=excluded.checked_at
                WHERE excluded.checked_at>=route_health.checked_at""", result)
            if not saved.rowcount:
                if checking_id is not None:
                    db.execute("""UPDATE queue SET status='scheduled' WHERE id=?
                        AND status IN ('checking','sending')""", (checking_id,))
                return
            if platform == "YouTube" and not transport.youtube_write_route_allowed(route):
                return
            if result["state"] != "ok":
                db.execute("""UPDATE queue SET status='route_wait',detail=? WHERE status='scheduled'
                    AND account_id IN (SELECT id FROM accounts WHERE route=? AND platform=?)""",
                           (result["detail"], route, platform))
                if checking_id is not None:
                    db.execute("""UPDATE queue SET status='route_wait',detail=? WHERE id=?
                        AND status IN ('checking','sending')""",
                               (result["detail"], checking_id))
                return
            recovered = db.execute("""SELECT q.id,q.account_id,q.at,a.lang FROM queue q
                                      JOIN accounts a ON q.account_id=a.id
                                      WHERE a.route=? AND a.platform=? AND q.status='route_wait'
                                      ORDER BY q.at,q.id""", (route, platform)).fetchall()
            instant = now()
            for row in recovered:
                slot = row["at"]
                if datetime.fromisoformat(slot) <= instant:
                    slot = self.next_slot(db, row["account_id"], row["lang"], instant, row["id"])
                db.execute("UPDATE queue SET status='scheduled',at=?,detail='' WHERE id=?", (slot, row["id"]))

    def check_connection(self, route, platform):
        if route not in transport.REGIONS or platform not in transport.API_HOSTS:
            raise ValueError("Выберите маршрут и площадку.")
        result = connections.observe(route, platform)
        self.record_connection(result)
        return result

    def monitor_connections(self):
        routes = {(route, platform) for route in transport.REGIONS - {"server"} if transport.route_available(route)
                  for platform in transport.API_HOSTS}
        with self.db() as db:
            routes.update(tuple(row) for row in db.execute("SELECT DISTINCT route,platform FROM accounts"))
        if routes:
            with ThreadPoolExecutor(max_workers=6) as pool:
                for result in pool.map(lambda pair: connections.observe(*pair), sorted(routes)):
                    self.record_connection(result)

    def refresh_metrics(self):
        with self.db() as db:
            records = db.execute("""SELECT a.*,m.payload FROM accounts a
                LEFT JOIN account_metrics m ON m.account_id=a.id""").fetchall()
        threshold = iso(now() - timedelta(hours=6))
        for record in records:
            account = dict(record)
            old = json.loads(account.pop("payload") or "{}")
            if old.get("checked_at", "") >= threshold:
                continue
            try:
                stats = metrics.read(account)
            except transport.ConnectionProblem as error:
                stats = {**old, "note": "Последнее обновление не удалось. " + str(error)}
            if stats is not None:
                stats["checked_at"] = iso(now())
                with self.db() as db:
                    current = db.execute("SELECT * FROM accounts WHERE id=?", (account["id"],)).fetchone()
                    if not current or dict(current) != account:
                        continue
                    db.execute("INSERT OR REPLACE INTO account_metrics(account_id,payload) VALUES (?,?)",
                               (account["id"], json.dumps(stats, ensure_ascii=False)))

    def upload_status(self, identifier, status, detail="", retry_at=None, external=None, at=None):
        with self.db() as db:
            db.execute("""UPDATE queue SET status=?,detail=?,retry_at=?,
                external_id=COALESCE(?,external_id),at=COALESCE(?,at) WHERE id=?""",
                       (status, detail, retry_at, external, at, identifier))

    @staticmethod
    def shorts_valid(path):
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
            "-of", "json", str(path),
        ], capture_output=True, text=True, timeout=30, check=True)
        info = json.loads(result.stdout)
        video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
        return (path.suffix.lower() == ".mp4" and video is not None and video["width"] < video["height"]
                and 25.0 <= float(info["format"]["duration"]) <= 50.05)

    @staticmethod
    def planned_time(job, remote_dates, instant):
        earliest = instant + PUBLISH_LEAD
        requested = datetime.fromisoformat(job["at"])
        reserved = [datetime.fromisoformat(date) for date in remote_dates]
        lang = job["lang"]
        day_start, day_end = schedule.civil_day(requested, lang)
        automatic = job["auto_scheduled"] == 1
        if automatic:
            available = schedule.slot_is_available(requested, lang, reserved)
        else:
            available = (sum(day_start <= stamp < day_end for stamp in reserved) < schedule.daily_limit(lang)
                         and all(abs(stamp - requested) >= schedule.slot_spacing(lang) for stamp in reserved))
        if requested >= earliest and available:
            return requested
        after = earliest if automatic else max(earliest, requested)
        return schedule.available_slot(after, lang, reserved)

    @staticmethod
    def schedule_limit():
        raw = os.environ.get("STREAMCUT_YOUTUBE_SCHEDULE_LIMIT", str(max(10, schedule.daily_limit())))
        if not raw.isdecimal() or not 1 <= int(raw) <= 100:
            raise ValueError("STREAMCUT_YOUTUBE_SCHEDULE_LIMIT должен быть от 1 до 100.")
        return int(raw)

    def slot_wait(self, account_id, delay, message, *, project_wide=False, job_id=None):
        retry = now() + timedelta(seconds=delay)
        with self.db() as db:
            if project_wide:
                db.execute("""INSERT INTO settings VALUES ('youtube_retry_after',?)
                    ON CONFLICT(name) DO UPDATE SET value=max(value,excluded.value)""", (int(retry.timestamp()),))
            db.execute("""UPDATE queue SET status='scheduled',retry_at=max(COALESCE(retry_at,?),?),detail=?
                WHERE (account_id=? OR (? AND account_id IN (SELECT id FROM accounts WHERE platform='YouTube')))
                AND (status='scheduled' OR id=? AND status IN ('checking','sending')) AND external_id IS NULL""",
                       (iso(retry), iso(retry), message, account_id, project_wide, job_id))

    @staticmethod
    def youtube_throttled(db, instant):
        retry = db.execute("SELECT value FROM settings WHERE name='youtube_retry_after'").fetchone()
        return retry is not None and retry[0] > instant.timestamp()

    def reassign_overflow(self, job, account_id, instant, *, before_day=None):
        if job["auto_scheduled"] != 1:
            return False
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT value FROM settings WHERE name='paused'").fetchone()[0]:
                return False
            accounts = db.execute("""SELECT a.id,a.route,h.state,
                (SELECT count(*) FROM queue q WHERE q.account_id=a.id AND q.at>?
                 AND q.status IN ('preloaded','remote_check') AND q.external_id IS NOT NULL) AS preloaded
                FROM accounts a LEFT JOIN route_health h ON a.route=h.route AND a.platform=h.platform
                WHERE a.id>? AND a.lang=? AND a.platform='YouTube' AND a.auto=1 AND a.refresh_token IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM queue q WHERE q.account_id=a.id
                                AND q.status='scheduled' AND q.retry_at>?)
                ORDER BY a.id""", (iso(instant), account_id, job["lang"], iso(instant))).fetchall()
            for account in accounts:
                if (account["state"] in ("unconfigured", "failed") or
                        not transport.youtube_write_route_allowed(account["route"]) or
                        account["preloaded"] >= self.schedule_limit()):
                    continue
                slot = self.next_slot(db, account["id"], job["lang"], instant + PUBLISH_LEAD)
                day = datetime.fromisoformat(slot).astimezone(schedule.ZONES[job["lang"]]).date()
                if before_day is not None and day >= before_day:
                    continue
                updated = db.execute("""UPDATE queue SET account_id=?,at=?,status='scheduled',retry_at=NULL,
                     detail='Перенесено на следующий канал по лимиту расписания.' WHERE id=? AND account_id=?
                     AND status='checking' AND sent_at IS NULL AND external_id IS NULL""",
                                     (account["id"], slot, job["id"], account_id))
                return updated.rowcount == 1
        return False

    def apply_youtube_status(self, job, video, instant):
        if not video:
            if job["status"] == "preloaded":
                self.upload_status(job["id"], "blocked", "Ранее запланированное видео больше не найдено в YouTube.")
            return
        if (video.get("uploadStatus") in {"deleted", "failed", "rejected"} or
                video.get("processingStatus") in {"terminated", "failed"}):
            self.upload_status(job["id"], "blocked", "YouTube отклонил обработку. Проверьте YouTube Studio.")
            return
        if video.get("privacyStatus") == "public":
            self.upload_status(job["id"], "uploaded")
            return
        if video.get("privacyStatus") == "private":
            planned = youtube_release.utc_datetime(video.get("publishAt"))
            if planned and planned > instant:
                self.upload_status(job["id"], "preloaded", at=iso(planned))
                return
            if not planned and video.get("uploadStatus") == "processed":
                self.upload_status(job["id"], "blocked", "YouTube принял файл приватно. Проверьте дату в YouTube Studio.")
                return
        self.upload_status(job["id"], job["status"], "YouTube ещё проверяет видео или его расписание.")

    def sync_youtube(self):
        if not self.uploads_enabled():
            return
        instant = now()
        with self.db() as db:
            if self.youtube_throttled(db, instant):
                return
            jobs = db.execute("""SELECT id,account_id,status,external_id FROM queue
                WHERE external_id IS NOT NULL AND status IN ('remote_check','preloaded')
                ORDER BY CASE WHEN status='remote_check' THEN 0 ELSE 1 END,at""").fetchall()
        by_account = {}
        for job in jobs:
            by_account.setdefault(job["account_id"], []).append(job)
        for account_id, pending in by_account.items():
            account = self.get_account(account_id)
            if not account["refresh_token"]:
                continue
            try:
                token = transport.prepare_upload(account)
                videos = youtube_release.publication_status(account["route"], token,
                                                            [job["external_id"] for job in pending])
            except youtube_release.SlotUnavailable as error:
                if error.project_wide:
                    self.slot_wait(account_id, error.retry_after_seconds, str(error), project_wide=True)
                    return
                continue
            except transport.ConnectionProblem:
                continue
            for job in pending:
                self.apply_youtube_status(job, videos.get(job["external_id"]), instant)

    def move_youtube(self, job, local):
        if not self.uploads_enabled() or job["lang"] != "RU":
            raise ValueError("Планирование RU-канала выключено.")
        planned = schedule.local_time(local, job["lang"])
        if planned < now() + PUBLISH_LEAD:
            raise ValueError("Для переноса выберите время не раньше чем через 15 минут.")
        account = self.get_account(job["account_id"])
        if not account["refresh_token"]:
            raise ValueError("Подключите канал YouTube через OAuth.")
        transport.require_youtube_write_route(account["route"])
        token = transport.prepare_upload(account)
        try:
            youtube_release.reschedule(account["route"], token, job["external_id"], planned,
                                       expected_channel=account["target"])
        except youtube_release.ScheduleUncertain:
            self.upload_status(job["id"], "remote_check", "Проверяем новый час в YouTube.", at=iso(planned))
            raise
        self.upload_status(job["id"], "preloaded", at=iso(planned))

    def publish_one(self):
        if not self.uploads_enabled():
            return
        started = now()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if (db.execute("SELECT value FROM settings WHERE name='paused'").fetchone()[0] or
                    self.youtube_throttled(db, started)):
                return
            allowed = youtube_auto_broadcasters()
            if allowed is not None:
                self.hold_unapproved_youtube_uploads(db, allowed)
                if allowed:
                    self.promote_automatic_youtube(db, allowed)
            if transport.youtube_gateway_required():
                db.execute("""UPDATE queue SET status='route_wait',detail=? WHERE status='scheduled'
                    AND external_id IS NULL AND account_id IN (SELECT id FROM accounts
                            WHERE platform='YouTube' AND lang='RU' AND route<>?)""",
                           (transport.GATEWAY_REQUIRED_DETAIL, transport.GATEWAY))
            job = db.execute("""SELECT q.id,q.account_id,q.at,q.auto_scheduled,c.title,c.lang,c.relative_path,c.description,c.hashtags
                                FROM queue q JOIN clips c ON c.id=q.clip_id JOIN accounts a ON a.id=q.account_id
                                WHERE q.status='scheduled' AND q.external_id IS NULL AND a.platform='YouTube'
                                AND a.lang='RU' AND (q.retry_at IS NULL OR q.retry_at<=?)
                                AND NOT EXISTS(SELECT 1 FROM queue older WHERE older.account_id=q.account_id
                                    AND older.status='scheduled' AND older.external_id IS NULL
                                    AND (older.at<q.at OR (older.at=q.at AND older.id<q.id)))
                                ORDER BY q.at,q.id LIMIT 1""", (iso(started),)).fetchone()
            if not job:
                return
            db.execute("UPDATE queue SET status='checking' WHERE id=?", (job["id"],))
        account = self.get_account(job["account_id"])
        try:
            transport.require_youtube_write_route(account["route"])
            path = (self.output / job["relative_path"]).resolve()
            if not path.is_relative_to(self.output) or not path.is_file() or not account["refresh_token"]:
                raise ValueError("Клип или OAuth-подключение канала недоступны.")
            if not self.shorts_valid(path):
                raise ValueError("Для MVP нужен вертикальный MP4 длительностью 25–50 секунд.")
            token = transport.prepare_upload(account)
            limit = self.schedule_limit()
            entries = youtube_release.scheduled_entries(account["route"], token, account["target"], limit)
            with self.db() as db:
                confirmed = db.execute("""SELECT external_id,at,status FROM queue
                    WHERE account_id=? AND external_id IS NOT NULL
                    AND status IN ('preloaded','remote_check','uploaded')""", (account["id"],)).fetchall()
            entries |= {row["external_id"]: row["at"] for row in confirmed
                        if row["external_id"] not in entries and row["at"] > iso(started)
                        and row["status"] != "uploaded"}
            if len(entries) >= limit:
                if self.reassign_overflow(job, account["id"], started):
                    return
                delay = min(300, max(10, int((min(datetime.fromisoformat(v) for v in entries.values()) - now()).total_seconds())))
                self.slot_wait(account["id"], delay, "Запланированные слоты YouTube заняты.", job_id=job["id"])
                return
            reserved = ({row["external_id"]: row["at"] for row in confirmed} | entries).values()
            planned = self.planned_time(job, reserved, now())
            start, end = schedule.civil_day(started, job["lang"])
            planned_day = planned.astimezone(schedule.ZONES[job["lang"]]).date()
            daily_used = sum(start <= datetime.fromisoformat(date) < end for date in reserved)
            if (planned >= end and
                    daily_used >= schedule.daily_limit(job["lang"]) and
                    self.reassign_overflow(job, account["id"], started, before_day=planned_day)):
                return
        except youtube_release.SlotUnavailable as error:
            self.slot_wait(account["id"], error.retry_after_seconds, str(error), project_wide=error.project_wide,
                           job_id=job["id"])
            return
        except (transport.RouteProblem, transport.NetworkProblem) as error:
            self.hold_for_youtube_route(account, job["id"], error)
            return
        except (transport.ConnectionProblem, ValueError, OSError, subprocess.SubprocessError, KeyError) as error:
            self.upload_status(job["id"], "blocked", str(error)[:350])
            return
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh = db.execute("SELECT * FROM accounts WHERE id=?", (account["id"],)).fetchone()
            paused = db.execute("SELECT value FROM settings WHERE name='paused'").fetchone()[0]
            if fresh is None or dict(fresh) != account:
                db.execute("UPDATE queue SET status='review',detail=? WHERE id=?",
                           ("Во время проверки настройки изменились.", job["id"]))
                return
            if paused or self.youtube_throttled(db, now()):
                db.execute("UPDATE queue SET status='scheduled' WHERE id=?", (job["id"],))
                return
            latest = db.execute("""SELECT state,detail FROM route_health WHERE route=? AND platform=?
                AND checked_at>=?""", (account["route"], account["platform"], iso(started))).fetchone()
            if latest and latest["state"] != "ok":
                db.execute("UPDATE queue SET status='route_wait',detail=? WHERE id=?", (latest["detail"], job["id"]))
                return
            db.execute("UPDATE queue SET status='sending',sent_at=? WHERE id=?", (iso(now()), job["id"]))
        description, tags = clip_metadata(job["lang"], job["title"])
        try:
            external = youtube_release.upload(
                account["route"], token, title=job["title"], description=job["description"] or description,
                tags=json.loads(job["hashtags"]) or tags, lang=job["lang"], path=path, publish_at=planned,
                license=os.environ.get("STREAMCUT_YOUTUBE_LICENSE", "youtube"), made_for_kids=False,
                expected_channel=account["target"],
            )
        except youtube_release.SlotUnavailable as error:
            self.slot_wait(account["id"], error.retry_after_seconds, str(error), project_wide=error.project_wide,
                           job_id=job["id"])
            return
        except (youtube_release.UploadRejected, ValueError) as error:
            self.upload_status(job["id"], "blocked", str(error)[:350])
            return
        except transport.RouteProblem as error:
            self.hold_for_youtube_route(account, job["id"], error)
            return
        except (transport.ConnectionProblem, OSError):
            self.upload_status(job["id"], "unknown", "Проверьте YouTube Studio перед повтором.")
            return
        self.upload_status(job["id"], "remote_check", "YouTube принял файл. Проверяем дату публикации.",
                           external=external, at=iso(planned))
        try:
            video = youtube_release.publication_status(account["route"], token, [external]).get(external)
        except youtube_release.SlotUnavailable as error:
            if error.project_wide:
                self.slot_wait(account["id"], error.retry_after_seconds, str(error), project_wide=True)
            return
        except transport.ConnectionProblem:
            return
        self.apply_youtube_status(dict(id=job["id"],status="remote_check"), video, now())

    def hold_for_youtube_route(self, account, identifier, error):
        if not transport.youtube_write_route_allowed(account["route"]):
            self.upload_status(identifier, "route_wait", transport.GATEWAY_REQUIRED_DETAIL)
            return
        self.record_connection(dict(route=account["route"], platform=account["platform"], state="failed",
                                    country=None, detail=str(error), checked_at=iso(now())), identifier)

    @staticmethod
    def promote_automatic_youtube(db, allowed, account_id=None):
        pending = db.execute("""SELECT q.id,s.twitch_broadcaster
            FROM queue q JOIN clips c ON c.id=q.clip_id JOIN accounts a ON a.id=q.account_id
            LEFT JOIN sources s ON s.id=c.source_id
            WHERE a.auto=1 AND a.platform='YouTube' AND a.lang='RU' AND a.refresh_token IS NOT NULL
            AND q.account_id=COALESCE(?,q.account_id) AND q.status='review'
            AND q.external_id IS NULL AND c.verified=1""", (account_id,)).fetchall()
        db.executemany("""UPDATE queue SET status='scheduled',detail='',retry_at=NULL,auto_scheduled=1
            WHERE id=?""", [(job["id"],) for job in pending
                            if allowed is None or job["twitch_broadcaster"] in allowed])

    @staticmethod
    def hold_unapproved_youtube_uploads(db, allowed):
        # NULL applies the new source check to legacy jobs with no record of manual approval.
        pending = db.execute("""SELECT q.id,s.twitch_broadcaster FROM queue q
            JOIN clips c ON c.id=q.clip_id JOIN accounts a ON a.id=q.account_id
            LEFT JOIN sources s ON s.id=c.source_id
            WHERE a.platform='YouTube' AND a.lang='RU' AND q.status IN ('scheduled','route_wait')
            AND q.external_id IS NULL AND q.auto_scheduled IS NOT 0""").fetchall()
        db.executemany("""UPDATE queue SET status='review',retry_at=NULL,
            detail='Для автоотправки нужен разрешённый источник Twitch.' WHERE id=?""",
            [(job["id"],) for job in pending if job["twitch_broadcaster"] not in allowed])

    def loop(self, operation, interval):
        while not self.stop.is_set():
            try:
                operation()
            except Exception:
                logging.exception("Фоновое задание остановлено ошибкой.")
            self.stop.wait(interval)

    def start_oauth(self, identifier, origin):
        account = self.get_account(identifier)
        if account["platform"] != "YouTube" or not transport.youtube_configured():
            raise ValueError("На сервере ещё не настроен OAuth YouTube.")
        transport.check_route(account["route"], "YouTube")
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
        self.pending_auth = {key: val for key, val in self.pending_auth.items() if val[0] > time.monotonic()}
        redirect = f"{origin}/oauth/youtube"
        self.pending_auth[state] = (time.monotonic() + 600, identifier, account["target"], account["route"],
                                    verifier, redirect)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = urlencode({"client_id": os.environ["STREAMCUT_GOOGLE_CLIENT_ID"], "redirect_uri": redirect,
                            "response_type": "code", "scope": transport.YOUTUBE_SCOPES, "state": state,
                            "access_type": "offline", "prompt": "consent select_account",
                            "code_challenge": challenge, "code_challenge_method": "S256"})
        return "https://accounts.google.com/o/oauth2/v2/auth?" + params

    def finish_oauth(self, params):
        state = params.get("state", [""])[0]
        code = params.get("code", [""])[0]
        stored = self.pending_auth.pop(state, None)
        if not code or not stored or time.monotonic() >= stored[0]:
            raise ValueError("Сессия подключения истекла или доступ отменён.")
        _, identifier, target, route, verifier, redirect = stored
        account = self.get_account(identifier)
        if (account["target"], account["route"]) != (target, route):
            raise ValueError("Настройки канала изменились. Начните подключение снова.")
        transport.check_route(route, "YouTube")
        tokens = transport.token_request(route, grant_type="authorization_code", code=code,
                                         code_verifier=verifier, redirect_uri=redirect)
        transport.confirm_channel(route, tokens["access_token"], target)
        token = tokens.get("refresh_token")
        if not token:
            raise ValueError("Google не выдал доступ для фоновой работы.")
        with self.db() as db:
            cursor = db.execute("""UPDATE accounts SET refresh_token=?,auto=0
                 WHERE id=? AND target=? AND route=?""", (token, identifier, target, route))
            if not cursor.rowcount:
                raise ValueError("Настройки канала изменились. Начните подключение снова.")
            db.execute("DELETE FROM account_metrics WHERE account_id=?", (identifier,))


class Handler(SimpleHTTPRequestHandler):
    MAX_BODY = 16 * 1024
    MAX_SOURCE_BODY = 4 * 1024 * 1024

    def log_message(self, *_):
        # OAuth callback query parameters contain authorization codes.
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        super().end_headers()

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def is_local(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_HEAD(self):
        self.send_error(405)

    def do_GET(self):
        if not self.is_local():
            self.send_error(403)
            return
        url = urlsplit(self.path)
        if url.path == "/healthz":
            self.send_json({"ok": True})
            return
        if url.path == "/api/state":
            self.send_json(self.server.studio.snapshot())
            return
        if url.path == "/api/source-edit":
            try:
                identifier = int(parse_qs(url.query, max_num_fields=1).get("id", ["0"])[0])
                self.send_json(self.server.studio.source_edit(identifier))
            except ValueError as error:
                self.send_json({"error": str(error)}, 400)
            return
        if url.path == "/oauth/youtube":
            try:
                self.server.studio.finish_oauth(parse_qs(url.query))
            except (ValueError, transport.ConnectionProblem) as error:
                self.send_response(303)
                self.send_header("Location", "/?oauth=" + urlencode({"": str(error)})[1:] + "#accounts")
                self.end_headers()
                return
            self.send_response(303)
            self.send_header("Location", "/?oauth=ok#accounts")
            self.end_headers()
            return
        if match := re.fullmatch(r"/media/([0-9]+)", url.path):
            self.send_media(int(match[1]))
            return
        if url.path == "/":
            self.path = "/index.html"
        elif not re.fullmatch(r"/(?:index\.html|app\.js|core\.mjs|styles\.css|assets/[A-Za-z0-9_.-]+)", url.path):
            self.send_error(404)
            return
        super().do_GET()

    def send_media(self, clip_id):
        studio = self.server.studio
        with studio.db() as db:
            row = db.execute("SELECT relative_path FROM clips WHERE id=?", (clip_id,)).fetchone()
        if not row:
            self.send_error(404)
            return
        path = (studio.output / row["relative_path"]).resolve()
        if not path.is_relative_to(studio.output) or not path.is_file():
            self.send_error(404)
            return
        with path.open("rb") as stream:
            size = path.stat().st_size
            start, end, partial = 0, size - 1, False
            if header := self.headers.get("Range"):
                match = re.fullmatch(r"bytes=([0-9]{1,16})-([0-9]{0,16})", header)
                if not match or int(match[1]) >= size or match[2] and int(match[2]) < int(match[1]):
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
                partial = True
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            stream.seek(start)
            remaining = end - start + 1
            try:
                while remaining:
                    data = stream.read(min(remaining, 256 * 1024))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
            except (BrokenPipeError, ConnectionResetError):
                return

    def do_POST(self):
        origin = f"http://127.0.0.1:{self.server.server_port}"
        if not self.is_local() or self.headers.get("Origin") != origin:
            self.send_json({"error": "Откройте панель по локальному адресу сервиса."}, 403)
            return
        studio = self.server.studio
        try:
            size = int(self.headers.get("Content-Length", "0"))
            url = urlsplit(self.path)
            if url.path == "/api/inbox":
                if len(url.query) > 8000 or self.headers.get_content_type() != "application/octet-stream":
                    raise ValueError("Некорректная загрузка.")
                fields = {key: values[0] for key, values in
                          parse_qs(url.query, max_num_fields=8).items()}
                for key in ("mark", "id"):
                    if key in fields:
                        fields[key] = int(fields[key])
                self.connection.settimeout(45)
                self.send_json(studio.receive_video(self.rfile, size, fields), 201)
                return
            limit = self.MAX_SOURCE_BODY if url.path == "/api/sources" else self.MAX_BODY
            if not 0 < size <= limit or self.headers.get_content_type() != "application/json":
                raise ValueError("Некорректное тело запроса.")
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError("Некорректное тело запроса.")
            result = {}
            if self.path == "/api/sources":
                result = {"id": studio.add_source(payload, return_id=True)}
            elif self.path == "/api/sources/dismiss":
                studio.dismiss_source(payload.get("id"))
            elif self.path == "/api/accounts":
                studio.add_account(payload)
            elif self.path == "/api/check":
                result = studio.check_connection(text_field(payload, "route", 10), text_field(payload, "platform", 12))
            elif self.path == "/api/oauth":
                result = {"url": studio.start_oauth(payload.get("id"), origin)}
            elif self.path == "/api/auto":
                studio.auto_account(payload.get("id"), payload.get("enabled"))
            elif self.path == "/api/settings":
                studio.change_setting(payload.get("name"), payload.get("enabled"))
            elif self.path == "/api/queue":
                studio.queue_action(payload)
            else:
                self.send_json({"error": "Действие не найдено."}, 404)
                return
            self.send_json(result)
        except (ValueError, transport.ConnectionProblem) as error:
            self.send_json({"error": str(error)}, 400)
        except (OSError, sqlite3.Error, TypeError):
            logging.exception("Сбой обработки запроса.")
            self.send_json({"error": "Ошибка сервиса. Проверьте журнал."}, 500)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", type=Path, default=Path.home() / ".streamcut")
    args = parser.parse_args()
    os.umask(0o077)

    def terminate(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    studio = Studio(args.data)
    try:
        with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
            server.studio = studio
            server.RequestHandlerClass.extensions_map = {**Handler.extensions_map, ".mjs": "text/javascript"}

            def handler(*args, **kwargs):
                return Handler(*args, directory=str(HERE / "dist"), **kwargs)

            server.RequestHandlerClass = handler

            Thread(target=studio.loop, args=(studio.scan, 10), daemon=True).start()
            Thread(target=studio.loop, args=(studio.edit_one, 4), daemon=True).start()
            Thread(target=studio.loop, args=(studio.publish_one, 8), daemon=True).start()
            Thread(target=studio.loop, args=(studio.sync_youtube, 180), daemon=True).start()
            Thread(target=studio.loop, args=(studio.search, 60), daemon=True).start()
            Thread(target=studio.loop, args=(studio.fetch_twitch_media, 60), daemon=True).start()
            Thread(target=studio.loop, args=(studio.monitor_connections, 60), daemon=True).start()
            Thread(target=studio.loop, args=(studio.refresh_metrics, 60), daemon=True).start()
            print(f"Streamcut: http://127.0.0.1:{server.server_port}/", flush=True)
            print(f"Входящие файлы: {studio.input}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        studio.stop.set()
        studio.close()


if __name__ == "__main__":
    main()
