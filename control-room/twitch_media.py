"""Fetch an already published Twitch clip for a broadcaster or authorized editor."""

from datetime import datetime, timezone
from http.client import HTTPSConnection, HTTPException
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from time import monotonic
from urllib.parse import urlsplit

import discovery
import regional


TOKEN_KEY = "STREAMCUT_TWITCH_CLIP_TOKEN_"
EDITOR_KEY = "STREAMCUT_TWITCH_CLIP_EDITOR_ID_"
CLIP_ID = re.compile(r"[a-zA-Z0-9_-]{1,160}")
CLIP_HOST = re.compile(r"(?:[a-z0-9-]+\.)*assets\.clips\.twitchcdn\.net|clips-media-assets\d*\.twitch\.tv")
MAX_BYTES = 256 * 1024 * 1024
CHUNK = 1024 * 1024
DOWNLOAD_SECONDS = 180


def configured(candidate=None):
    if not os.environ.get("TWITCH_CLIENT_ID"):
        return False
    if candidate is None:
        return any(name.startswith(TOKEN_KEY) and discovery.TWITCH_ID.fullmatch(name[len(TOKEN_KEY):])
                   and value.strip() for name, value in os.environ.items())
    broadcaster = candidate.get("twitch_user", "")
    return bool(candidate.get("kind") == "vod" and isinstance(broadcaster, str)
                and discovery.TWITCH_ID.fullmatch(broadcaster) and os.environ.get(TOKEN_KEY + broadcaster, "").strip())


def origin(candidate):
    video_url = candidate.get("url", "")
    match = re.fullmatch(r"https://www\.twitch\.tv/videos/([0-9]{1,30})", video_url) if isinstance(video_url, str) else None
    if not match or candidate.get("archive_id", match[1]) != match[1]:
        raise ValueError("Нужен подтверждённый ID записи Twitch.")
    return match[1]


def clip_options(candidate, broadcaster, vod, current, headers):
    earliest = current - discovery.WINDOW
    published = regional.timestamp(candidate.get("observed"))
    after = max(earliest, published) if published and published <= current else earliest
    params = [("broadcaster_id", broadcaster), ("first", 100),
              ("started_at", after.isoformat(timespec="seconds").replace("+00:00", "Z"))]
    clips = discovery.helix("clips", params, headers, "Список клипов Twitch")
    expected_game = candidate.get("game_id")
    expected_lang = {"RU": "ru", "EN": "en", "ESP": "es"}.get(candidate.get("lang"))
    ranked = {}
    for clip in clips:
        verified_time = regional.timestamp(clip.get("created_at"))
        duration = clip.get("duration")
        clip_id = clip.get("id", "")
        if not isinstance(clip_id, str) or not CLIP_ID.fullmatch(clip_id):
            continue
        if (clip.get("video_id") != vod or clip.get("broadcaster_id") != broadcaster
                or not verified_time or not after <= verified_time <= current
                or type(duration) not in (int, float) or not 25 <= duration <= 60
                or clip.get("viewable", "public") != "public" or clip.get("is_public") is False
                or expected_game and clip.get("game_id") != expected_game
                or expected_lang and clip.get("language") != expected_lang):
            continue
        ranked[clip_id] = (regional.count(clip.get("view_count")), verified_time)
    return sorted(ranked, key=ranked.get, reverse=True)[:5]


def download_location(raw):
    if not isinstance(raw, str) or len(raw) > 4096:
        raise discovery.ConnectionProblem("Twitch вернул неправильную ссылку на файл.")
    try:
        url = urlsplit(raw)
        if (url.scheme != "https" or not CLIP_HOST.fullmatch(url.hostname or "")
                or url.port is not None or url.username is not None or url.password is not None
                or not url.path.startswith("/") or url.fragment):
            raise ValueError
    except ValueError:
        raise discovery.ConnectionProblem("Twitch вернул адрес за пределами CDN клипов.") from None
    return url.hostname, url.path + ("?" + url.query if url.query else "")


def download(raw_url, destination):
    host, request_path = download_location(raw_url)
    deadline = monotonic() + DOWNLOAD_SECONDS
    connection = HTTPSConnection(host, timeout=20)
    try:
        connection.request("GET", request_path)
        with connection.getresponse() as response:
            if response.status != 200:
                raise discovery.ConnectionProblem(f"CDN Twitch не выдал файл (HTTP {response.status}).")
            length = response.getheader("Content-Length")
            expected_bytes = None if length is None else int(length)
            if expected_bytes is not None and not 12 <= expected_bytes <= MAX_BYTES:
                raise discovery.ConnectionProblem("Файл Twitch превысил допустимый размер или пуст.")
            with NamedTemporaryFile(dir=destination.parent, prefix=".clip-download-", suffix=".part") as output:
                chunk = response.read(12)
                if len(chunk) < 12 or chunk[4:8] != b"ftyp":
                    raise discovery.ConnectionProblem("CDN Twitch не вернул MP4.")
                written = len(chunk)
                output.write(chunk)
                while chunk := response.read(CHUNK):
                    written += len(chunk)
                    if written > MAX_BYTES or monotonic() >= deadline:
                        raise discovery.ConnectionProblem("Истёк лимит времени или размера файла Twitch.")
                    output.write(chunk)
                if expected_bytes is not None and written != expected_bytes:
                    raise discovery.ConnectionProblem("CDN Twitch передал только часть файла.")
                output.flush()
                os.fsync(output.fileno())
                os.link(output.name, destination)
    except FileExistsError:
        if destination.is_file() and not destination.is_symlink():
            return destination
        raise
    except (HTTPException, OSError, ValueError):
        raise discovery.ConnectionProblem("Не удалось сохранить исходник Twitch.") from None
    finally:
        connection.close()
    return destination


def fetch(candidate, destination, *, current=None):
    """Save an approved landscape clip under an unused local MP4 filename."""
    if not configured(candidate):
        return None
    broadcaster = candidate["twitch_user"]
    vod = origin(candidate)
    destination = Path(destination)
    if destination.suffix.lower() != ".mp4":
        raise ValueError("Имя файла Twitch должно оканчиваться на .mp4.")
    if destination.exists() or destination.is_symlink():
        return destination if destination.is_file() and not destination.is_symlink() else None
    editor = os.environ.get(EDITOR_KEY + broadcaster, broadcaster)
    if not discovery.TWITCH_ID.fullmatch(editor):
        raise ValueError("Неверный ID редактора Twitch.")
    headers = {"Client-Id": os.environ["TWITCH_CLIENT_ID"],
               "Authorization": "Bearer " + os.environ[TOKEN_KEY + broadcaster].strip()}
    ids = clip_options(candidate, broadcaster, vod, current or datetime.now(timezone.utc), headers)
    if not ids:
        return None
    links = discovery.helix("clips/downloads", [("broadcaster_id", broadcaster), ("editor_id", editor),
                            *(("clip_id", value) for value in ids)], headers, "Разрешение на скачивание Twitch")
    landscape = {link.get("clip_id"): link.get("landscape_download_url") for link in links}
    for clip_id in ids:
        if raw_url := landscape.get(clip_id):
            return download(raw_url, destination)
    return None
