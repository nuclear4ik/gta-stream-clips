"""Prepare reaction jobs from local recordings without per-recording input."""

from fnmatch import fnmatchcase
from functools import lru_cache
import json
import math
import os
from pathlib import Path
from statistics import median
import subprocess

import reaction
import moment_model
from captions import Caption, plain_text, srt_timestamp
from clips import FFMPEG, probe
from editing import Rectangle, get_crops, parse_crop


LANGUAGES = {"RU": "ru", "EN": "en", "ESP": "es"}
WINDOW_SECONDS = 120
WINDOW_COUNT = 4
MIN_CLIP_SECONDS = 25
MAX_CLIP_SECONDS = 50
MAX_SOURCE_SECONDS = 24 * 60 * 60
CAMERA_SAMPLE_POSITIONS = (.08, .24, .45, .68, .88)
MIN_CAMERA_HITS = 4


def enabled():
    return os.environ.get("STREAMCUT_AUTOCUT") == "1"


def analysis_windows(duration):
    duration = min(duration, MAX_SOURCE_SECONDS)
    if duration <= WINDOW_COUNT * WINDOW_SECONDS:
        return [(start, min(WINDOW_SECONDS, duration - start))
                for start in range(0, math.ceil(duration), WINDOW_SECONDS) if start < duration]
    partition = duration / WINDOW_COUNT
    return [((index + .5) * partition - WINDOW_SECONDS / 2, WINDOW_SECONDS)
            for index in range(WINDOW_COUNT)]


def configured_layout(video, title, recording):
    raw = os.environ.get("STREAMCUT_AUTOCUT_LAYOUTS", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.stat().st_size > 32_768:
        raise ValueError("Файл шаблонов автомонтажа должен занимать не более 32 КиБ.")
    rules = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rules, list) or len(rules) > 100:
        raise ValueError("Файл шаблонов автомонтажа должен содержать массив до 100 правил.")
    selectors = {"filename": video.name, "title": title}
    for rule in rules:
        if (not isinstance(rule, dict) or not rule.keys() & selectors.keys()
                or not rule.keys() <= selectors.keys() | reaction.CROP_FIELDS.keys()
                or "facecam" not in rule
                or any(not isinstance(value, str) or not value.strip() or len(value) > 140
                       for value in rule.values())):
            raise ValueError("Шаблон автомонтажа требует facecam и шаблон filename или title.")
        if all(fnmatchcase(value.casefold(), rule[key].casefold())
               for key, value in selectors.items() if key in rule):
            layout = {key: rule[key] for key in reaction.CROP_FIELDS if key in rule}
            # Validate optional focus and actual media dimensions before using a saved layout.
            reaction.normalize_profile({"headline": "Reaction layout profile", **layout})
            get_crops(recording.width, recording.height, parse_crop(layout["facecam"]),
                      parse_crop(layout["gameplay"]) if "gameplay" in layout else None)
            return layout
    return None


@lru_cache(maxsize=2)
def whisper_model(name):
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise ValueError("Для автомонтажа установите faster-whisper==1.2.1 в окружение Streamcut.") from error
    return WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=min(4, os.cpu_count() or 1))


def decode(video, start, stream, options):
    result = subprocess.run([
        *FFMPEG, "-ss", f"{start:.3f}", "-i", str(video), "-map", f"0:{stream}", *options, "-"
    ], capture_output=True, timeout=160)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ValueError(f"Автомонтаж: FFmpeg не прочитал запись. {detail[-1][:240] if detail else ''}")
    return result.stdout


def audio_samples(video, recording, start, duration):
    import numpy as np

    raw = decode(video, start, recording.audio_index, [
        "-t", f"{duration:.3f}", "-ac", "1", "-ar", "16000", "-f", "f32le",
    ])
    return np.frombuffer(raw, dtype="<f4")


def transcription(model, samples, lang, start, duration, *, vad):
    segments, _ = model.transcribe(
        samples, language=lang, beam_size=5, vad_filter=vad, word_timestamps=True,
        vad_parameters={"min_silence_duration_ms": 450}, hallucination_silence_threshold=1.0,
        condition_on_previous_text=False,
    )
    captions = []
    for segment in segments:
        text = plain_text(segment.text)
        begin, end = max(0, segment.start), min(duration, segment.end)
        if text and end - begin >= .22 and any(character.isalpha() for character in text):
            captions.append(Caption(start + begin, start + end, text))
    return captions


def transcribe_recording(video, recording, model, lang):
    captions = []
    for start, duration in analysis_windows(recording.duration):
        samples = audio_samples(video, recording, start, duration)
        if samples.size < 8000:
            continue
        window = transcription(model, samples, lang, start, duration, vad=True)
        if sum(cue.end - cue.start for cue in window) < 2:
            # Quiet streamer microphones can be missed by the speech activity filter.
            retry = transcription(model, samples, lang, start, duration, vad=False)
            if sum(cue.end - cue.start for cue in retry) > sum(cue.end - cue.start for cue in window):
                window = retry
        captions.extend(window)
    if not captions:
        raise ValueError("Автомонтаж не нашёл речь с таймкодами в исходной дорожке.")
    return captions


def as_srt(captions):
    return "\n\n".join(
        f"{number}\n{srt_timestamp(cue.start)} --> {srt_timestamp(cue.end)}\n{cue.text}"
        for number, cue in enumerate(captions, 1)
    ) + "\n"


def edge_faces(boxes, width, height):
    # Pixels from normalized sample frames are converted to frame-relative boxes.
    result = []
    for x, y, w, h in boxes:
        center_x = (x + w / 2) / width
        if (center_x <= .23 or center_x >= .77) and .024 <= w / width <= .16 and .035 <= h / height <= .28:
            result.append((x / width, y / height, w / width, h / height))
    return result


def center(box):
    x, y, w, h = box
    return x + w / 2, y + h / 2


def nearby(first, second):
    x, y = center(first)
    a, b = center(second)
    return abs(x - a) <= .07 and abs(y - b) <= .10 and .60 <= first[2] / second[2] <= 1.67


def stable_face(frames):
    # Count independent sample frames rather than multiple detections in the same frame.
    groups = []
    for frame in frames:
        for seed in frame:
            neighboring = [next((box for box in boxes if nearby(seed, box)), None) for boxes in frames]
            candidates = [box for box in neighboring if box]
            if len(candidates) >= MIN_CAMERA_HITS:
                representative = tuple(median(values) for values in zip(*candidates))
                groups.append((representative, len(candidates)))
    if not groups:
        return None
    best, count = max(groups, key=lambda group: group[1])
    if any(support >= count - 1 and not nearby(best, other) for other, support in groups):
        return None
    return best


@lru_cache(maxsize=1)
def face_detector():
    try:
        import cv2
        classifier = cv2.CascadeClassifier
        model_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    except (ImportError, AttributeError) as error:
        raise reaction.MissingReactionProfile(
            "Установите opencv-python-headless>=4.8,<5 для определения камеры "
            "или добавьте шаблон STREAMCUT_AUTOCUT_LAYOUTS."
        ) from error
    detector = classifier(str(model_path))
    if detector.empty():
        raise ValueError("OpenCV не нашёл классификатор лиц.")
    return detector


def crop_string(box):
    return ",".join(str(value) for value in (box.x, box.y, box.width, box.height))


def detected_layout(video, recording, interval):
    import numpy as np

    detector = face_detector()
    width = max(2, min(960, recording.width) // 2 * 2)
    height = max(2, round(recording.height * width / recording.width / 2) * 2)
    start, end = interval["start_seconds"], interval["end_seconds"]
    frames = []
    for fraction in CAMERA_SAMPLE_POSITIONS:
        raw = decode(video, start + (end - start) * fraction, recording.video_index, [
            "-frames:v", "1", "-vf", f"scale={width}:{height},format=gray", "-pix_fmt", "gray",
            "-c:v", "rawvideo", "-threads:v", "1", "-f", "rawvideo",
        ])
        if len(raw) != width * height:
            continue
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
        faces = detector.detectMultiScale(frame, scaleFactor=1.1, minNeighbors=6, minSize=(14, 14))
        frames.append(edge_faces(faces, width, height))
    box = stable_face(frames)
    if box is None:
        return None
    x, y, w, h = box
    left, right = max(0, x - .88 * w), min(1, x + 1.88 * w)
    top, bottom = max(0, y - .42 * h), min(1, y + 1.64 * h)
    px0, px1 = round(left * recording.width), round(right * recording.width)
    py0, py1 = round(top * recording.height), round(bottom * recording.height)
    facecam = crop_string(Rectangle(px0, py0, px1 - px0, py1 - py0))
    cx, cy = center(box)
    # A face detection covers the host, while the original camera box may extend past it.
    # Use the opposite side of the source for gameplay to keep the whole overlay outside it.
    if cx < .5:
        cut = round(max(.22, right + .045) * recording.width)
        game = Rectangle(cut, 0, recording.width - cut, recording.height)
    else:
        cut = round(min(.78, left - .045) * recording.width)
        game = Rectangle(0, 0, cut, recording.height)
    try:
        get_crops(recording.width, recording.height, parse_crop(facecam), parse_crop(crop_string(game)))
    except ValueError:
        return None
    focus = f"{100 * (cx - left) / (right - left):.3f}%,{100 * (cy - top) / (bottom - top):.3f}%"
    gameplay_focus = f"{100 * (recording.width / 2 - game.x) / game.width:.3f}%,50%"
    return {"facecam": facecam, "gameplay": crop_string(game), "facecam_focus": focus,
            "gameplay_focus": gameplay_focus}


def compatible(existing, candidate, recording):
    width, height = recording.width, recording.height
    first = parse_crop(existing["facecam"]).resolve(width, height, "facecam")
    second = parse_crop(candidate["facecam"]).resolve(width, height, "facecam")
    overlap = (max(0, min(first.right, second.right) - max(first.x, second.x))
               * max(0, min(first.bottom, second.bottom) - max(first.y, second.y)))
    return overlap / (first.area + second.area - overlap) >= .60


def speech_language(video, recording, model, intervals):
    evidence = []
    for interval in intervals:
        start, end = interval["start_seconds"], interval["end_seconds"]
        samples = audio_samples(video, recording, start, end - start)
        if samples.size < 8000:
            return None
        code, confidence, _ = model.detect_language(audio=samples, language_detection_segments=2,
                                                    language_detection_threshold=.9)
        if not isinstance(code, str) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            return None
        evidence.append((code, confidence))
    if len({code for code, _ in evidence}) == 1:
        return {"code": evidence[0][0], "confidence": min(score for _, score in evidence)}
    return None


def prepare(video: Path, lang: str, title: str, limit: int) -> dict:
    if lang not in LANGUAGES or not 1 <= limit <= moment_model.MAX_MOMENTS:
        raise ValueError("Проверьте язык и количество мест для автомонтажа.")
    if not os.environ.get("STREAMCUT_OLLAMA_MODEL", "").strip():
        raise ValueError("Для автомонтажа укажите STREAMCUT_OLLAMA_MODEL и запустите Ollama.")
    try:
        recording = probe(video)
    except RuntimeError as error:
        raise ValueError(f"Не удалось прочитать исходник для автомонтажа. {str(error)[-240:]}") from error
    if recording.audio_index is None:
        raise ValueError("Для автомонтажа нужна оригинальная аудиодорожка с речью.")
    if recording.duration < MIN_CLIP_SECONDS:
        raise ValueError(f"Для автомонтажа нужна запись не короче {MIN_CLIP_SECONDS} секунд.")
    configured = configured_layout(video, title, recording)
    if configured is None:
        face_detector()
    name = os.environ.get("STREAMCUT_AUTOCUT_WHISPER_MODEL", "base").strip() or "base"
    model = whisper_model(name)
    captions = transcribe_recording(video, recording, model, LANGUAGES[lang])
    srt = as_srt(captions)
    moments = moment_model.select_moments(
        srt, lang, minimum=MIN_CLIP_SECONDS, maximum=MAX_CLIP_SECONDS, complete=True,
    )
    maximum = min(MAX_SOURCE_SECONDS, math.floor(recording.duration * 1000) / 1000)
    chosen, layout = [], configured
    for interval in moments:
        start, end = interval["start_seconds"], interval["end_seconds"]
        if not (0 <= start < end <= maximum and
                MIN_CLIP_SECONDS * 1000 <= round((end - start) * 1000) <= MAX_CLIP_SECONDS * 1000):
            continue
        if any(interval["start_seconds"] < old["end_seconds"] and old["start_seconds"] < interval["end_seconds"]
               for old in chosen):
            continue
        candidate = configured or detected_layout(video, recording, interval)
        if candidate is None or (layout is not None and not compatible(layout, candidate, recording)):
            continue
        layout = layout or candidate
        chosen.append(interval)
        if len(chosen) == limit:
            break
    if not chosen:
        raise reaction.MissingReactionProfile(
            "В выбранных моментах не удалось уверенно определить камеру. Добавьте шаблон STREAMCUT_AUTOCUT_LAYOUTS."
        )
    validated = reaction.normalize_profile({
        **layout, "headline": chosen[0]["headline"], "mode": "manual", "transcript": srt, "moments": chosen,
    })
    validated["speech_language"] = speech_language(video, recording, model, chosen)
    return validated
