"""Prepare reaction jobs from local recordings without per-recording input."""

from concurrent.futures import ThreadPoolExecutor
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
WINDOW_CONTEXT_SECONDS = 6
MIN_CLIP_SECONDS = 25
MAX_CLIP_SECONDS = 50
MAX_SOURCE_SECONDS = 24 * 60 * 60
CAMERA_SAMPLE_POSITIONS = (.08, .24, .45, .68, .88)
MIN_CAMERA_HITS = 4
MIN_INSET_FACE_HITS = 2


def enabled():
    return os.environ.get("STREAMCUT_AUTOCUT") == "1"


def analysis_windows(duration):
    if not math.isfinite(duration) or not 0 < duration <= MAX_SOURCE_SECONDS:
        raise ValueError("Автомонтаж поддерживает исходные записи длительностью до 24 часов.")
    return [(start, min(WINDOW_SECONDS, duration - start))
            for start in range(0, math.ceil(duration), WINDOW_SECONDS) if start < duration]


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
    chunks = iter(analysis_windows(recording.duration))
    with ThreadPoolExecutor(max_workers=1) as decoder:
        def read_chunk(chunk):
            begin, duration = chunk
            start = max(0, begin - WINDOW_CONTEXT_SECONDS)
            length = min(recording.duration, begin + duration + WINDOW_CONTEXT_SECONDS) - start
            return begin, duration, start, length, decoder.submit(audio_samples, video, recording, start, length)

        current = read_chunk(next(chunks))
        while current is not None:
            begin, duration, start, length, audio = current
            samples = audio.result()
            following = next(chunks, None)
            current = read_chunk(following) if following is not None else None
            if samples.size < 8000:
                continue
            window = transcription(model, samples, lang, start, length, vad=True)
            spoken = sum(cue.end - cue.start for cue in window)
            if spoken < 2:
                # Quiet streamer microphones can be missed by the speech activity filter.
                retry = transcription(model, samples, lang, start, length, vad=False)
                if sum(cue.end - cue.start for cue in retry) > spoken:
                    window = retry
            # Context on both sides preserves utterances spanning a decode boundary.
            captions.extend(cue for cue in window if begin <= (cue.start + cue.end) / 2 < begin + duration)
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


def stable_face(frames, *, minimum_hits=MIN_CAMERA_HITS):
    # Count independent sample frames rather than multiple detections in the same frame.
    groups = []
    for frame in frames:
        for seed in frame:
            neighboring = [next((box for box in boxes if nearby(seed, box)), None) for boxes in frames]
            candidates = [box for box in neighboring if box]
            if len(candidates) >= minimum_hits:
                representative = tuple(median(values) for values in zip(*candidates))
                groups.append((representative, len(candidates)))
    if not groups:
        return None
    best, count = max(groups, key=lambda group: group[1])
    if any(support >= count - 1 and not nearby(best, other) for other, support in groups):
        return None
    return best


@lru_cache(maxsize=2)
def face_detector(*, profile=False):
    try:
        import cv2
        classifier = cv2.CascadeClassifier
        cascade = "profileface" if profile else "frontalface_default"
        model_path = Path(cv2.data.haarcascades) / f"haarcascade_{cascade}.xml"
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


def corner_rectangle(frames):
    """Find the two inner edges of an inset anchored to the upper-left corner."""
    import numpy as np

    _, height, width, _ = frames.shape
    if min(width, height) < 120:
        return None
    corner = frames[:, :int(height * .67), :int(width * .57)]
    vertical = np.abs(corner[:, :, 1:] - corner[:, :, :-1]).max(axis=-1)
    horizontal = np.abs(corner[:, 1:] - corner[:, :-1]).max(axis=-1)
    # Median RGB contrast suppresses brief gameplay edges and retains camera boundaries.
    col = np.pad(np.clip(np.median(vertical, axis=0) / 32, 0, 1).cumsum(axis=0), ((1, 0), (0, 0)))
    row = np.pad(np.clip(np.median(horizontal, axis=0) / 32, 0, 1).cumsum(axis=1), ((0, 0), (1, 0)))
    xs = np.arange(int(width * .11), int(width * .42))
    ys = np.arange(int(height * .13), int(height * .49))[:, None]
    vertical_inside = col[ys - 3, xs - 1] / (ys - 3)
    horizontal_inside = row[ys - 1, xs - 3] / (xs - 3)
    outside_y = np.minimum(len(col) - 1, ys + np.maximum(18, ys // 3))
    outside_x = np.minimum(row.shape[1] - 1, xs + np.maximum(18, xs // 3))
    vertical_outside = (col[outside_y, xs - 1] - col[ys + 4, xs - 1]) / (outside_y - ys - 4)
    horizontal_outside = (row[ys - 1, outside_x] - row[ys - 1, xs + 4]) / (outside_x - xs - 4)
    vertical_change = vertical_inside - vertical_outside
    horizontal_change = horizontal_inside - horizontal_outside
    ratio = xs / ys
    viable = ((vertical_inside > .34) & (horizontal_inside > .34) & (vertical_change > .15)
              & (horizontal_change > .15) & (ratio > .75) & (ratio < 2.9))
    # Both strong edges should end near their intersection, not continue into gameplay.
    scores = np.where(viable, .5 * (vertical_inside + horizontal_inside)
                      + .3 * (vertical_change + horizontal_change), 0)
    y, x = np.unravel_index(scores.argmax(), scores.shape)
    if scores[y, x] > 0:
        return Rectangle(0, 0, int(xs[x]), int(ys[y, 0]))
    return None


def inset_rectangles(frames):
    import numpy as np

    if len(frames) < MIN_CAMERA_HITS:
        return []
    pixels = np.stack(frames, dtype=np.int16)
    _, height, width, _ = pixels.shape
    candidates = []
    for bottom in (False, True):
        for right in (False, True):
            oriented = pixels[:, ::-1 if bottom else 1, ::-1 if right else 1]
            if box := corner_rectangle(oriented):
                candidates.append(Rectangle(width - box.width if right else 0,
                                            height - box.height if bottom else 0, box.width, box.height))
    return candidates


def inset_face(frames, camera, detector):
    import cv2

    profile = face_detector(profile=True)
    contrast = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(6, 6))
    options = {"scaleFactor": 1.08, "minNeighbors": 3, "minSize": (22, 22)}
    faces_by_frame = []
    for frame in frames:
        region = frame[camera.y:camera.bottom, camera.x:camera.right]
        enhanced = contrast.apply(region)
        faces = detector.detectMultiScale(region, **options)
        if not len(faces):
            faces = detector.detectMultiScale(enhanced, **options)
        if not len(faces):
            faces = profile.detectMultiScale(enhanced, **options)
        if not len(faces):
            faces = [(camera.width - x - w, y, w, h)
                     for x, y, w, h in profile.detectMultiScale(cv2.flip(enhanced, 1), **options)]
        faces_by_frame.append(edge_faces(((x + camera.x, y + camera.y, w, h) for x, y, w, h in faces),
                                         frame.shape[1], frame.shape[0]))
    return stable_face(faces_by_frame, minimum_hits=MIN_INSET_FACE_HITS)


def detected_layout(video, recording, interval):
    import cv2
    import numpy as np

    detector = face_detector()
    width = max(2, min(960, recording.width) // 2 * 2)
    height = max(2, round(recording.height * width / recording.width / 2) * 2)
    start, end = interval["start_seconds"], interval["end_seconds"]
    images = []
    for fraction in CAMERA_SAMPLE_POSITIONS:
        raw = decode(video, start + (end - start) * fraction, recording.video_index, [
            "-frames:v", "1", "-vf", f"scale={width}:{height},format=rgb24", "-pix_fmt", "rgb24",
            "-c:v", "rawvideo", "-threads:v", "1", "-f", "rawvideo",
        ])
        if len(raw) == width * height * 3:
            images.append(np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3))
    grays = [cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY) for frame in images]
    insets = [(camera, box) for camera in inset_rectangles(images) if (box := inset_face(grays, camera, detector))]
    if len(insets) > 1:
        return None
    if insets:
        camera, box = insets[0]
        left, right = camera.x / width, camera.right / width
        top, bottom = camera.y / height, camera.bottom / height
    else:
        box = stable_face([edge_faces(detector.detectMultiScale(
            frame, scaleFactor=1.1, minNeighbors=6, minSize=(14, 14)), width, height) for frame in grays])
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


def speech_languages(video, recording, model, intervals):
    evidence = []
    for interval in intervals:
        start, end = interval["start_seconds"], interval["end_seconds"]
        samples = audio_samples(video, recording, start, end - start)
        if samples.size < 8000:
            evidence.append(None)
            continue
        code, confidence, _ = model.detect_language(audio=samples, language_detection_segments=2,
                                                    language_detection_threshold=.9)
        valid = isinstance(code, str) and math.isfinite(confidence) and 0 <= confidence <= 1
        evidence.append({"code": code, "confidence": confidence} if valid else None)
    return evidence


def agreed_language(evidence):
    if all(evidence) and len({item["code"] for item in evidence}) == 1:
        return {"code": evidence[0]["code"], "confidence": min(item["confidence"] for item in evidence)}
    return None


def prepare(video: Path, lang: str, title: str, limit: int) -> dict:
    if lang not in LANGUAGES or not 1 <= limit <= moment_model.MAX_ARCHIVE_MOMENTS:
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
        limit=moment_model.MAX_ARCHIVE_MOMENTS, archive=True,
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
    }, automatic=True)
    evidence = speech_languages(video, recording, model, chosen)
    validated["speech_languages"] = evidence
    validated["speech_language"] = agreed_language(evidence)
    return validated
