"""Follow the live Grand Theft Auto VI directory and the selected channels' archives."""

from datetime import timedelta
import os

import discovery
import regional


TOP_CHANNELS = 10
ARCHIVES_PER_CHANNEL = 5
CATEGORY_ARCHIVES = 50
RECHECK_ARCHIVES = 100
RECHECK_STREAMS = 30
CATEGORY = "Grand Theft Auto VI"
FOLLOWUP_WINDOW = timedelta(hours=24)
STRICT_RU = "STREAMCUT_TWITCH_STRICT_RU"
RU_AUTHORS = "STREAMCUT_TWITCH_RU"


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


def category_archives(game_id, code, headers, current):
    try:
        rows = discovery.helix(
            "videos", [("game_id", game_id), ("language", code), ("type", "archive"),
                       ("sort", "time"), ("period", "week"), ("first", CATEGORY_ARCHIVES)],
            headers, "Свежие записи категории GTA VI")
    except discovery.ConnectionProblem:
        return
    for row in rows:
        if row.get("viewable") != "public":
            continue
        login = str(row.get("user_login", "")).lower()
        item = regional.twitch_item(row, "RU", login, current, live=False, category=True)
        if item:
            yield dict(item, stream_id=row.get("stream_id"), archive_id=row["id"],
                       game_id=game_id, directory_followup=True)


def current_archive_ids(items, headers):
    identifiers = dict.fromkeys(item.get("archive_id") for item in items if item["kind"] == "vod")
    identifiers = [identifier for identifier in identifiers if
                   isinstance(identifier, str) and discovery.TWITCH_ID.fullmatch(identifier)][:RECHECK_ARCHIVES]
    if not identifiers:
        return set()
    try:
        rows = discovery.helix("videos", [("id", identifier) for identifier in identifiers],
                               headers, "Доступность ранее найденных записей Twitch")
    except discovery.ConnectionProblem:
        return set()
    return {row["id"] for row in rows if row.get("id") in identifiers and
            row.get("viewable") == "public" and row.get("type") == "archive" and row.get("language") == "ru"}


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


def follow_archives(selected, previous, code, archives, headers, current, *, verify_vods=False):
    seen = {(item["key"], item["kind"]) for item in selected}
    prior_vods = [item for item in previous if item["kind"] == "vod" and (item["key"], "vod") not in seen]
    if verify_vods:
        prior_vods.sort(key=lambda item: item["observed"], reverse=True)
    confirmed = current_archive_ids(prior_vods, headers) if verify_vods else None
    for item in prior_vods:
        if confirmed is not None and item.get("archive_id") not in confirmed:
            continue
        selected.append(item)
        seen.add((item["key"], "vod"))
    pending = [item for item in previous if item["kind"] == "live" and
               (item["key"], "live") not in seen and (item["key"], "vod") not in seen]
    if verify_vods:
        pending.sort(key=lambda item: item["observed"], reverse=True)
        pending = pending[:RECHECK_STREAMS]
    for item in pending:
        key = item["key"]
        if (key, "live") in seen or (key, "vod") in seen:
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
    if strict and languages.get("RU") != "ru":
        return results
    headers = discovery.twitch_headers(client, secret)
    game_id = category_id(headers)
    known_live = set(known_live)
    previous = previous or {}
    for lang, code in ({"RU": "ru"} if strict else languages).items():
        selected, archives = [], {}
        active = market_streams(lang, code, game_id, headers, current)
        for stream in active:
            selected.append(stream)
            user = stream["twitch_user"]
            rows = optional_archive_rows(user, code, archives, headers)
            selected.extend(channel_archives(stream, rows, current, known_live))
        followed = optional_ru_channels(headers) if lang == "RU" else {}
        extra_live = []
        if strict:
            offline_or_unranked = {user: login for user, login in followed.items() if user not in archives}
            if offline_or_unranked:
                try:
                    extra_live = market_streams(lang, code, game_id, headers, current, channels=offline_or_unranked)
                except discovery.ConnectionProblem:
                    pass
            for stream in extra_live:
                stream.pop("directory_rank")
                stream["directory_followup"] = True
            selected.extend(extra_live)
        selected.extend(archive_candidates(followed, code, archives, headers, current, known_live, game_id,
                                           active=extra_live))
        if strict:
            selected.extend(category_archives(game_id, code, headers, current))
        retained = list(previous_selection(previous.get(lang, ()), lang, game_id, current))
        follow_archives(selected, retained, code, archives, headers, current, verify_vods=strict)
        if strict:
            priority = {user: index for index, user in enumerate(followed, start=1)}
            unique = {}
            for item in selected:
                if item["twitch_user"] in priority:
                    item["directory_priority"] = priority[item["twitch_user"]]
                else:
                    item.pop("directory_priority", None)
                unique.setdefault((item["key"], item["kind"]), item)
            selected = sorted(unique.values(), key=lambda item: item.get("directory_priority", len(priority) + 1))
        results[lang] = selected
    return results
