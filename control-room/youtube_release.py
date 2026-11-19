"""YouTube publication and schedule queries over the authenticated route."""

from datetime import datetime, timezone
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

import transport


HOST = transport.API_HOSTS["YouTube"]
UPLOAD_PATH = "/upload/youtube/v3/videos"
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
LANGUAGES = {"RU": "ru", "EN": "en", "ESP": "es"}
MUTABLE_STATUS = ("license", "embeddable", "publicStatsViewable", "selfDeclaredMadeForKids", "containsSyntheticMedia")
SlotUnavailable = transport.YouTubeCapacityProblem


class PublishUncertain(transport.ConnectionProblem):
    """Media may have been accepted, but no usable video ID was confirmed."""


class UploadRejected(transport.ConnectionProblem):
    """The upload can be retried after correcting the settings or route."""


class ScheduleUncertain(transport.ConnectionProblem):
    """A schedule update needs reconciliation with the current YouTube status."""


def utc_datetime(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def future_publication(value):
    result = utc_datetime(value)
    if result is not None:
        result = result.replace(microsecond=0)
    if result is None or result <= datetime.now(timezone.utc):
        raise UploadRejected("Время публикации YouTube должно быть в будущем.")
    return result


def clean_text(value):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value.translate({60: "‹", 62: "›"})).strip()


def youtube_tags(tags):
    result = []
    length = 0
    for raw in tags:
        tag = " ".join(clean_text(raw).lstrip("#").split())
        if not tag or tag in result:
            continue
        # Commas and the implicit quotes around a multiword tag count toward the YouTube limit.
        cost = len(tag) + (2 if " " in tag else 0) + bool(result)
        if length + cost <= 500:
            result.append(tag)
            length += cost
    return result


def upload_metadata(title, description, tags, lang, publish_at, license, made_for_kids):
    title = " ".join(clean_text(title).split())[:100]
    if not title:
        raise UploadRejected("Укажите заголовок видео для YouTube.")
    if lang not in LANGUAGES or license not in ("youtube", "creativeCommon") or type(made_for_kids) is not bool:
        raise UploadRejected("Проверьте язык, лицензию и отметку о детской аудитории.")
    snippet = {
        "title": title,
        "description": clean_text(description).encode("utf-8")[:5000].decode("utf-8", errors="ignore"),
        "tags": youtube_tags(tags),
        "categoryId": "20",
        "defaultLanguage": LANGUAGES[lang],
    }
    status = {"privacyStatus": "private", "license": license, "selfDeclaredMadeForKids": made_for_kids}
    if publish_at is not None:
        status["publishAt"] = future_publication(publish_at).isoformat(timespec="seconds").replace("+00:00", "Z")
    return json.dumps({"snippet": snippet, "status": status}, ensure_ascii=False).encode()


def session_path(response_headers):
    try:
        location = next((value for key, value in response_headers.items() if key.lower() == "location"), "")
        url = urlsplit(location)
        query = parse_qs(url.query, max_num_fields=40)
    except (AttributeError, ValueError):
        return None
    if (url.scheme != "https" or url.netloc != HOST or url.path != UPLOAD_PATH or url.fragment
            or len(query.get("upload_id", [])) != 1):
        return None
    return url.path + "?" + url.query


def verify_write_route(route, token, expected_channel):
    try:
        transport.verify_youtube_write_route(route, token, expected_channel)
    except (transport.RouteProblem, SlotUnavailable):
        raise
    except transport.ConnectionProblem as error:
        raise UploadRejected(str(error)) from error


def upload_result_id(status, body):
    try:
        identifier = transport.json_response(status, body, "Загрузка YouTube").get("id")
    except transport.ConnectionProblem:
        return None
    return identifier if isinstance(identifier, str) and VIDEO_ID.fullmatch(identifier) else None


def check_upload_result(route, auth, session, size):
    # Query the existing resumable session without sending the media again.
    headers = {**auth, "Content-Length": "0", "Content-Range": f"bytes */{size}"}
    try:
        transport.require_youtube_write_route(route)
        status, _, body = transport.request(route, HOST, "PUT", session, headers)
    except transport.ConnectionProblem:
        return None
    return upload_result_id(status, body)


def upload(route, token, *, title, description, tags, lang, path, publish_at=None,
           license="youtube", made_for_kids=False, expected_channel=None):
    body = upload_metadata(title, description, tags, lang, publish_at, license, made_for_kids)
    try:
        media = path.open("rb")
    except OSError as error:
        raise UploadRejected("Файл MP4 недоступен.") from error
    auth = {"Authorization": f"Bearer {token}"}
    with media:
        media.seek(0, 2)
        size = media.tell()
        media.seek(0)
        if not size:
            raise UploadRejected("Файл MP4 пуст.")
        verify_write_route(route, token, expected_channel)
        headers = {**auth, "Content-Type": "application/json",
                   "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)}
        try:
            code, response_headers, data = transport.request(
                route, HOST, "POST", UPLOAD_PATH + "?uploadType=resumable&part=snippet,status&notifySubscribers=false",
                headers, body)
        except transport.NetworkProblem as error:
            raise transport.RouteProblem(str(error)) from error
        except transport.ConnectionProblem as error:
            raise UploadRejected(str(error)) from error
        transport.check_youtube_capacity(code, data, response_headers, retry_transient=True)
        session = session_path(response_headers) if code in (200, 201) else None
        if session is None:
            raise UploadRejected(f"YouTube не открыл сессию загрузки (HTTP {code}).")
        verify_write_route(route, token, expected_channel)
        if publish_at is not None:
            future_publication(publish_at)
        headers = {**auth, "Content-Type": "video/mp4", "Content-Length": str(size)}
        try:
            code, response_headers, body = transport.request(route, HOST, "PUT", session, headers, media, timeout=120)
        except (OSError, transport.ConnectionProblem):
            code = None
    identifier = None
    if code is not None:
        transport.check_youtube_capacity(code, body, response_headers)
        identifier = upload_result_id(code, body)
    if identifier is None and (code is None or 200 <= code < 300 or code == 308 or code >= 500):
        identifier = check_upload_result(route, auth, session, size)
    if identifier is None:
        raise PublishUncertain("YouTube не подтвердил ID видео. Проверьте YouTube Studio.")
    return identifier


def read(route, token, resource, **query):
    status, headers, body = transport.request(
        route, HOST, "GET", f"/youtube/v3/{resource}?" + urlencode(query),
        {"Authorization": f"Bearer {token}"})
    transport.check_youtube_capacity(status, body, headers, retry_transient=True)
    return transport.json_response(status, body, "Проверка расписания YouTube")


def items(payload):
    rows = payload.get("items", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise transport.ConnectionProblem("YouTube вернул некорректный список видео.")
    return rows


def object_field(payload, name, *, required=False):
    value = payload.get(name)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise transport.ConnectionProblem("YouTube вернул неполные данные о видео или канале.")
    return value


def publication_status(route, token, video_ids):
    """Return trusted status fields keyed by requested video ID."""
    ids = list(dict.fromkeys(value for value in video_ids if isinstance(value, str) and VIDEO_ID.fullmatch(value)))
    result = {}
    for offset in range(0, len(ids), 50):
        batch = ids[offset:offset + 50]
        entry = read(route, token, "videos", part="status,contentDetails,processingDetails",
                     id=",".join(batch), fields="items(id,status,contentDetails/contentRating/ytRating,processingDetails)")
        for row in items(entry):
            if row.get("id") not in batch:
                continue
            status = object_field(row, "status", required=True)
            if (status.get("privacyStatus") not in ("private", "public", "unlisted") or
                    (status.get("publishAt") is not None and utc_datetime(status["publishAt"]) is None)):
                raise transport.ConnectionProblem("YouTube вернул неполный статус публикации.")
            rating = object_field(object_field(row, "contentDetails"), "contentRating")
            processing = object_field(row, "processingDetails")
            result[row["id"]] = {
                **status,
                **{key: processing[key] for key in ("processingStatus", "processingFailureReason") if key in processing},
                "ytRating": rating.get("ytRating"),
            }
    return result


def scheduled_entries(route, token, channel_id, limit):
    """Scan the authenticated channel's uploads for pending YouTube schedules."""
    if type(limit) is not int or limit < 1:
        raise ValueError("Лимит расписания должен быть положительным.")
    me = read(route, token, "channels", part="contentDetails", mine="true",
              fields="items(id,contentDetails/relatedPlaylists/uploads)")
    playlist = next((
        object_field(object_field(row, "contentDetails"), "relatedPlaylists").get("uploads")
        for row in items(me) if row.get("id") == channel_id
    ), None)
    if not isinstance(playlist, str) or not playlist:
        raise transport.ConnectionProblem("YouTube не подтвердил канал и его список загрузок.")
    result, page, seen_pages = {}, {}, set()
    current = datetime.now(timezone.utc)
    while True:
        listing = read(route, token, "playlistItems", part="contentDetails", playlistId=playlist,
                       maxResults=50, fields="items/contentDetails/videoId,nextPageToken", **page)
        ids = [object_field(row, "contentDetails", required=True).get("videoId") for row in items(listing)]
        if any(not isinstance(video, str) or not VIDEO_ID.fullmatch(video) for video in ids):
            raise transport.ConnectionProblem("YouTube вернул неполный список загрузок.")
        for video, status in publication_status(route, token, ids).items():
            stamp = utc_datetime(status.get("publishAt"))
            if (status.get("privacyStatus") == "private" and stamp and stamp > current
                    and status.get("uploadStatus") not in ("failed", "rejected", "deleted")):
                result[video] = stamp.isoformat(timespec="seconds")
        next_page = listing.get("nextPageToken")
        if len(result) >= limit or next_page in (None, ""):
            return result
        if not isinstance(next_page, str) or next_page in seen_pages:
            raise transport.ConnectionProblem("YouTube вернул некорректную страницу списка загрузок.")
        seen_pages.add(next_page)
        page = {"pageToken": next_page}


def reschedule(route, token, video_id, publish_at, *, expected_channel=None):
    future_publication(publish_at)
    transport.require_youtube_write_route(route)
    status = publication_status(route, token, [video_id]).get(video_id, {})
    original = utc_datetime(status.get("publishAt"))
    if status.get("privacyStatus") != "private" or original is None or original <= datetime.now(timezone.utc):
        raise UploadRejected("Перенести можно неопубликованный ролик с будущей датой в YouTube.")
    verify_write_route(route, token, expected_channel)
    planned = future_publication(publish_at)
    if original <= datetime.now(timezone.utc):
        raise UploadRejected("Прежнее время публикации уже наступило. Проверьте YouTube Studio.")
    fields = {key: status[key] for key in MUTABLE_STATUS if key in status}
    fields.update(privacyStatus="private", publishAt=planned.isoformat().replace("+00:00", "Z"))
    body = json.dumps({"id": video_id, "status": fields}).encode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        code, response_headers, data = transport.request(route, HOST, "PUT", "/youtube/v3/videos?part=status", headers, body)
    except transport.ConnectionProblem as error:
        raise ScheduleUncertain("YouTube не подтвердил перенос. Проверьте текущее расписание.") from error
    transport.check_youtube_capacity(code, data, response_headers)
    if 400 <= code < 500:
        raise UploadRejected(f"YouTube отклонил перенос (HTTP {code}). Проверьте доступ OAuth и дату.")
    try:
        updated = transport.json_response(code, data, "Перенос YouTube").get("status", {})
        if updated.get("privacyStatus") != "private" or utc_datetime(updated.get("publishAt")) != planned:
            raise ValueError("YouTube omitted or changed the schedule")
    except (AttributeError, transport.ConnectionProblem, ValueError) as error:
        raise ScheduleUncertain("YouTube не подтвердил новую дату. Проверьте текущее расписание.") from error
