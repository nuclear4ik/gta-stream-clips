"""Delivery windows are defined in the audience's civil time, stored in UTC."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
ZONES = {lang: ZoneInfo(name) for lang, name in {
    "RU": "Europe/Moscow", "EN": "America/New_York", "ESP": "Europe/Madrid",
}.items()}
HOURS = range(9, 19)
DAY_LIMIT = len(HOURS)


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
    while True:
        if sum(instant.date() == day for instant in used) < DAY_LIMIT:
            for hour in HOURS:
                candidate = datetime.combine(day, time(hour), zone).astimezone(UTC)
                if (candidate > after and
                        all(abs((candidate - instant.astimezone(UTC)).total_seconds()) >= 3600 for instant in used)):
                    return candidate
        day += timedelta(days=1)
