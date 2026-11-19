"""Bounded official API discovery and rolling candidate rankings."""

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import unescape
from itertools import islice
import math
import os
import re
from urllib.parse import urlencode

import discovery as d

REGIONAL_INTERVAL = 5 * 3600
BOARD_SIZE = 10
MAX_YOUTUBE_TERMS = 12
YOUTUBE_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}")


def configured():
    return bool(os.environ.get("STREAMCUT_YOUTUBE_KEY") or
                os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"))


def batches(items, size):
    iterator = iter(items)
    while group := list(islice(iterator, size)):
        yield group


def timestamp(raw):
    try:
        value = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    return value if value.tzinfo else None


def count(raw):
    if type(raw) is int:
        return max(raw, 0)
    if isinstance(raw, str) and re.fullmatch(r"[0-9]{1,18}", raw):
        return int(raw)
    return 0


def popularity(value, observed, current, live):
    numeric = count(value)
    if not live:
        hours = max(6, (current - observed).total_seconds() / 3600)
        numeric /= hours
    return round(math.log1p(numeric), 6)


def candidate(lang, label, author, title, url, kind, key, provider, seen, score, **fields):
    return dict(lang=lang, creator=label[:100], author=author, title=title[:140], url=url,
                kind=kind, key=key, provider=provider, observed=seen.isoformat(),
                popularity=score, **fields)


def youtube_query(path, params, key):
    status, _, body = d.request("server", "www.googleapis.com", "GET",
                                path + "?" + urlencode(params), {"X-Goog-Api-Key": key})
    return d.json_response(status, body, "Поиск YouTube").get("items", [])


def alias_groups(lang):
    return [(names[0], d.creator_pattern(names))
            for names in d.youtube_authors(lang)[:MAX_YOUTUBE_TERMS]]


def creator_identity(lang, label):
    return f"known:{lang}:{d.matching_text(label)}"


def youtube_market(lang, code, current, *, live_only=False):
    key = os.environ.get("STREAMCUT_YOUTUBE_KEY")
    if not key:
        return None
    names = d.youtube_authors(lang)[:MAX_YOUTUBE_TERMS]
    markers = ["GTA 6", "GTA VI", "Grand Theft Auto VI"]
    if lang == "RU":
        markers.extend(["ГТА 6", "ГТА VI"])
    after = current - d.WINDOW
    base = dict(part="snippet", type="video", order="viewCount", maxResults=35,
                regionCode=d.REGION[lang], relevanceLanguage=code)
    live_base = {**base, "eventType": "live"}
    queries = [{**live_base, "q": "|".join(markers)}]
    if not live_only:
        recent = {**base, "publishedAfter": after.isoformat().replace("+00:00", "Z")}
        queries.append({**recent, "q": "|".join(markers)})
        for group in batches(names, 4):
            terms = [f"GTA 6 {alias}" for aliases in group for alias in aliases[:2]]
            queries.append({**recent, "q": "|".join(terms)})
    ids = {}
    for query in queries:
        for item in youtube_query("/youtube/v3/search", query, key):
            identifier = item.get("id", {}).get("videoId", "")
            if d.YOUTUBE_ID.fullmatch(identifier):
                ids[identifier] = None
    aliases = alias_groups(lang)
    results = []
    for batch in batches(ids, 50):
        for row in youtube_query("/youtube/v3/videos", {"id": ",".join(batch),
                                      "part": "snippet,statistics,liveStreamingDetails"}, key):
            info = row.get("snippet", {})
            identifier, uploader_id = row.get("id", ""), info.get("channelId", "")
            if identifier not in ids or not YOUTUBE_CHANNEL.fullmatch(uploader_id):
                continue
            title = unescape(info.get("title", ""))
            uploader = unescape(info.get("channelTitle", ""))
            if not d.TOPIC.search(d.subject(title)) or not uploader:
                continue
            broadcast = info.get("liveBroadcastContent")
            if broadcast == "upcoming" or live_only and broadcast != "live":
                continue
            live = broadcast == "live"
            stamp = timestamp(info.get("publishedAt"))
            if not live and (not stamp or stamp < after or stamp > current):
                continue
            preferred = [label for label, alias in aliases if alias.search(d.matching_text(uploader))]
            named = preferred or [label for label, alias in aliases if alias.search(d.subject(title))]
            if len(named) > 1:
                continue
            declared = (info.get("defaultAudioLanguage") or info.get("defaultLanguage") or "").lower()
            if (declared and declared.split("-")[0] != code) or (not named and not declared):
                continue
            # A matching creator in the subject may be the person in a reupload,
            # not its uploader. Unknown names must come from a real channel ID.
            label = named[0] if named else uploader
            author = creator_identity(lang, label) if named else "youtube:" + uploader_id
            viewers = (row.get("liveStreamingDetails", {}).get("concurrentViewers") if live
                       else row.get("statistics", {}).get("viewCount"))
            results.append(candidate(lang, label, author, title, f"https://www.youtube.com/watch?v={identifier}",
                                     "live" if live else "video", f"youtube:{identifier}", "YouTube",
                                     current if live else stamp, popularity(viewers, stamp or current, current, live)))
    return results


def twitch_names(lang):
    return {creator.twitch: creator.label for creator in d.WATCHLIST.get(lang, ())}


def twitch_item(row, lang, login, current, *, live, category=False, known_live=()):
    user, identifier = row.get("user_id", ""), row.get("id", "")
    if not (d.TWITCH_ID.fullmatch(user) and d.TWITCH_ID.fullmatch(identifier)):
        return None
    code = {"RU": "ru", "EN": "en", "ESP": "es"}[lang]
    if row.get("language") != code:
        return None
    raw_title = row.get("title", "")
    subject_match = d.TOPIC.search(d.subject(raw_title))
    stream = identifier if live else row.get("stream_id") or ""
    if live:
        game = d.matching_text(row.get("game_name", ""))
        by_game = category or bool(d.TOPIC.fullmatch(game))
        if row.get("type") != "live" or not (by_game or game in ("", "just chatting") and subject_match):
            return None
        if not d.TWITCH_LOGIN.fullmatch(login):
            return None
        stamp = current
    else:
        if row.get("type") != "archive" or row.get("viewable", "public") != "public":
            return None
        if not (category or subject_match or (lang, stream) in known_live):
            return None
        stamp = timestamp(row.get("created_at"))
        if not stamp or not current - d.WINDOW <= stamp <= current:
            return None
    known = twitch_names(lang).get(login)
    label = known or row.get("user_name") or login or f"Twitch {user}"
    author = creator_identity(lang, label) if known else "twitch:" + user
    key = f"twitch:stream:{stream}" if d.TWITCH_ID.fullmatch(stream) else f"twitch:vod:{identifier}"
    url = f"https://www.twitch.tv/{login}" if live else f"https://www.twitch.tv/videos/{identifier}"
    score = popularity(row.get("viewer_count" if live else "view_count"), stamp, current, live)
    return candidate(lang, label, author, raw_title or "GTA 6", url, "live" if live else "vod", key,
                     "Twitch", stamp, score, twitch_user=user, twitch_login=login)


def twitch_markets(languages, current, known_live, known_channels=None, *, live_only=False):
    client = os.environ.get("TWITCH_CLIENT_ID")
    secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client or not secret:
        return None
    headers = d.twitch_headers(client, secret)
    followers = {lang: d.twitch_authors(lang) for lang in languages}
    channels = d.twitch_channels(languages, followers, headers)
    for user, (login, lang) in (known_channels or {}).items():
        if user not in channels and lang in languages:
            channels[user] = (login, [(lang, languages[lang])])
    games = d.helix("games", [("name", "Grand Theft Auto VI"), ("name", "Grand Theft Auto 6")],
                    headers, "Категория Twitch")
    game_ids = list(dict.fromkeys(x["id"] for x in games if d.TWITCH_ID.fullmatch(x.get("id", ""))
                                  and d.TOPIC.fullmatch(d.matching_text(x.get("name", "")))))[:2]
    results = {}
    for lang, code in languages.items():
        results[lang] = []
        active = set(known_live)
        watched = [(user, login) for user, (login, markets) in channels.items() if (lang, code) in markets]
        logins = dict(watched)
        relevant = []
        for game in game_ids:
            items = d.helix("streams", [("game_id", game), ("language", code), ("first", 40)], headers, "Эфиры Twitch")
            relevant.extend((item, True) for item in items)
        for group in batches(watched, 100):
            filters = [("user_id", uid) for uid, _ in group]
            relevant.extend((item, False) for item in d.helix("streams", filters, headers, "Эфиры Twitch"))
        for item, by_game in relevant:
            uid = item.get("user_id", "")
            if not by_game and uid not in logins:
                continue
            login = item.get("user_login", logins.get(uid, "")).lower()
            if found := twitch_item(item, lang, login, current, live=True, category=by_game):
                results[lang].append(found)
                active.add((lang, item["id"]))
        if live_only:
            continue
        for uid, login in watched:
            rows = d.helix("videos", [("user_id", uid), ("language", code), ("type", "archive"), ("first", 5),
                                     ("sort", "time")], headers, "Записи Twitch")
            for item in rows:
                if item.get("user_id") == uid and (found := twitch_item(
                        item, lang, login, current, live=False, known_live=active)):
                    results[lang].append(found)
        for game in game_ids:
            rows = d.helix("videos", [("game_id", game), ("language", code), ("type", "archive"),
                                     ("first", 40), ("period", "week"), ("sort", "views")],
                           headers, "Записи категории Twitch")
            for row in rows:
                login = row.get("user_login", logins.get(row.get("user_id"), "")).lower()
                if found := twitch_item(row, lang, login, current, live=False, category=True):
                    results[lang].append(found)
    return results


def current_candidates(entries, instant):
    """Keep recent videos and only streams seen in the latest six hours."""
    end = instant - d.WINDOW
    live_end = instant - timedelta(hours=6)
    best = {}
    for item in entries:
        observed = timestamp(item["observed"])
        if observed is None or observed < (live_end if item["kind"] == "live" else end):
            continue
        old = best.get(item["key"])
        if old is None or (old["kind"] == "live" and item["kind"] != "live") or (
                old["kind"] == item["kind"] and
                (item["popularity"], item["observed"]) > (old["popularity"], old["observed"])):
            best[item["key"]] = item
    return list(best.values())


def leaderboard(entries):
    """Compare rank positions inside each measurement group, not raw cross-site counts."""
    groups = defaultdict(dict)
    for row in entries:
        if row["popularity"] <= 0:
            continue
        group = (row["provider"], row["kind"] == "live")
        author = row["author"]
        old = groups[group].get(author)
        if old is None or row["popularity"] > old["popularity"]:
            groups[group][author] = row
    scores, labels = defaultdict(float), {}
    for group, records in sorted(groups.items()):
        ordered = sorted(records.values(), key=lambda x: (-x["popularity"], x["author"]))
        for index, entry in enumerate(ordered):
            author = entry["author"]
            labels[author] = entry["creator"]
            scores[author] += 1 / (1 + index)
    order = sorted(scores, key=lambda author: (-scores[author], labels[author].casefold(), author))
    return [{"author": author, "creator": labels[author]} for author in order[:BOARD_SIZE]]
