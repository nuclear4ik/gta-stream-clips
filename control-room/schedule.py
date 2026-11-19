"""Delivery windows are defined in the audience's civil time, stored in UTC."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
ZONES = {lang: ZoneInfo(name) for lang, name in {
    "RU": "Europe/Moscow", "EN": "America/New_York", "ESP": "Europe/Madrid",
}.items()}
DEFAULT_RELEASE_TIMES = tuple(time(hour) for hour in range(9, 19))
RU_RELEASE_TIMES = tuple(time(hour, minute) for hour in range(15, 20) for minute in (0, 30))
DAY_LIMIT = len(DEFAULT_RELEASE_TIMES)


def release_times(lang):
    return RU_RELEASE_TIMES if lang == "RU" else DEFAULT_RELEASE_TIMES


def slot_spacing(lang):
    return timedelta(minutes=30 if lang == "RU" else 60)


def in_release_window(instant, lang):
    wall = instant.astimezone(ZONES[lang])
    return wall.time() in release_times(lang)


def local_time(value, lang):
    try:
        wall = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValueError("Укажите дату и местное время.") from None
    possibilities = set()
    for fold in (0, 1):
        utc = wall.replace(tzinfo=ZONES[lang], fold=fold).astimezone(UTC)
        if utc.astimezone(ZONES[lang]).replace(tzinfo=None) == wall:
            possibilities.add(utc)
    if len(possibilities) != 1:
        raise ValueError("Это время неоднозначно при смене часового пояса. Выберите другой час.")
    return possibilities.pop()


def civil_day(instant, lang):
    zone = ZONES[lang]
    day = instant.astimezone(zone).date()
    start = datetime.combine(day, time.min, zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def available_slot(after, lang, reserved):
    zone = ZONES[lang]
    day = after.astimezone(zone).date()
    used = [instant.astimezone(zone) for instant in reserved]
    spacing = slot_spacing(lang)
    while True:
        if sum(instant.date() == day for instant in used) < DAY_LIMIT:
            for release in release_times(lang):
                candidate = datetime.combine(day, release, zone).astimezone(UTC)
                if (candidate > after and
                        all(abs(candidate - instant.astimezone(UTC)) >= spacing for instant in used)):
                    return candidate
        day += timedelta(days=1)
