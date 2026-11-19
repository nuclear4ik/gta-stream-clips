"""Follow the live Grand Theft Auto VI directory and the selected channels' archives."""

from datetime import timedelta
import os

import discovery
import regional


TOP_CHANNELS = 10
ARCHIVES_PER_CHANNEL = 5
CATEGORY = "Grand Theft Auto VI"
FOLLOWUP_WINDOW = timedelta(hours=24)


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


def archive_rows(user, headers):
    return discovery.helix("videos", [("user_id", user), ("type", "archive"),
                                      ("sort", "time"), ("first", ARCHIVES_PER_CHANNEL)],
                            headers, "Записи выбранного канала Twitch")


def optional_archive_rows(user, archives, headers):
    if user not in archives:
        try:
            archives[user] = archive_rows(user, headers)
        except discovery.ConnectionProblem:
            archives[user] = []
    return archives[user]


def channel_archives(channel, rows, current, known_live, *, exact=False):
    user, lang = channel["twitch_user"], channel["lang"]
    stream_id = channel.get("stream_id")
    broadcasts = known_live | {(lang, stream_id)} if stream_id else known_live
    for row in rows:
        if row.get("user_id") != user or row.get("viewable") != "public":
            continue
        if exact and (not stream_id or row.get("stream_id") != stream_id):
            continue
        item = regional.twitch_item(row, lang, channel["twitch_login"], current, live=False,
                                    known_live=broadcasts)
        if item:
            item.update(stream_id=row.get("stream_id"), archive_id=row["id"], game_id=channel["game_id"],
                        viewer_count=channel.get("viewer_count", 0))
            if "directory_rank" in channel and not exact:
                item["directory_rank"] = channel["directory_rank"]
            else:
                item["directory_followup"] = True
            yield item


def configured_ru_archives(archives, headers, current, known_live, game_id):
    if "STREAMCUT_TWITCH_RU" not in os.environ:
        return
    try:
        channels = discovery.twitch_channels({"RU": "ru"}, {"RU": discovery.twitch_authors("RU")}, headers)
    except discovery.ConnectionProblem:
        return
    for user, (login, _) in channels.items():
        if user in archives:
            continue  # Current top channels are already covered.
        channel = dict(twitch_user=user, twitch_login=login, lang="RU", game_id=game_id)
        rows = optional_archive_rows(user, archives, headers)
        yield from channel_archives(channel, rows, current, known_live)


def previous_selection(entries, lang, game_id, current):
    for item in entries:
        if (item.get("provider") != "Twitch" or item.get("lang") != lang or item.get("game_id") != game_id or
                not ("directory_rank" in item or item.get("directory_followup"))):
            continue
        window = FOLLOWUP_WINDOW if item["kind"] == "live" else discovery.WINDOW
        stamp = regional.timestamp(item["observed"])
        if stamp and current - window <= stamp <= current:
            yield {key: value for key, value in item.items() if key != "directory_rank"} | {
                "directory_followup": True}


def follow_archives(selected, previous, archives, headers, current):
    seen = {(item["key"], item["kind"]) for item in selected}
    for item in previous:
        if item["kind"] == "vod" and (item["key"], "vod") not in seen:
            selected.append(item)
            seen.add((item["key"], "vod"))
    for item in previous:
        key = item["key"]
        if item["kind"] != "live" or (key, "live") in seen or (key, "vod") in seen:
            continue
        user = item["twitch_user"]
        rows = optional_archive_rows(user, archives, headers)
        matched = next(channel_archives(item, rows, current, set(), exact=True), None)
        if matched:
            selected.append(matched)
            seen.add((key, "vod"))
        else:
            selected.append(item)
            seen.add((key, "live"))


def search(languages, current, *, known_live=(), previous=None):
    """Return ranked streams and recent archives from ranked or explicitly followed channels."""
    client = os.environ.get("TWITCH_CLIENT_ID")
    secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client or not secret:
        return None
    headers = discovery.twitch_headers(client, secret)
    game_id = category_id(headers)
    known_live = set(known_live)
    results = {}
    previous = previous or {}
    for lang, code in languages.items():
        selected, archives = [], {}
        for stream in market_streams(lang, code, game_id, headers, current):
            selected.append(stream)
            user = stream["twitch_user"]
            archives[user] = archive_rows(user, headers)
            selected.extend(channel_archives(stream, archives[user], current, known_live))
        if lang == "RU":
            selected.extend(configured_ru_archives(archives, headers, current, known_live, game_id))
        retained = list(previous_selection(previous.get(lang, ()), lang, game_id, current))
        follow_archives(selected, retained, archives, headers, current)
        results[lang] = selected
    return results
