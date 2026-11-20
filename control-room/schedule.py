"""Delivery windows are defined in the audience's civil time, stored in UTC."""
from datetime import datetime, time, timedelta, timezone
import os
from zoneinfo import ZoneInfo

UTC = timezone.utc
ZONES = {lang: ZoneInfo(name) for lang, name in {
    "RU": "Europe/Moscow", "EN": "America/New_York", "ESP": "Europe/Madrid",
}.items()}
DEFAULT_RELEASE_TIMES = tuple(time(hour) for hour in range(9, 19))
RU_RELEASE_TIMES = tuple(time(hour, minute) for hour in range(15, 20) for minute in (0, 30))
DAY_LIMIT = len(DEFAULT_RELEASE_TIMES)
RU_START = time(15)
RU_END = time(20)


def ru_subdivisions():
    """Prefer midpoints of wider gaps before filling smaller gaps."""
    gaps = [(minute, minute + 30) for minute in range(RU_START.hour * 60, RU_END.hour * 60, 30)]
    additional = []
    while gaps:
        smaller = []
        for left, right in gaps:
            if right - left > 1:
                middle = (left + right) // 2
                additional.append(time(*divmod(middle, 60)))
                smaller.extend(((left, middle), (middle, right)))
        gaps = smaller
    return tuple(additional)


RU_SUBDIVISIONS = ru_subdivisions()


def daily_limit(lang="RU"):
    if lang != "RU":
        return DAY_LIMIT
    value = os.environ.get("STREAMCUT_YOUTUBE_DAILY_LIMIT", str(DAY_LIMIT))
    if not value.isdecimal() or len(value) > 3 or not 1 <= int(value) <= 100:
        raise ValueError("STREAMCUT_YOUTUBE_DAILY_LIMIT должен быть от 1 до 100.")
    return int(value)


def release_times(lang):
    return RU_RELEASE_TIMES if lang == "RU" else DEFAULT_RELEASE_TIMES


def slot_spacing(lang, *, subdivided=False):
    if lang == "RU":
        return timedelta(minutes=1 if subdivided else 30)
    return timedelta(hours=1)


def in_release_window(instant, lang, *, subdivided=False):
    wall = instant.astimezone(ZONES[lang])
    if lang == "RU" and subdivided:
        return RU_START <= wall.time() < RU_END and not (wall.second or wall.microsecond)
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


def slot_is_available(candidate, lang, reserved):
    """Validate a previously assigned automatic slot against the current calendar."""
    start, end = civil_day(candidate, lang)
    spacing = slot_spacing(lang, subdivided=True)
    used = tuple(reserved)
    return (in_release_window(candidate, lang, subdivided=True)
            and sum(start <= instant < end for instant in used) < daily_limit(lang)
            and all(abs(candidate - instant) >= spacing for instant in used))


def available_slot(after, lang, reserved):
    zone = ZONES[lang]
    day = after.astimezone(zone).date()
    used = [instant.astimezone(zone) for instant in reserved]
    minimum = after.astimezone(UTC)
    limit = daily_limit(lang)
    windows = [(release_times(lang), slot_spacing(lang))]
    if lang == "RU":
        windows.append((RU_SUBDIVISIONS, slot_spacing(lang, subdivided=True)))
    while True:
        occupied_today = [instant.astimezone(UTC) for instant in used if instant.date() == day]
        if len(occupied_today) < limit:
            for times, spacing in windows:
                for release in times:
                    candidate = datetime.combine(day, release, zone).astimezone(UTC)
                    if candidate > minimum and all(abs(candidate - instant) >= spacing for instant in occupied_today):
                        return candidate
        day += timedelta(days=1)
