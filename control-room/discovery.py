"""Discover metadata from approved YouTube Data and Twitch Helix APIs."""

from dataclasses import dataclass
from datetime import timedelta
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
