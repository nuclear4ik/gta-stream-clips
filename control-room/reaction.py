"""Use the existing split editing mode for source recordings with a facecam."""

import argparse
from contextlib import contextmanager
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from captions import headline_words, parse_srt, plain_text
from editing import parse_crop, parse_focus

MAX_TRANSCRIPT = 512 * 1024
CROP_FIELDS = {"facecam": parse_crop, "gameplay": parse_crop,
               "facecam_focus": parse_focus, "gameplay_focus": parse_focus}


class MissingReactionProfile(ValueError):
    """The source should wait for the actual camera geometry."""


def profile_path(video):
    return video.with_suffix(".reaction.json")


def has_profile(video):
    profile = profile_path(video)
    return profile.is_file() and not profile.is_symlink()


def clean_headline(raw):
    if not isinstance(raw, str) or len(raw) > 100:
        raise ValueError("Заголовок должен содержать 3–5 слов и до 50 символов.")
    value = plain_text(raw)
    if len(value) > 50 or not 3 <= len(headline_words(value)) <= 5:
        raise ValueError("Заголовок должен содержать 3–5 слов и до 50 символов.")
    return value


def normalize_profile(data):
    if (not isinstance(data, dict) or not {"facecam", "headline"} <= data.keys() or
            not data.keys() <= CROP_FIELDS.keys() | {"headline", "transcript", "moments", "mode"}):
        raise ValueError("Укажите область веб-камеры и заголовок.")
    profile = {"headline": clean_headline(data["headline"])}
    for field, parser in CROP_FIELDS.items():
        if field not in data:
            continue
        value = data[field]
        if not isinstance(value, str) or len(value) > 100:
            raise ValueError("Проверьте координаты областей кадра.")
        if not value.strip() and field != "facecam":
            continue
        try:
            parser(value)
        except argparse.ArgumentTypeError as error:
            raise ValueError(str(error)) from error
        profile[field] = value.strip()
    mode = data.get("mode", "auto")
    if mode not in ("auto", "manual", "model"):
        raise ValueError("Выберите режим отбора моментов.")
    profile["mode"] = mode
    transcript = data.get("transcript", "")
    if not isinstance(transcript, str) or len(transcript.encode("utf-8")) > MAX_TRANSCRIPT:
        raise ValueError("Расшифровка SRT должна занимать до 512 КиБ.")
    if transcript.strip():
        cues = parse_srt(transcript)
        if not cues:
            raise ValueError("В SRT нет реплик с таймкодами.")
        profile["transcript"] = transcript
    elif mode != "auto":
        raise ValueError("Для точных моментов нужна расшифровка SRT.")
    if mode == "manual":
        moments = data.get("moments")
        if not isinstance(moments, list) or not 1 <= len(moments) <= 6:
            raise ValueError("Укажите от одного до шести интервалов.")
        profile["moments"] = []
        for moment in moments:
            if (not isinstance(moment, dict) or not {"start_seconds", "end_seconds"} <= moment.keys() or
                    not moment.keys() <= {"start_seconds", "end_seconds", "headline"}):
                raise ValueError("Для каждого интервала укажите начало и конец.")
            start, end = moment["start_seconds"], moment["end_seconds"]
            if (type(start) not in (int, float) or type(end) not in (int, float) or
                    not 0 <= start < end <= 86400 or not math.isfinite(end)):
                raise ValueError("Интервалы задаются в секундах от 0 до 86400, начало раньше конца.")
            if not any(start < cue.end and cue.start < end for cue in cues):
                raise ValueError("У каждого интервала должна быть реплика в расшифровке.")
            moment = {"start_seconds": start, "end_seconds": end,
                      **({"headline": clean_headline(moment["headline"])} if "headline" in moment else {})}
            profile["moments"].append(moment)
    return profile


def read_profile(video):
    path = profile_path(video)
    if path.stat().st_size > 8192:
        raise ValueError("Профиль реакции слишком большой.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.keys() <= CROP_FIELDS.keys() | {"headline"}:
        raise ValueError("Укажите в файле профиля координаты и заголовок.")
    return normalize_profile(data)


@contextmanager
def editor_arguments(video, *, required, transcribe, profile=None, moments=None):
    if profile is None and has_profile(video):
        profile = read_profile(video)
    if profile is None:
        if required:
            raise MissingReactionProfile(
                f"Укажите область веб-камеры в карточке источника или добавьте {profile_path(video).name}."
            )
        yield ["--layout", "crop", *(("--transcribe", "--burn-captions") if transcribe else ())]
        return

    args = ["--layout", "split", "--require-speech", "--burn-captions"]
    for field in (*CROP_FIELDS, "headline"):
        if field in profile:
            args.append(f"--{field.replace('_', '-')}={profile[field]}")
    with TemporaryDirectory(prefix="streamcut-edit-") as directory:
        srt = video.with_suffix(".srt")
        if transcript := profile.get("transcript"):
            srt = Path(directory) / "transcript.srt"
            srt.write_text(transcript, encoding="utf-8")
        if srt.is_file() and not srt.is_symlink():
            args.extend(("--srt", str(srt)))
        else:
            args.append("--transcribe")
        if moments is not None:
            ranges = Path(directory) / "moments.json"
            ranges.write_text(json.dumps({"moments": moments}, ensure_ascii=False), encoding="utf-8")
            args.extend(("--ranges", str(ranges)))
        yield args
