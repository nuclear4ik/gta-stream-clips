"""Read-only account metrics. Missing or delayed reports stay missing."""
from datetime import datetime, timedelta, timezone
import os
import re
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from transport import ConnectionProblem, json_response, request, token_request


def metric(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value):
        return int(value)
    return None


def get(account, host, path, token):
    status, _, body = request(account["route"], host, "GET", path,
                              {"Authorization": f"Bearer {token}"})
    return json_response(status, body, "Статистика")


def youtube(account):
    token = token_request(account["route"], grant_type="refresh_token",
                          refresh_token=account["refresh_token"])["access_token"]
    channel = get(account, "www.googleapis.com", "/youtube/v3/channels?part=statistics&mine=true", token)
    items = [r for r in channel.get("items", []) if r.get("id") == account["target"]]
    if not items:
        raise ConnectionProblem("Токен относится к другому каналу.")
    result = {"followers_total": metric(items[0].get("statistics", {}).get("subscriberCount"))}
    yesterday = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)
    params = urlencode({"ids": "channel==MINE", "startDate": yesterday - timedelta(days=6),
                        "endDate": yesterday, "metrics": "views,subscribersGained",
                        "dimensions": "day", "sort": "-day", "maxResults": 1})
    try:
        report = get(account, "youtubeanalytics.googleapis.com", "/v2/reports?" + params, token)
    except ConnectionProblem:
        return {**result, "note": "Подключите YouTube Analytics API и обновите доступ."}
    rows = report.get("rows")
    if rows and len(rows[0]) == 3:
        day, views, gained = rows[0]
        if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return {**result, "views": metric(views), "followers_gained": metric(gained), "day": day,
                    "note": "Последние доступные сутки по данным YouTube."}
    return {**result, "note": "YouTube пока не вернул суточный отчёт."}


def handle(account, received):
    if not isinstance(received, str) or account["target"].lstrip("@").casefold() != received.casefold():
        raise ConnectionProblem("Токен относится к другому аккаунту.")


def tiktok(account, token):
    response = get(account, "open.tiktokapis.com",
                   "/v2/user/info/?fields=username,follower_count", token)
    if response.get("error", {}).get("code") != "ok":
        raise ConnectionProblem("Проверьте разрешения TikTok Display API.")
    user = response.get("data", {}).get("user", {})
    handle(account, user.get("username"))
    return {"followers_total": metric(user.get("follower_count")),
            "note": "TikTok Display API не возвращает статистику за сутки."}


def instagram(account, token):
    host = "graph.instagram.com"
    user = get(account, host, "/v26.0/me?fields=user_id,username,followers_count", token)
    handle(account, user.get("username"))
    identifier = str(user.get("user_id", ""))
    if not re.fullmatch(r"[0-9]{1,24}", identifier):
        raise ConnectionProblem("Instagram API не вернул ID аккаунта.")
    result = {"followers_total": metric(user.get("followers_count"))}
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    params = {"period": "day", "metric_type": "total_value", "since": int(start.timestamp()),
              "until": int(end.timestamp())}
    endpoint = f"/v26.0/{identifier}/insights?"
    try:
        views = get(account, host, endpoint + urlencode({**params, "metric": "views"}), token)
        for row in views.get("data", []):
            if row.get("name") == "views":
                result["views"] = metric(row.get("total_value", {}).get("value"))
    except ConnectionProblem:
        pass
    try:
        follows = get(account, host, endpoint + urlencode({**params, "metric": "follows_and_unfollows",
                                                           "breakdown": "follow_type"}), token)
        for row in follows.get("data", []):
            if row.get("name") != "follows_and_unfollows":
                continue
            for group in row.get("total_value", {}).get("breakdowns", []):
                for entry in group.get("results", []):
                    dimension = [str(value).lower() for value in entry.get("dimension_values", [])]
                    if dimension in (["follow"], ["follows"]):
                        result["followers_gained"] = metric(entry.get("value"))
    except ConnectionProblem:
        pass
    if any(result.get(key) is not None for key in ("views", "followers_gained")):
        result.update(day=start.date().isoformat(), note="Последние полные сутки, UTC.")
    else:
        result["note"] = "Instagram не вернул суточный отчёт."
    return result


def read(account):
    if account["platform"] == "YouTube":
        return youtube(account) if account["refresh_token"] else None
    prefix = "TIKTOK" if account["platform"] == "TikTok" else "INSTAGRAM"
    token = os.environ.get(f"STREAMCUT_{prefix}_METRICS_{account['id']}")
    if not token:
        return None
    return tiktok(account, token) if prefix == "TIKTOK" else instagram(account, token)
