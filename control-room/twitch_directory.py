"""Follow the live Grand Theft Auto VI directory and the selected channels' archives."""

import os

import discovery
import regional


TOP_CHANNELS = 10
ARCHIVES_PER_CHANNEL = 5
CATEGORY = "Grand Theft Auto VI"


def category_id(headers):
    games = discovery.helix("games", [("name", CATEGORY)], headers, "Категория GTA VI")
    for game in games:
        identifier = game.get("id", "")
        if game.get("name", "").casefold() == CATEGORY.casefold() and discovery.TWITCH_ID.fullmatch(identifier):
            return identifier
    raise discovery.ConnectionProblem("Категория Grand Theft Auto VI не найдена в Twitch.")


def market_streams(lang, code, game_id, headers, current):
    rows = discovery.helix("streams", [("game_id", game_id), ("language", code),
                                      ("first", TOP_CHANNELS * 2)], headers, "Топ каналов Twitch")
    candidates = []
    users = set()
    for row in sorted(rows, key=lambda item: regional.count(item.get("viewer_count")), reverse=True):
        if row.get("language") != code or row.get("game_id") != game_id:
            continue
        login = row.get("user_login", "").lower()
        item = regional.twitch_item(row, lang, login, current, live=True, category=True)
        if not item or item["twitch_user"] in users:
            continue
        users.add(item["twitch_user"])
        candidates.append(dict(item, stream_id=row["id"], game_id=game_id, directory_rank=len(candidates) + 1,
                               viewer_count=regional.count(row.get("viewer_count"))))
        if len(candidates) == TOP_CHANNELS:
            break
    return candidates


def channel_archives(stream, headers, current, known_live):
    user = stream["twitch_user"]
    rows = discovery.helix("videos", [("user_id", user), ("type", "archive"),
                                     ("sort", "time"), ("first", ARCHIVES_PER_CHANNEL)],
                           headers, "Записи выбранного канала Twitch")
    lang = stream["lang"]
    broadcasts = known_live | {(lang, stream["stream_id"])}
    for row in rows:
        if row.get("user_id") != user or row.get("viewable") != "public":
            continue
        item = regional.twitch_item(row, lang, stream["twitch_login"], current, live=False,
                                    known_live=broadcasts)
        if item:
            yield dict(item, stream_id=row.get("stream_id"), archive_id=row["id"], game_id=stream["game_id"],
                       directory_rank=stream["directory_rank"], viewer_count=stream["viewer_count"])


def search(languages, current, *, known_live=()):
    """Return ranked streams plus recent public GTA VI archives of those channels."""
    client = os.environ.get("TWITCH_CLIENT_ID")
    secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client or not secret:
        return None
    headers = discovery.twitch_headers(client, secret)
    game_id = category_id(headers)
    known_live = set(known_live)
    results = {}
    for lang, code in languages.items():
        results[lang] = []
        for stream in market_streams(lang, code, game_id, headers, current):
            results[lang].append(stream)
            results[lang].extend(channel_archives(stream, headers, current, known_live))
    return results
