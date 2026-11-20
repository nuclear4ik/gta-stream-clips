"""Network checks and OAuth for the official platform APIs."""
import base64
from datetime import datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
import http.client
import ipaddress
import json
import os
from urllib.parse import unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

API_HOSTS = {"YouTube": "www.googleapis.com", "TikTok": "open.tiktokapis.com",
             "Reels": "graph.instagram.com"}
GATEWAY = "gateway"
REGIONS = {"server", GATEWAY, "RU", "US", "ES"}
OAUTH_HOST = "oauth2.googleapis.com"
GATEWAY_HOSTS = frozenset((*API_HOSTS.values(), OAUTH_HOST))
GATEWAY_REQUIRED_DETAIL = "Для загрузки YouTube выберите доверенный шлюз."
REDIRECTS = {301, 302, 303, 307, 308}
YOUTUBE_SCOPES = " ".join((
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
))
YOUTUBE_PROJECT_QUOTA_REASONS = ("dailyLimitExceeded", "quotaExceeded")
YOUTUBE_CAPACITY_REASONS = (
    *YOUTUBE_PROJECT_QUOTA_REASONS, "uploadLimitExceeded",
    "rateLimitExceeded", "userRateLimitExceeded", "tooManyRequests",
)


class ConnectionProblem(Exception):
    """A bounded diagnostic that can be displayed without exposing credentials."""


class RouteProblem(ConnectionProblem):
    """A failure before uploading any bytes, eligible for route recovery."""


class NetworkProblem(ConnectionProblem):
    """No usable HTTP response was received."""


class YouTubeCapacityProblem(ConnectionProblem):
    """A definitive rejection due to rate, account or API capacity."""

    def __init__(self, message, *, reason=None, retry_after_seconds=300):
        super().__init__(message)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds

    @property
    def project_wide(self):
        return self.reason in YOUTUBE_PROJECT_QUOTA_REASONS


def proxy_setting(region):
    if region == GATEWAY:
        return os.environ.get("STREAMCUT_UPLOAD_GATEWAY")
    return os.environ.get(f"STREAMCUT_PROXY_{region}")


def route_available(region):
    return region == "server" or region in REGIONS and bool(proxy_setting(region))


def youtube_gateway_required():
    return os.environ.get("STREAMCUT_YOUTUBE_REQUIRE_GATEWAY") == "1"


def youtube_write_route_allowed(region):
    return not youtube_gateway_required() or region == GATEWAY


def require_youtube_write_route(region):
    if not youtube_write_route_allowed(region):
        raise RouteProblem(GATEWAY_REQUIRED_DETAIL)
    if region == GATEWAY and not route_available(region):
        raise RouteProblem("Доверенный шлюз не настроен на сервере.")


def request(region, host, method, path, headers=None, body=None, timeout=15):
    if region not in REGIONS or region == GATEWAY and host not in GATEWAY_HOSTS:
        raise ConnectionProblem("Маршрут не поддерживает этот адрес.")
    proxy = proxy_setting(region) if region != "server" else None
    if region != "server" and not proxy:
        raise NetworkProblem("Маршрут не настроен на сервере.")
    try:
        if region != "server":
            url = urlsplit(proxy)
            if url.scheme != "http" or not url.hostname or url.path not in ("", "/") or url.query or url.fragment:
                raise NetworkProblem("Нужен HTTP CONNECT через доверенный туннель.")
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
        raise NetworkProblem("Соединение не установлено. Проверьте маршрут и TLS.") from error


def check_route(region, platform):
    if platform not in API_HOSTS:
        raise ConnectionProblem("Неизвестная площадка.")
    if region == GATEWAY:
        return {"country": None, "api_status": check_platform(region, platform)}
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
    return {"country": country, "api_status": check_platform(region, platform)}


def check_platform(region, platform):
    status, _, _ = request(region, API_HOSTS[platform], "GET", "/")
    if not 200 <= status < 500 or status in REDIRECTS or status in (407, 429, 451):
        raise ConnectionProblem(f"API площадки недоступен (HTTP {status}).")
    return status


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


def youtube_retry_delay(reason, headers):
    current = datetime.now(timezone.utc)
    retry_after = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if isinstance(retry_after, str) and retry_after:
        try:
            seconds = int(retry_after) if retry_after.isdecimal() else (
                parsedate_to_datetime(retry_after) - current).total_seconds()
            return max(1, min(172800, round(seconds)))
        except (TypeError, ValueError, OverflowError):
            pass
    if reason in YOUTUBE_PROJECT_QUOTA_REASONS:
        pacific = ZoneInfo("America/Los_Angeles")
        next_day = current.astimezone(pacific).date() + timedelta(days=1)
        resets = datetime.combine(next_day, time.min, tzinfo=pacific)
        return max(300, round((resets - current).total_seconds()) + 5)
    return 3600 if reason == "uploadLimitExceeded" else 300


def check_youtube_capacity(status, data, headers, *, retry_transient=False):
    if status in (400, 403, 429):
        try:
            errors = json.loads(data)["error"]["errors"]
            reasons = {entry["reason"] for entry in errors if isinstance(entry, dict)
                       and isinstance(entry.get("reason"), str)}
        except (ValueError, TypeError, KeyError):
            reasons = set()
        reason = next((item for item in YOUTUBE_CAPACITY_REASONS if item in reasons), None)
        if reason:
            raise YouTubeCapacityProblem(f"YouTube временно отклонил запрос ({reason}).", reason=reason,
                                         retry_after_seconds=youtube_retry_delay(reason, headers))
    if retry_transient and (status == 429 or 500 <= status < 600):
        raise YouTubeCapacityProblem(f"YouTube временно недоступен (HTTP {status}).",
                                     retry_after_seconds=youtube_retry_delay(None, headers))


def youtube_auth_response(status, headers, data, action):
    check_youtube_capacity(status, data, headers)
    if status == 429 or 500 <= status < 600:
        raise RouteProblem(f"{action}: сервис временно недоступен (HTTP {status}).")
    return json_response(status, data, action)


def token_request(region, **fields):
    if not youtube_configured():
        raise ConnectionProblem("На сервере не настроен OAuth YouTube.")
    body = urlencode({**fields, "client_id": os.environ["STREAMCUT_GOOGLE_CLIENT_ID"],
                      "client_secret": os.environ["STREAMCUT_GOOGLE_CLIENT_SECRET"]}).encode()
    status, headers, data = request(region, OAUTH_HOST, "POST", "/token",
                                    {"Content-Type": "application/x-www-form-urlencoded"}, body)
    token = youtube_auth_response(status, headers, data, "Авторизация YouTube")
    if not isinstance(token.get("access_token"), str):
        raise ConnectionProblem("YouTube не выдал токен доступа.")
    return token


def confirm_channel(region, token, expected):
    headers = {"Authorization": f"Bearer {token}"}
    status, response_headers, data = request(region, API_HOSTS["YouTube"], "GET",
                                             "/youtube/v3/channels?part=id&mine=true", headers)
    channels = youtube_auth_response(status, response_headers, data, "Проверка канала").get("items", [])
    if not isinstance(channels, list) or not any(isinstance(item, dict) and item.get("id") == expected
                                                for item in channels):
        raise ConnectionProblem("Подключён другой канал YouTube. Выберите нужный канал при входе.")


def prepare_upload(account):
    try:
        check_route(account["route"], account["platform"])
    except ConnectionProblem as error:
        raise RouteProblem(str(error)) from error
    try:
        token = token_request(account["route"], grant_type="refresh_token",
                              refresh_token=account["refresh_token"])["access_token"]
        confirm_channel(account["route"], token, account["target"])
    except NetworkProblem as error:
        raise RouteProblem(str(error)) from error
    return token


def verify_youtube_write_route(region, token, expected_channel):
    require_youtube_write_route(region)
    if region != GATEWAY:
        return
    if not expected_channel:
        raise ConnectionProblem("Проверьте ID канала YouTube перед загрузкой.")
    try:
        # A successful authenticated request proves both gateway reachability and account identity.
        confirm_channel(region, token, expected_channel)
    except NetworkProblem as error:
        raise RouteProblem(str(error)) from error
