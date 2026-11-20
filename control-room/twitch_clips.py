"""Find recent Russian clips in the official Twitch game category."""

from datetime import timedelta, timezone
from urllib.parse import urlencode

import discovery
import regional
from twitch_media import CLIP_ID


CLIPS_PER_PAGE = 100
PAGES_PER_WINDOW = 3
RECENT_WINDOW = timedelta(hours=1)
BACKFILL_WINDOW = timedelta(minutes=31)
BACKFILL_AGES = (6, 24, 48)


def helix_page(endpoint, params, headers, description):
    status, _, body = discovery.request(
        "server", "api.twitch.tv", "GET", f"/helix/{endpoint}?{urlencode(params)}", headers,
    )
    return discovery.json_response(status, body, description)


def search_windows(current):
    end = current.astimezone(timezone.utc).replace(second=0, microsecond=0)
    yield end - RECENT_WINDOW, end
    for age in BACKFILL_AGES:
        earlier = end - timedelta(hours=age)
        yield earlier - BACKFILL_WINDOW, earlier


def utc_query_value(moment):
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def clip_candidate(row, game_id, start, end, current):
    identifier, broadcaster = row.get("id"), row.get("broadcaster_id")
    if (not isinstance(identifier, str) or not CLIP_ID.fullmatch(identifier)
            or not isinstance(broadcaster, str) or not discovery.TWITCH_ID.fullmatch(broadcaster)
            or row.get("game_id") != game_id or row.get("language") != "ru"
            or row.get("viewable", "public") != "public" or row.get("is_public") is False):
        return None
    created, duration = regional.timestamp(row.get("created_at")), row.get("duration")
    if not created or not start <= created <= end or type(duration) not in (float, int) or not 25 <= duration <= 60:
        return None
    title, name = row.get("title"), row.get("broadcaster_name")
    if not isinstance(title, str) or not title.strip() or not isinstance(name, str) or not name.strip():
        return None
    return regional.candidate(
        "RU", name.strip(), f"twitch:{broadcaster}", title.strip(), f"https://clips.twitch.tv/{identifier}",
        "video", f"twitch:clip:{identifier}", "Twitch", created,
        regional.popularity(row.get("view_count"), created, current, False),
        game_id=game_id, twitch_user=broadcaster, clip_id=identifier, directory_followup=True,
    )


def search(game_id, headers, current):
    """Collect metadata only. A separate ingestion step verifies file availability."""
    found = {}
    completed = False
    failure = None
    for start, end in search_windows(current):
        base = [("game_id", game_id), ("first", CLIPS_PER_PAGE),
                ("started_at", utc_query_value(start)), ("ended_at", utc_query_value(end))]
        params = base
        cursors = set()
        for _ in range(PAGES_PER_WINDOW):
            try:
                page = helix_page("clips", params, headers, "Клипы категории Twitch")
                rows = page.get("data")
                if not isinstance(rows, list):
                    raise discovery.ConnectionProblem("Клипы категории Twitch: неожиданный список.")
            except discovery.ConnectionProblem as error:
                failure = error
                break
            completed = True
            for row in rows:
                if isinstance(row, dict) and (item := clip_candidate(row, game_id, start, end, current)):
                    found.setdefault(item["clip_id"], item)
            pagination = page.get("pagination")
            cursor = pagination.get("cursor") if isinstance(pagination, dict) else None
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                break
            cursors.add(cursor)
            params = [*base, ("after", cursor)]
    if not completed and failure is not None:
        raise failure
    return sorted(found.values(), key=lambda item: (item["observed"], item["clip_id"]), reverse=True)
