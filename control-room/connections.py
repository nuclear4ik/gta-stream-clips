"""Status observations from the same routes used by platform requests."""

from datetime import datetime, timezone
import re

import transport


COUNTRY = re.compile(r"[A-Z]{2}")
COUNTRY_MISMATCH = re.compile(r"Страна выхода ([A-Z]{2}), ожидается ([A-Z]{2})\.")
BAD_STATUS = re.compile(r"API площадки недоступен \(HTTP ([1-5][0-9]{2})\)\.")
SAFE_FAILURES = frozenset({
    "Соединение не установлено. Проверьте маршрут и TLS.",
    "Не удалось подтвердить страну выхода.",
    "Нужен HTTP CONNECT через доверенный туннель.",
    "Ответ сервиса превысил допустимый размер.",
})


def observe(route: str, platform: str) -> dict:
    """Run a fresh, read-only check. Return no IP, credentials, or raw responses."""
    country = None
    if route not in transport.REGIONS:
        state, detail = "unconfigured", "Неизвестный маршрут."
    elif platform not in transport.API_HOSTS:
        state, detail = "unconfigured", "Неизвестная площадка."
    elif not transport.route_available(route):
        state, detail = "unconfigured", "Маршрут не настроен."
    else:
        try:
            result = transport.check_route(route, platform)
        except transport.ConnectionProblem as error:
            message = str(error)
            state, detail = "failed", "Маршрут не прошёл проверку."
            mismatch = COUNTRY_MISMATCH.fullmatch(message)
            if mismatch and mismatch[2] == route:
                country = mismatch[1]
                detail = f"Страна выхода {country}, ожидается {route}."
            elif match := BAD_STATUS.fullmatch(message):
                detail = f"API площадки недоступен (HTTP {match[1]})."
            elif message in SAFE_FAILURES:
                detail = message
        else:
            reported = result.get("country")
            if route == transport.GATEWAY:
                state, detail = "ok", "Доверенный шлюз проверен, API отвечает по HTTPS."
            elif not isinstance(reported, str) or not COUNTRY.fullmatch(reported):
                state, detail = "failed", "Не удалось подтвердить страну выхода."
            elif route != "server" and reported != route:
                country = reported
                state, detail = "failed", f"Страна выхода {country}, ожидается {route}."
            else:
                country = reported
                state, detail = "ok", "Маршрут проверен, API отвечает по HTTPS."

    return {
        "route": route,
        "platform": platform,
        "state": state,
        "country": country,
        "detail": detail,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
