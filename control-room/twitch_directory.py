"""Follow the live Grand Theft Auto VI directory and the selected channels' archives."""

from datetime import timedelta
import os
from urllib.parse import urlencode

import discovery
import regional


TOP_CHANNELS = 10
ARCHIVES_PER_CHANNEL = 5
CATEGORY = "Grand Theft Auto VI"
FOLLOWUP_WINDOW = timedelta(hours=24)
STRICT_RU = "STREAMCUT_TWITCH_STRICT_RU"
RU_AUTHORS = "STREAMCUT_TWITCH_RU"
MIN_RU_FOLLOWERS = 1_000_000


def category_id(headers):
    games = discovery.helix("games", [("name", CATEGORY)], headers, "Категория GTA VI")
    for game in games:
        identifier = game.get("id", "")
        if game.get("name", "").casefold() == CATEGORY.casefold() and discovery.TWITCH_ID.fullmatch(identifier):
            return identifier
    raise discovery.ConnectionProblem("Категория Grand Theft Auto VI не найдена в Twitch.")


def market_streams(lang, code, game_id, headers, current, *, channels=None):
    params = [("game_id", game_id), ("language", code), ("first", TOP_CHANNELS * 2)]
    if channels is not None:
        params.extend(("user_id", user) for user in channels)
    rows = discovery.helix("streams", params, headers, "Топ каналов Twitch")
    candidates = []
    users = set()
    if channels is None:
        rows = sorted(rows, key=lambda row: regional.count(row.get("viewer_count")), reverse=True)
    else:
        priority = {user: index for index, user in enumerate(channels)}
        rows = sorted(rows, key=lambda row: priority.get(row.get("user_id"), len(priority)))
    for row in rows:
        if row.get("language") != code or row.get("game_id") != game_id:
            continue
        if channels is not None and row.get("user_id") not in channels:
            continue
        login = row.get("user_login", "").lower() if channels is None else channels[row["user_id"]]
        item = regional.twitch_item(row, lang, login, current, live=True, category=True)
        if not item or item["twitch_user"] in users:
            continue
        users.add(item["twitch_user"])
        candidates.append(dict(item, stream_id=row["id"], game_id=game_id, directory_rank=len(candidates) + 1,
                               viewer_count=regional.count(row.get("viewer_count"))))
        if len(candidates) == TOP_CHANNELS:
            break
    return candidates


def archive_rows(user, code, headers):
    return discovery.helix("videos", [("user_id", user), ("language", code), ("type", "archive"),
                                      ("sort", "time"), ("first", ARCHIVES_PER_CHANNEL)],
                            headers, "Записи выбранного канала Twitch")


def optional_archive_rows(user, code, archives, headers):
    if user not in archives:
        try:
            archives[user] = archive_rows(user, code, headers)
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


def explicit_ru_channels(headers):
    if RU_AUTHORS not in os.environ:
        return {}
    terms = discovery.twitch_authors("RU")
    channels = discovery.twitch_channels({"RU": "ru"}, {"RU": terms}, headers)
    by_login = {login: user for user, (login, _) in channels.items()}
    ordered = {}
    for term in terms:
        user = term if discovery.TWITCH_ID.fullmatch(term) else by_login.get(term)
        if user in channels:
            ordered[user] = channels[user][0]
    return ordered


def channel_followers(user, headers):
    params = urlencode({"broadcaster_id": user, "first": 1})
    status, _, body = discovery.request("server", "api.twitch.tv", "GET",
                                        f"/helix/channels/followers?{params}", headers)
    total = discovery.json_response(status, body, "Число подписчиков Twitch").get("total")
    if type(total) is not int or total < 0:
        raise discovery.ConnectionProblem("Twitch не подтвердил число подписчиков.")
    return total


def qualified_channels(headers):
    channels = explicit_ru_channels(headers)
    eligible = {}
    failures = []
    for user, login in channels.items():
        try:
            followers = channel_followers(user, headers)
        except discovery.ConnectionProblem as error:
            failures.append(error)
            continue
        if followers >= MIN_RU_FOLLOWERS:
            eligible[user] = login
    if failures and len(failures) == len(channels):
        raise failures[0]
    return eligible


def archive_candidates(channels, code, archives, headers, current, known_live, game_id, *, active=()):
    live_channels = {stream["twitch_user"]: stream for stream in active}
    for user, login in channels.items():
        if user in archives:
            continue  # Current top channels are already covered.
        channel = live_channels.get(user) or dict(twitch_user=user, twitch_login=login, lang="RU", game_id=game_id)
        rows = optional_archive_rows(user, code, archives, headers)
        yield from channel_archives(channel, rows, current, known_live)


def optional_ru_channels(headers):
    try:
        return explicit_ru_channels(headers)
    except discovery.ConnectionProblem:
        return {}


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


def follow_archives(selected, previous, code, archives, headers, current):
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
        rows = optional_archive_rows(user, code, archives, headers)
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
    strict = os.environ.get(STRICT_RU) == "1"
    results = {lang: [] for lang in languages}
    if strict and (languages.get("RU") != "ru" or not os.environ.get(RU_AUTHORS, "").strip()):
        return results
    headers = discovery.twitch_headers(client, secret)
    channels = qualified_channels(headers) if strict else None
    if strict and not channels:
        return results
    game_id = category_id(headers)
    known_live = set(known_live)
    previous = previous or {}
    for lang, code in ({"RU": "ru"} if strict else languages).items():
        selected, archives = [], {}
        active = market_streams(lang, code, game_id, headers, current, channels=channels)
        if strict:
            selected.extend(active)
            selected.extend(archive_candidates(channels, code, archives, headers, current, known_live, game_id,
                                               active=active))
        else:
            for stream in active:
                selected.append(stream)
                user = stream["twitch_user"]
                archives[user] = archive_rows(user, code, headers)
                selected.extend(channel_archives(stream, archives[user], current, known_live))
            if lang == "RU":
                followed = optional_ru_channels(headers)
                selected.extend(archive_candidates(followed, code, archives, headers, current, known_live, game_id))
        retained = list(previous_selection(previous.get(lang, ()), lang, game_id, current))
        if strict:
            retained = [item for item in retained if item["twitch_user"] in channels]
        follow_archives(selected, retained, code, archives, headers, current)
        if strict:
            priority = {user: index for index, user in enumerate(channels, start=1)}
            for item in selected:
                item["directory_priority"] = priority[item["twitch_user"]]
            selected.sort(key=lambda item: item["directory_priority"])
        results[lang] = selected
    return results
