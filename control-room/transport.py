"""Network checks and private uploads via the official YouTube API."""
import base64
import http.client
import ipaddress
import json
import os
from urllib.parse import unquote, urlencode, urlsplit

API_HOSTS = {"YouTube": "www.googleapis.com", "TikTok": "open.tiktokapis.com",
             "Reels": "graph.instagram.com"}
REGIONS = {"server", "RU", "US", "ES"}
REDIRECTS = {301, 302, 303, 307, 308}
YOUTUBE_SCOPES = " ".join((
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
))


class ConnectionProblem(Exception):
    """A bounded diagnostic that can be displayed without exposing credentials."""


class RouteProblem(ConnectionProblem):
    """A failure before uploading any bytes, eligible for route recovery."""


def route_available(region):
    return region == "server" or bool(os.environ.get(f"STREAMCUT_PROXY_{region}"))


def request(region, host, method, path, headers=None, body=None, timeout=15):
    if region not in REGIONS or not route_available(region):
        raise ConnectionProblem("Маршрут не настроен на сервере.")
    proxy = os.environ.get(f"STREAMCUT_PROXY_{region}") if region != "server" else None
    try:
        if proxy:
            url = urlsplit(proxy)
            if url.scheme != "http" or not url.hostname or url.path not in ("", "/") or url.query or url.fragment:
                raise ConnectionProblem("Нужен HTTP CONNECT через доверенный туннель.")
            connection = http.client.HTTPSConnection(url.hostname, url.port or 8080, timeout=timeout)
            tunnel_headers = {}
            if url.username is not None:
                auth = f"{unquote(url.username)}:{unquote(url.password or '')}"
                tunnel_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(auth.encode()).decode()
            connection.set_tunnel(host, 443, headers=tunnel_headers)
        else:
            connection = http.client.HTTPSConnection(host, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ConnectionProblem("Ответ сервиса превысил допустимый размер.")
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()
    except (OSError, http.client.HTTPException, ValueError) as error:
        raise ConnectionProblem("Соединение не установлено. Проверьте маршрут и TLS.") from error


def check_route(region, platform):
    if platform not in API_HOSTS:
        raise ConnectionProblem("Неизвестная площадка.")
    status, _, data = request(region, "www.cloudflare.com", "GET", "/cdn-cgi/trace")
    try:
        trace = dict(line.split("=", 1) for line in data.decode().splitlines() if "=" in line)
        ip = ipaddress.ip_address(trace["ip"])
        country = trace["loc"]
        if status != 200 or not ip.is_global or len(country) != 2 or not country.isalpha():
            raise ValueError
    except (ValueError, KeyError) as error:
        raise ConnectionProblem("Не удалось подтвердить страну выхода.") from error
    if region != "server" and country != region:
        raise ConnectionProblem(f"Страна выхода {country}, ожидается {region}.")
    status, _, _ = request(region, API_HOSTS[platform], "GET", "/")
    if not 200 <= status < 500 or status in REDIRECTS or status in (407, 429, 451):
        raise ConnectionProblem(f"API площадки недоступен (HTTP {status}).")
    return {"country": country, "api_status": status}


def youtube_configured():
    return bool(os.environ.get("STREAMCUT_GOOGLE_CLIENT_ID") and os.environ.get("STREAMCUT_GOOGLE_CLIENT_SECRET"))


def json_response(status, data, action):
    if not 200 <= status < 300:
        raise ConnectionProblem(f"{action}: HTTP {status}.")
    try:
        result = json.loads(data)
    except ValueError as error:
        raise ConnectionProblem(f"{action}: неожиданный ответ.") from error
    if not isinstance(result, dict):
        raise ConnectionProblem(f"{action}: неожиданный ответ.")
    return result


def token_request(region, **fields):
    if not youtube_configured():
        raise ConnectionProblem("На сервере не настроен OAuth YouTube.")
    body = urlencode({**fields, "client_id": os.environ["STREAMCUT_GOOGLE_CLIENT_ID"],
                      "client_secret": os.environ["STREAMCUT_GOOGLE_CLIENT_SECRET"]}).encode()
    status, _, data = request(region, "oauth2.googleapis.com", "POST", "/token",
                              {"Content-Type": "application/x-www-form-urlencoded"}, body)
    token = json_response(status, data, "Авторизация YouTube")
    if not isinstance(token.get("access_token"), str):
        raise ConnectionProblem("YouTube не выдал токен доступа.")
    return token


def confirm_channel(region, token, expected):
    headers = {"Authorization": f"Bearer {token}"}
    status, _, data = request(region, API_HOSTS["YouTube"], "GET",
                              "/youtube/v3/channels?part=id&mine=true", headers)
    channels = json_response(status, data, "Проверка канала").get("items", [])
    if not any(isinstance(item, dict) and item.get("id") == expected for item in channels):
        raise ConnectionProblem("Подключён другой канал YouTube. Выберите нужный канал при входе.")


def prepare_upload(account):
    try:
        check_route(account["route"], account["platform"])
    except ConnectionProblem as error:
        raise RouteProblem(str(error)) from error
    token = token_request(account["route"], grant_type="refresh_token",
                          refresh_token=account["refresh_token"])["access_token"]
    confirm_channel(account["route"], token, account["target"])
    return token


def upload_private(region, token, title, lang, path):
    size = path.stat().st_size
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)}
    language = {"RU": "ru", "EN": "en", "ESP": "es"}
    metadata = json.dumps({"snippet": {"title": title[:100], "categoryId": "20",
                                        "defaultLanguage": language[lang]},
                           "status": {"privacyStatus": "private"}}, ensure_ascii=False).encode()
    status, reply, _ = request(region, API_HOSTS["YouTube"], "POST",
                               "/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                               headers, metadata)
    location = urlsplit(next((v for k, v in reply.items() if k.lower() == "location"), ""))
    if status not in (200, 201) or location.scheme != "https" or location.netloc != API_HOSTS["YouTube"] or not location.path.startswith("/upload/"):
        raise ConnectionProblem(f"YouTube не открыл сессию загрузки (HTTP {status}).")
    with path.open("rb") as video:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                   "Content-Length": str(size)}
        target = location.path + ("?" + location.query if location.query else "")
        status, _, data = request(region, API_HOSTS["YouTube"], "PUT", target, headers, video, timeout=120)
    result = json_response(status, data, "Загрузка YouTube")
    if not result.get("id"):
        raise ConnectionProblem("YouTube не подтвердил идентификатор видео.")
    return result["id"]
