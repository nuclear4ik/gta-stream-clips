"""Discover metadata from approved YouTube Data and Twitch Helix APIs."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
import os
import re
from unicodedata import normalize
from urllib.parse import urlencode

from transport import ConnectionProblem, json_response, request

NAME_SEPARATOR = r"\s._\-‐‑‒–—"
TOPIC = re.compile(rf"(?<!\w)(?:gta|гта|grand[{NAME_SEPARATOR}]+theft[{NAME_SEPARATOR}]+auto)"
                   rf"[{NAME_SEPARATOR}:]*(?:vi|6)(?!\w)")
TITLE_SUFFIX = re.compile(
    r"(?:^|\s*[|•·]\s*|\s+[-–—]\s+)"
    r"(?:(?:tags|keywords|теги|ключевые слова|etiquetas|palabras clave)\s*:|"
    r"(?:follow|subscribe|shoutout|подписывайтесь|подпишитесь|sígueme|suscríbete)\b|"
    r"(?:#\w+\s*)+$)"
)
YOUTUBE_ID = re.compile(r"[a-zA-Z0-9_-]{11}")
TWITCH_LOGIN = re.compile(r"[a-zA-Z0-9_]{3,25}")
TWITCH_ID = re.compile(r"[0-9]{1,30}")
REGION = {"RU": "RU", "EN": "US", "ESP": "ES"}
WINDOW = timedelta(days=3)
@dataclass(frozen=True)
class Creator:
    label: str
    twitch: str
    aliases: tuple[str, ...] = ()

    @property
    def names(self):
        return (self.label, *self.aliases)


WATCHLIST = {
    "RU": (
        Creator("Бустер", "buster", ("Buster",)),
        Creator("Bratishkinoff", "bratishkinoff", ("Братишкин",)),
        Creator("Recrent", "recrent", ("Рекрент",)),
        Creator("Evelone192", "evelone192", ("Эвелон",)),
        Creator("Kussia88", "kussia88", ("Куся",)),
        Creator("Stray228", "stray228", ("Стрей",)),
        Creator("StRoGo", "strogo", ("Строго",)),
        Creator("DreadzTV", "dreadztv", ("Дред",)),
        Creator("Dyrachyo", "dyrachyo", ("Дурачьё",)),
        Creator("WELOVEGAMES", "welovegames"),
    ),
    "EN": (
        Creator("Kai Cenat", "kaicenat", ("KaiCenat",)),
        Creator("CaseOh", "caseoh_", ("CaseOh_",)),
        Creator("LIRIK", "lirik"),
        Creator("Jynxzi", "jynxzi"),
        Creator("Tarik", "tarik"),
        Creator("Summit1g", "summit1g"),
        Creator("TimTheTatman", "timthetatman", ("Tim The Tatman",)),
        Creator("NICKMERCS", "nickmercs"),
        Creator("Ludwig", "ludwig", ("Ludwig Ahgren",)),
        Creator("Ninja", "ninja", ("Tyler Blevins",)),
    ),
    "ESP": (
        Creator("Ibai", "ibai", ("Ibai Llanos",)),
        Creator("AuronPlay", "auronplay", ("Auron Play",)),
        Creator("IlloJuan", "illojuan", ("Illo Juan",)),
        Creator("Rubius", "rubius", ("El Rubius",)),
        Creator("TheGrefg", "thegrefg", ("The Grefg",)),
        Creator("ElXokas", "elxokas", ("El Xokas",)),
        Creator("DjMaRiiO", "djmariio", ("DJ Mario",)),
        Creator("Cristinini", "cristinini"),
        Creator("Alexelcapo", "alexelcapo"),
        Creator("Knekro", "knekro"),
    ),
}


def unique(items):
    """Preserve the first spelling of each configured name."""
    seen = set()
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            yield item


def youtube_authors(lang):
    configured = os.environ.get(f"STREAMCUT_YOUTUBE_{lang}")
    if configured is None:
        return [creator.names for creator in WATCHLIST.get(lang, ())]
    authors = []
    for names in configured.split("|"):
        aliases = (alias.strip() for alias in names.split(","))
        aliases = tuple(unique(alias for alias in aliases if 0 < len(alias) <= 70))[:4]
        if aliases:
            authors.append(aliases)
    return authors[:12]


def twitch_authors(lang):
    configured = os.environ.get(f"STREAMCUT_TWITCH_{lang}")
    if configured is None:
        return [creator.twitch for creator in WATCHLIST.get(lang, ())]
    terms = (item.strip().lower().lstrip("@") for item in configured.split("|"))
    return list(unique(term for term in terms if TWITCH_ID.fullmatch(term) or TWITCH_LOGIN.fullmatch(term)))[:12]


def roster():
    """Names the owner can expect to see in discovery, with overrides applied."""
    result = {}
    for lang, seeds in WATCHLIST.items():
        twitch_labels = {creator.twitch: creator.label for creator in seeds}
        youtube_labels = [names[0] for names in youtube_authors(lang)]
        twitch_labels = [twitch_labels.get(login, login) for login in twitch_authors(lang)]
        result[lang] = list(unique([*youtube_labels, *twitch_labels]))
    return result


def configured():
    return bool(os.environ.get("STREAMCUT_YOUTUBE_KEY") and any(youtube_authors(lang) for lang in WATCHLIST) or
                os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET") and
                any(twitch_authors(lang) for lang in WATCHLIST))


def matching_text(text):
    return normalize("NFKC", text).casefold().strip()


def subject(title):
    return TITLE_SUFFIX.split(matching_text(title), maxsplit=1)[0]


def creator_pattern(names):
    alternatives = []
    for name in names:
        # Allow separators inside a name while preserving literal handle suffixes.
        tokens = re.split(rf"(?<=\w)[{NAME_SEPARATOR}]+(?=[^\W_])", matching_text(name))
        alternatives.append(rf"[{NAME_SEPARATOR}]+".join(map(re.escape, tokens)))
    return re.compile(r"(?<!\w)(?:" + "|".join(alternatives) + r")(?!\w)")


def youtube(lang, code):
    key = os.environ.get("STREAMCUT_YOUTUBE_KEY")
    if not key or lang not in REGION:
        return
    after = (datetime.now(timezone.utc) - WINDOW).isoformat(timespec="seconds").replace("+00:00", "Z")
    seen = set()
    for names in youtube_authors(lang):
        # Quota is per search call. Search spelling variants in one query per creator.
        game_names = ("GTA 6", "GTA VI", "GTA6", "Grand Theft Auto VI")
        if lang == "RU":
            game_names += ("ГТА 6", "ГТА VI")
        terms = "|".join(f"{game} {name}" for name in names for game in game_names)
        query = urlencode({"part": "snippet", "q": terms, "type": "video", "maxResults": 25,
                           "order": "date", "regionCode": REGION[lang], "relevanceLanguage": code,
                           "publishedAfter": after})
        status, _, raw = request("server", "www.googleapis.com", "GET", f"/youtube/v3/search?{query}",
                                 {"X-Goog-Api-Key": key})
        data = json_response(status, raw, "Поиск YouTube")
        alias = creator_pattern(names)
        for item in data.get("items", []):
            snippet = item.get("snippet", {})
            title, uploader = unescape(snippet.get("title", "")), unescape(snippet.get("channelTitle", ""))
            video = item.get("id", {}).get("videoId", "")
            broadcast = snippet.get("liveBroadcastContent", "none")
            title_subject = subject(title)
            if (video in seen or not YOUTUBE_ID.fullmatch(video) or broadcast == "upcoming" or
                    not TOPIC.search(title_subject) or
                    not (alias.search(title_subject) or alias.search(matching_text(uploader)))):
                continue
            seen.add(video)
            yield {"lang": lang, "title": title[:140], "url": f"https://www.youtube.com/watch?v={video}",
                   "kind": "live" if broadcast == "live" else "video"}


def twitch_headers(client, secret):
    body = urlencode({"client_id": client, "client_secret": secret, "grant_type": "client_credentials"}).encode()
    status, _, raw = request("server", "id.twitch.tv", "POST", "/oauth2/token",
                             {"Content-Type": "application/x-www-form-urlencoded"}, body)
    token = json_response(status, raw, "Авторизация Twitch").get("access_token")
    if not token:
        raise ConnectionProblem("Twitch не выдал токен.")
    return {"Client-Id": client, "Authorization": f"Bearer {token}"}


def helix(endpoint, params, headers, description):
    status, _, raw = request("server", "api.twitch.tv", "GET",
                             f"/helix/{endpoint}?{urlencode(params)}", headers)
    return json_response(status, raw, description).get("data", [])


def twitch_channels(languages, followers, headers):
    requested = list(dict.fromkeys(term for terms in followers.values() for term in terms))
    if not requested:
        return {}
    lookups = [("id" if TWITCH_ID.fullmatch(term) else "login", term) for term in requested]
    channels = {}
    for account in helix("users", lookups, headers, "Каналы Twitch"):
        user_id, login = account.get("id", ""), account.get("login", "").lower()
        if not TWITCH_ID.fullmatch(user_id) or not TWITCH_LOGIN.fullmatch(login):
            continue
        markets = [(lang, languages[lang]) for lang, terms in followers.items() if user_id in terms or login in terms]
        if markets:
            channels[user_id] = (login, markets)
    return channels


def twitch_live(channels, headers):
    seen = set()
    filters = [("user_id", user_id) for user_id in channels]
    for item in helix("streams", filters, headers, "Прямые эфиры Twitch"):
        user_id = item.get("user_id")
        channel = channels.get(user_id)
        stream = item.get("id", "")
        title = item.get("title", "")
        if channel is None or stream in seen or not TWITCH_ID.fullmatch(stream) or item.get("type") != "live":
            continue
        game = item.get("game_name", "")
        category = matching_text(game)
        by_title = TOPIC.search(subject(title))
        by_category = TOPIC.fullmatch(category)
        if not by_category and not (category in ("", "just chatting") and by_title):
            continue
        login, markets = channel
        for lang, code in markets:
            if item.get("language") == code:
                seen.add(stream)
                yield {"lang": lang, "title": (title or game)[:140],
                       "url": f"https://www.twitch.tv/{login}", "kind": "live",
                       "key": f"twitch:stream:{stream}"}


def twitch_vods(channels, headers, known_live=()):
    after = datetime.now(timezone.utc) - WINDOW
    seen_videos, seen_streams = set(), set()
    for user_id, (_, markets) in channels.items():
        params = [("user_id", user_id), ("type", "archive"), ("first", "5"), ("sort", "time")]
        for item in helix("videos", params, headers, "Записи Twitch"):
            title, video = item.get("title", ""), item.get("id", "")
            stream = item.get("stream_id") or ""
            if (video in seen_videos or stream in seen_streams or not TWITCH_ID.fullmatch(video) or
                    item.get("user_id", user_id) != user_id or item.get("type", "archive") != "archive"):
                continue
            try:
                created = datetime.fromisoformat(item.get("created_at", ""))
            except ValueError:
                continue
            if created.tzinfo is None or created < after or item.get("viewable", "public") != "public":
                continue
            for lang, code in markets:
                if item.get("language") != code:
                    continue
                if not TOPIC.search(subject(title)):
                    if (lang, stream) not in known_live:
                        continue
                candidate = {"lang": lang, "title": title[:140], "url": f"https://www.twitch.tv/videos/{video}",
                             "kind": "vod"}
                if TWITCH_ID.fullmatch(stream):
                    candidate["key"] = f"twitch:stream:{stream}"
                    seen_streams.add(stream)
                seen_videos.add(video)
                yield candidate


def twitch(languages, live_only=False, known_live=()):
    client, secret = os.environ.get("TWITCH_CLIENT_ID"), os.environ.get("TWITCH_CLIENT_SECRET")
    followers = {lang: twitch_authors(lang) for lang in languages}
    if not client or not secret or not any(followers.values()):
        return
    headers = twitch_headers(client, secret)
    channels = twitch_channels(languages, followers, headers)
    if channels and live_only:
        yield from twitch_live(channels, headers)
    elif channels:
        yield from twitch_vods(channels, headers, known_live)
