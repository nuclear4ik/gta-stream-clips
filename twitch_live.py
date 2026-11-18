#!/usr/bin/env python3
"""List Twitch broadcasts in viewer-count order using the official Helix API."""

import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


HELIX = "https://api.twitch.tv/helix"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"


def request_json(url, headers=None, form=None):
    data = urlencode(form).encode("ascii") if form else None
    request = Request(url, data=data, headers=headers or {})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def get_streams(client_id, client_secret, options):
    token = request_json(TOKEN_URL, form={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    })["access_token"]
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token}"}
    category = options.game_id
    if category is None:
        games = request_json(f"{HELIX}/games?{urlencode({'name': options.game})}", headers)["data"]
        if not games:
            raise ValueError("Категория Twitch не найдена. Укажите её название через --game или ID через --game-id.")
        category = games[0]["id"]

    query = [("game_id", category), ("first", options.limit)]
    if options.language:
        query.append(("language", options.language))
    query.extend(("user_login", channel) for channel in options.channel)
    streams = request_json(f"{HELIX}/streams?{urlencode(query)}", headers)["data"]
    return [{
        "channel": stream["user_login"],
        "url": f"https://www.twitch.tv/{stream['user_login']}",
        "title": stream["title"],
        "viewers": stream["viewer_count"],
        "game": stream["game_name"],
        "started_at": stream["started_at"],
    } for stream in streams]


def main():
    parser = argparse.ArgumentParser(description="Популярные прямые эфиры Twitch по категории.")
    parser.add_argument("--game", default="Grand Theft Auto VI", help="Название категории")
    parser.add_argument("--game-id", help="ID категории Twitch вместо названия")
    parser.add_argument("--language", help="Язык трансляции, например ru")
    parser.add_argument("--channel", action="append", default=[], help="Логин канала; можно указать несколько раз")
    parser.add_argument("--limit", type=int, default=5, help="От 1 до 100 результатов")
    options = parser.parse_args()
    options.channel = list(dict.fromkeys(login.strip().lower() for login in options.channel))
    if not 1 <= options.limit <= 100 or len(options.channel) > 100:
        parser.error("Допустимо до 100 результатов и до 100 каналов.")
    if any(not re.fullmatch(r"[a-z0-9_]{1,25}", login) for login in options.channel):
        parser.error("--channel принимает логин Twitch без URL.")

    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        parser.error("Задайте TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET для приложения Twitch.")
    try:
        streams = get_streams(client_id, client_secret, options)
    except HTTPError as error:
        print(f"Twitch отклонил запрос (HTTP {error.code}).", file=sys.stderr)
        return 1
    except (URLError, TimeoutError):
        print("Не удалось подключиться к Twitch.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    print(json.dumps(streams, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
