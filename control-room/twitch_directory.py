"""Follow live GTA VI streams, recent archives, and clips on Twitch."""

from datetime import timedelta
import os

import discovery
import regional
import twitch_clips


TOP_CHANNELS = 10
ARCHIVES_PER_CHANNEL = 5
CATEGORY_ARCHIVES = 50
CATEGORY_STREAM_PAGES = 10
CATEGORY_ARCHIVE_PAGES = 2
UNRANKED_ARCHIVE_CHANNELS = 8
ALTERNATE_ARCHIVE_CHANNELS = 6
RECENT_CLIP_LIMIT = 300
ARCHIVE_ORDERS = (("views", "day"), ("trending", "week"), ("views", "week"), ("trending", "day"))
DIRECTORY_INTERVAL_SECONDS = 30 * 60
DIRECTORY_CURSOR_TTL = timedelta(hours=2)
RECHECK_ARCHIVES = 100
RECHECK_STREAMS = 30
CATEGORY = "Grand Theft Auto VI"
CATEGORY_QUERIES = ("GTA 6", "GTA VI", "Grand Theft Auto 6")
CATEGORY_NAMES = frozenset(name.casefold() for name in (CATEGORY, *CATEGORY_QUERIES))
FOLLOWUP_WINDOW = timedelta(hours=24)
STRICT_RU = "STREAMCUT_TWITCH_STRICT_RU"
RU_AUTHORS = "STREAMCUT_TWITCH_RU"


def exact_category(games):
    identities = {game["id"] for game in games if isinstance(game.get("id"), str)
                  and discovery.TWITCH_ID.fullmatch(game["id"])
                  and str(game.get("name", "")).strip().casefold() in CATEGORY_NAMES}
    return next(iter(identities)) if len(identities) == 1 else None


def category_id(headers, *, aliases=False):
    games = discovery.helix("games", [("name", CATEGORY)], headers, "Категория GTA VI")
    found = exact_category(game for game in games if str(game.get("name", "")).casefold() == CATEGORY.casefold())
    if found:
        return found
    if aliases:
        related = []
        for query in CATEGORY_QUERIES:
            try:
                related.extend(discovery.helix("search/categories", [("query", query), ("first", 10)],
                                               headers, "Варианты категории GTA VI"))
            except discovery.ConnectionProblem:
                continue
        if found := exact_category(related):
            return found
    raise discovery.ConnectionProblem("Категория Grand Theft Auto VI не найдена в Twitch.")


def valid_cursor(value):
    return isinstance(value, str) and 0 < len(value) <= 2048 and value.isascii() and value.isprintable()


def directory_page(endpoint, params, headers, description):
    payload = twitch_clips.helix_page(endpoint, params, headers, description)
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise discovery.ConnectionProblem(f"{description}: неожиданный список.")
    pagination = payload.get("pagination")
    cursor = pagination.get("cursor") if isinstance(pagination, dict) else None
    return [row for row in rows if isinstance(row, dict)], cursor if valid_cursor(cursor) else None


def browse_directory_pages(endpoint, params, headers, description, limit, *, previous_cursor=None):
    head, head_cursor = directory_page(endpoint, params, headers, description)
    pages, consumed = [head], set()
    cursor = (previous_cursor or head_cursor) if head_cursor else None
    continuing = cursor != head_cursor
    for _ in range(limit - 1):
        if cursor is None or cursor in consumed:
            break
        try:
            rows, next_cursor = directory_page(endpoint, [*params, ("after", cursor)], headers, description)
        except discovery.ConnectionProblem:
            if not continuing:
                break
            # A prior cursor may have expired. Resume the fresh page chain once.
            cursor = head_cursor
            continuing = False
            continue
        pages.append(rows)
        consumed.add(cursor)
        cursor = next_cursor
        continuing = False
    return pages, None if cursor in consumed else cursor


def directory_pages(endpoint, params, headers, description, limit):
    pages, _ = browse_directory_pages(endpoint, params, headers, description, limit)
    return [row for batch in pages for row in batch]


def cached_stream_cursor(entries, lang, game_id, current):
    for item in entries:
        cursor = item.get("directory_after")
        if (item.get("provider") == "Twitch" and item.get("lang") == lang and item.get("game_id") == game_id
                and item.get("kind") == "live" and item.get("directory_rank") == 1 and valid_cursor(cursor)):
            stamp = regional.timestamp(item.get("observed"))
            if stamp and current - DIRECTORY_CURSOR_TTL <= stamp <= current:
                return cursor
    return None


def market_streams(lang, code, game_id, headers, current, *, channels=None, expanded=False, previous_cursor=None):
    params = [("game_id", game_id), ("language", code), ("first", 100 if expanded else TOP_CHANNELS * 2)]
    if channels is not None:
        params.extend(("user_id", user) for user in channels)
    def viewers(row):
        return regional.count(row.get("viewer_count"))

    next_cursor = None
    if expanded:
        pages, next_cursor = browse_directory_pages(
            "streams", params, headers, "Каталог русских эфиров Twitch", CATEGORY_STREAM_PAGES,
            previous_cursor=previous_cursor)
        rows = [row for batch in pages for row in sorted(batch, key=viewers, reverse=True)]
    else:
        rows = discovery.helix("streams", params, headers, "Топ каналов Twitch")
        if channels is None:
            rows = sorted(rows, key=viewers, reverse=True)
        else:
            priority = {user: index for index, user in enumerate(channels)}
            rows = sorted(rows, key=lambda row: priority.get(row.get("user_id"), len(priority)))
    candidates = []
    users = set()
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
        item.update(stream_id=row["id"], game_id=game_id, viewer_count=viewers(row))
        if len(candidates) < TOP_CHANNELS:
            item["directory_rank"] = len(candidates) + 1
        else:
            item["directory_followup"] = True
        candidates.append(item)
        if not expanded and len(candidates) == TOP_CHANNELS:
            break
    if candidates and next_cursor:
        candidates[0]["directory_after"] = next_cursor
    return candidates


def archive_rows(user, code, headers):
    return discovery.helix("videos", [("user_id", user), ("language", code), ("type", "archive"),
                                      ("sort", "time"), ("first", ARCHIVES_PER_CHANNEL)],
                            headers, "Записи выбранного канала Twitch")


def rotation(current):
    return int(current.timestamp()) // DIRECTORY_INTERVAL_SECONDS


def rotating_subset(items, turn, limit):
    if len(items) <= limit:
        return items
    start = (turn * limit) % len(items)
    return (items + items)[start:start + limit]


def alternate_channel_archives(channels, code, headers, current, known_live):
    turn = rotation(current)
    sort, period = ARCHIVE_ORDERS[turn % len(ARCHIVE_ORDERS)]
    for channel in rotating_subset(channels, turn, ALTERNATE_ARCHIVE_CHANNELS):
        try:
            rows = directory_pages(
                "videos", [("user_id", channel["twitch_user"]), ("language", code), ("type", "archive"),
                           ("sort", sort), ("period", period), ("first", ARCHIVES_PER_CHANNEL)],
                headers, "Альтернативная выборка записей канала Twitch", CATEGORY_ARCHIVE_PAGES)
        except discovery.ConnectionProblem:
            continue
        yield from channel_archives(channel, rows, current, known_live)


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
    orders = (("time", "week"), ARCHIVE_ORDERS[rotation(current) % len(ARCHIVE_ORDERS)])
    for sort, period in orders:
        try:
            rows = directory_pages(
                "videos", [("game_id", game_id), ("language", code), ("type", "archive"),
                           ("sort", sort), ("period", period), ("first", CATEGORY_ARCHIVES)],
                headers, "Свежие записи категории GTA VI", CATEGORY_ARCHIVE_PAGES)
        except discovery.ConnectionProblem:
            continue
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
            yield {key: value for key, value in item.items() if key not in ("directory_rank", "directory_after")} | {
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
    """Keep the display ranking separate from the broader search for material."""
    client = os.environ.get("TWITCH_CLIENT_ID")
    secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client or not secret:
        return None
    strict = os.environ.get(STRICT_RU) == "1"
    results = {lang: [] for lang in languages}
    if strict and languages.get("RU") != "ru":
        return results
    headers = discovery.twitch_headers(client, secret)
    game_id = category_id(headers, aliases=strict)
    known_live = set(known_live)
    previous = previous or {}
    for lang, code in ({"RU": "ru"} if strict else languages).items():
        selected, archives = [], {}
        entries = previous.get(lang, ())
        active = market_streams(lang, code, game_id, headers, current, expanded=strict,
                                previous_cursor=cached_stream_cursor(entries, lang, game_id, current) if strict else None)
        current_channels = {stream["twitch_user"]: stream for stream in active}
        archive_channels = (active[:TOP_CHANNELS] + rotating_subset(
            active[TOP_CHANNELS:], rotation(current), UNRANKED_ARCHIVE_CHANNELS) if strict else active)
        for stream in archive_channels:
            selected.append(stream)
            user = stream["twitch_user"]
            rows = optional_archive_rows(user, code, archives, headers)
            selected.extend(channel_archives(stream, rows, current, known_live))
        selected.extend(stream for stream in active if stream["twitch_user"] not in archives)
        followed = optional_ru_channels(headers) if lang == "RU" else {}
        extra_live = []
        if strict:
            outside_directory = {user: login for user, login in followed.items() if user not in current_channels}
            if outside_directory:
                try:
                    extra_live = market_streams(lang, code, game_id, headers, current, channels=outside_directory)
                except discovery.ConnectionProblem:
                    pass
            for stream in extra_live:
                stream.pop("directory_rank")
                stream["directory_followup"] = True
            selected.extend(extra_live)
        selected.extend(archive_candidates(followed, code, archives, headers, current, known_live, game_id,
                                           active=active + extra_live))
        if strict:
            followed_channels = {**current_channels, **{stream["twitch_user"]: stream for stream in extra_live}}
            additional = [followed_channels.get(user) or dict(twitch_user=user, twitch_login=login,
                          lang="RU", game_id=game_id) for user, login in followed.items()]
            additional.extend(stream for stream in archive_channels if stream["twitch_user"] not in followed)
            selected.extend(alternate_channel_archives(additional, code, headers, current, known_live))
            selected.extend(category_archives(game_id, code, headers, current))
        retained = list(previous_selection(entries, lang, game_id, current))
        if strict:
            clips = {item["clip_id"]: item for item in retained if item["kind"] == "video" and item.get("clip_id")}
            try:
                clips.update((item["clip_id"], item) for item in twitch_clips.search(game_id, headers, current))
            except discovery.ConnectionProblem:
                pass
            selected.extend(sorted(clips.values(), key=lambda item: item["observed"], reverse=True)[:RECENT_CLIP_LIMIT])
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
