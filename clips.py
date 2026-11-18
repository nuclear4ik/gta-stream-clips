#!/usr/bin/env python3
"""Find and export highlights from a local stream recording."""

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

from captions import (CAPTION_COLORS, caption_position, choose_headline, follow_edit,
                      from_transcription, headline_words, plain_text, read_srt, write_ass, write_srt)
from editing import composition, get_crops, parse_crop, parse_focus, plan_edit


SOUND_KEY = "lavfi.astats.Overall.RMS_level"
MOTION_KEY = "lavfi.signalstats.YAVG"
SECOND_PER_SAMPLE = 0.5
MAX_RANGES = 100
MAX_RANGES_BYTES = 64 * 1024
FFMPEG = [
    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
    "-filter_threads", "2", "-filter_complex_threads", "2",
]


@dataclass(frozen=True)
class Recording:
    duration: float
    video_index: int
    audio_index: int | None
    width: int
    height: int


@dataclass(frozen=True)
class Highlight:
    start: float
    end: float
    peak: float
    score: float | None
    cue: str
    headline: str | None = None


def execute(command, cwd=None):
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Код ошибки {result.returncode}")
    return result.stdout


def probe(source):
    data = json.loads(execute([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)
    ]))
    streams = data.get("streams", [])
    video = next((
        stream for stream in streams
        if stream["codec_type"] == "video" and not stream.get("disposition", {}).get("attached_pic")
    ), None)
    audio = next((stream for stream in streams if stream["codec_type"] == "audio"), None)
    if video is None:
        raise ValueError("Не найдена видеодорожка.")
    duration = video.get("duration") or data.get("format", {}).get("duration", "0")
    if duration == "N/A":
        duration = data.get("format", {}).get("duration", "0")
    duration = float(duration)
    if not math.isfinite(duration) or duration < 1:
        raise ValueError("Нужна запись с известной длительностью от одной секунды.")
    width, height = video["width"], video["height"]
    if any(abs(float(side.get("rotation", 0))) % 180 == 90 for side in video.get("side_data_list", [])):
        width, height = height, width
    return Recording(duration, video["index"], audio["index"] if audio else None, width, height)


def read_levels(source, stream_index, filter_option, media_filter, key, floor):
    output = execute([
        *FFMPEG, "-i", str(source), "-map", f"0:{stream_index}",
        filter_option, media_filter, "-f", "null", "-"
    ])
    timestamp = None
    samples = {}
    for line in output.splitlines():
        if "pts_time:" in line:
            timestamp = float(line.split("pts_time:", 1)[1].split()[0])
        elif timestamp is not None and line.startswith(f"{key}="):
            value = float(line.split("=", 1)[1])
            if math.isfinite(timestamp):
                index = max(0, round(timestamp / SECOND_PER_SAMPLE))
                samples[index] = max(floor, value) if math.isfinite(value) else floor
    return samples


def peaks(samples, minimum_spread):
    if not samples:
        return {}
    baseline = median(samples.values())
    spread = max(minimum_spread, median(abs(value - baseline) for value in samples.values()) * 1.4826)
    scores = {}
    for index, value in samples.items():
        level = max(0, (value - baseline) / spread)
        change = max(0, (value - samples.get(index - 1, value)) / spread)
        scores[index] = min(8, level) + 0.5 * min(6, change)
    return scores


def motion_levels(source, recording, crop):
    crop_filter = f"{crop.crop()}," if crop is not None else ""
    return peaks(read_levels(
        source, recording.video_index, "-vf",
        f"fps=2,{crop_filter}scale=96:54:flags=area,format=yuv444p,"
        f"tblend=all_mode=difference,signalstats,metadata=print:key={MOTION_KEY}:file=-",
        MOTION_KEY, 0
    ), minimum_spread=2)


def find_highlights(source, recording, options, camera, game):
    if options.ranges:
        return read_ranges(options.ranges, recording.duration)
    if options.at:
        return select_highlights(
            [(at, None, "manual") for at in options.at], recording.duration, options
        )

    has_audio = recording.audio_index is not None
    motion = motion_levels(source, recording, game)
    if camera is None:
        weighted = {"motion": (motion, 0.45 if has_audio else 1)}
    else:
        face_motion = motion_levels(source, recording, camera)
        weighted = {
            "motion": (motion, 0.19 if has_audio else 0.45),
            "facecam": (face_motion, 0.33 if has_audio else 0.55),
        }
    if has_audio:
        sound = peaks(read_levels(
            source, recording.audio_index, "-af",
            "aresample=8000,asetnsamples=n=4000:p=0,astats=metadata=1:reset=1,"
            f"ametadata=print:key={SOUND_KEY}:file=-",
            SOUND_KEY, -90
        ), minimum_spread=4)
        weighted["sound"] = (sound, 0.48 if camera is not None else 0.55)

    candidates = []
    indexes = set().union(*(levels for levels, _ in weighted.values()))
    for index in indexes:
        components = {cue: levels.get(index, 0) * weight for cue, (levels, weight) in weighted.items()}
        score = sum(components.values())
        if score >= options.threshold:
            cue = max(components, key=components.get)
            candidates.append((index * SECOND_PER_SAMPLE, score, cue))
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return select_highlights(candidates, recording.duration, options)


def read_ranges(path, duration):
    with path.open("rb") as stream:
        raw = stream.read(MAX_RANGES_BYTES + 1)
    if len(raw) > MAX_RANGES_BYTES:
        raise ValueError("Файл --ranges должен занимать не более 64 КиБ.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as error:
        raise ValueError("Не удалось прочитать JSON из --ranges.") from error
    if (not isinstance(data, dict) or data.keys() != {"moments"} or
            not isinstance(data["moments"], list) or not 1 <= len(data["moments"]) <= MAX_RANGES):
        raise ValueError(f"В --ranges нужен список moments с 1–{MAX_RANGES} интервалами.")
    highlights = []
    required = {"start_seconds", "end_seconds"}
    allowed = required | {"headline"}
    for index, moment in enumerate(data["moments"], 1):
        if not isinstance(moment, dict) or not required <= moment.keys() <= allowed:
            raise ValueError(f"Интервал {index} должен содержать start_seconds и end_seconds. Поле headline необязательно.")
        start, end = moment["start_seconds"], moment["end_seconds"]
        if (type(start) not in (int, float) or type(end) not in (int, float) or
                not 0 <= start < end <= duration):
            raise ValueError(f"Интервал {index} должен иметь числовые границы внутри записи ({duration:g} с).")
        start, end = round(start, 3), round(end, 3)
        if not start < end <= duration:
            raise ValueError(f"Интервал {index} должен умещаться в запись с точностью до миллисекунд.")
        headline = None
        if "headline" in moment:
            headline = validate_headline(moment["headline"], f"поле headline в интервале {index}")
        highlights.append(Highlight(start, end, (start + end) / 2, None, "manual", headline))
    return highlights


def select_highlights(candidates, duration, options):
    selected = []
    length = min(options.seconds, duration)
    for peak, score, cue in candidates:
        if peak >= duration:
            if cue == "manual":
                raise ValueError(f"Отметка {peak:g} с за пределами записи ({duration:.1f} с).")
            continue
        start = min(max(0, peak - length * 0.4), duration - length)
        end = start + length
        if cue != "manual" and any(
            min(end, item.end) - max(start, item.start) > length * 0.15 for item in selected
        ):
            continue
        selected.append(Highlight(start, end, peak, score, cue))
        if not options.at and len(selected) >= options.count:
            break
    return selected


def ffmpeg_excerpt(source, item):
    return [*FFMPEG, "-ss", f"{item.start:.3f}", "-i", str(source)]


def duration_args(item):
    return ["-t", f"{item.end - item.start:.3f}"]


def transcribe(model, source, recording, item, folder, language):
    wave = folder / "speech.wav"
    execute([
        *ffmpeg_excerpt(source, item), "-map", f"0:{recording.audio_index}", *duration_args(item),
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wave)
    ])
    segments, info = model.transcribe(
        str(wave), language=language, beam_size=3, vad_filter=True, word_timestamps=True
    )
    captions = from_transcription(segments, item.start, item.end - item.start)
    wave.unlink()
    detected = None
    if language is None and captions:
        detected = {"code": info.language, "confidence": info.language_probability}
    return captions, detected


def render(source, recording, sections, folder, options, camera, game, overlay):
    has_audio = recording.audio_index is not None
    inputs, graph, tracks = [], [], []
    for index, section in enumerate(sections):
        duration = f"{section.duration:.3f}"
        inputs.extend(["-ss", f"{section.start:.3f}", "-t", f"{section.duration + 0.12:.3f}", "-i", str(source)])
        graph.append(
            f"[{index}:{recording.video_index}]setpts=PTS-STARTPTS,fps=30,"
            f"tpad=stop_mode=clone:stop_duration=0.10,trim=duration={duration}[v{index}]"
        )
        tracks.append(f"[v{index}]")
        if has_audio:
            graph.append(
                f"[{index}:{recording.audio_index}]asetpts=PTS-STARTPTS,"
                f"aresample=48000,apad=whole_dur={duration},atrim=duration={duration}[a{index}]"
            )
            tracks.append(f"[a{index}]")

    if len(sections) > 1:
        audio_label = "[aout]" if has_audio else ""
        graph.append(f"{''.join(tracks)}concat=n={len(sections)}:v=1:a={int(has_audio)}[cut]{audio_label}")
        stream, audio = "[cut]", audio_label
    else:
        stream, audio = "[v0]", "[a0]"
    graph.extend(composition(stream, options, camera, game, overlay))
    maps = ["-map", "[vout]"]
    if has_audio:
        maps.extend(["-map", audio])
    execute([
        *FFMPEG, *inputs, "-filter_complex", ";".join(graph), *maps, "-map_metadata", "-1",
        "-t", f"{sum(section.duration for section in sections):.3f}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast",
        "-crf", "20", "-threads:v", "4", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        "-movflags", "+faststart", "clip.mp4"
    ], cwd=folder)


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cutoff = text[:limit - 1].rsplit(" ", 1)[0]
    return (cutoff or text[:limit - 1]).rstrip(" ,.!?") + "…"


def write_post(folder, item, phrases, options, rank):
    topic = options.topic
    creator = f"Автор эфира — {options.creator}." if options.creator else ""
    phrase = next((text for text in phrases if len(text.split()) >= 4), "")
    if phrase:
        title = f"{topic} — «{shorten(phrase, max(16, 96 - len(topic)))}»"
    else:
        hooks = {
            "sound": "Момент с резким всплеском звука",
            "motion": "Резкая смена кадра",
            "facecam": "Момент с вебкамеры",
            "manual": "Фрагмент эфира",
        }
        title = f"{topic} — {hooks[item.cue]} #{rank}"
    title = shorten(title, 100)
    offset = int(item.peak)
    timecode = f"{offset // 3600:02d}:{offset // 60 % 60:02d}:{offset % 60:02d}"
    source = f"Запись эфира — {options.source_url}" if options.source_url else ""
    description = "\n".join(part for part in [
        f"Фрагмент эфира по теме «{topic}». Отметка в записи {timecode}.",
        creator,
        f"Реплика из эфира: «{shorten(phrase, 180)}»" if phrase else "",
        source
    ] if part)
    x_line = shorten(" ".join(part for part in [title, creator] if part), 130)
    (folder / "post.txt").write_text(
        f"Заголовок\n{title}\n\nОписание\n{description}\n\nКороткий текст для X\n{x_line}\n",
        encoding="utf-8"
    )
    return title, description, x_line


def export(source, recording, highlights, options, camera, game, source_captions):
    output = options.output.resolve()
    if output.exists():
        raise ValueError(f"Папка уже существует. Укажите другую через --output.\n{output}")
    model = None
    if highlights and options.transcribe and recording.audio_index is not None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            raise RuntimeError("Для --transcribe установите faster-whisper.") from error
        model = WhisperModel(options.model, device="cpu", compute_type="int8")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".stream-clips-", dir=output.parent) as temporary:
        staged = Path(temporary) / "result"
        staged.mkdir()
        manifest = []
        for rank, item in enumerate(highlights, 1):
            folder = staged / f"clip-{rank:02d}"
            folder.mkdir()
            print(f"Монтаж {rank}/{len(highlights)}", flush=True)
            sections = plan_edit(item.start, item.end, item.peak, options.hook, options.hook_seconds)
            original_captions, speech_language = source_captions, None
            if model:
                original_captions, speech_language = transcribe(
                    model, source, recording, item, folder, options.language
                )
            headline = (
                choose_headline(
                    item.headline or options.headline, options.topic,
                    [cue for cue in original_captions if item.start < cue.end and cue.start < item.end],
                    item.peak,
                )
                if options.layout == "split" else ""
            )
            captions = follow_edit(original_captions, sections)
            if options.require_speech and not captions:
                # A reaction should not leave the queue without its dialogue.
                folder.rmdir()
                continue
            burn = bool(captions and options.burn_captions)
            if captions:
                write_srt(folder / "captions.srt", captions)
            overlay = "overlay.ass" if headline else ("captions.ass" if burn else "")
            if overlay:
                fonts = Path(__file__).resolve().parent / "assets" / "fonts"
                shutil.copytree(fonts, folder / "fonts", ignore=shutil.ignore_patterns("*.txt", "*.md"))
                shutil.copy2(fonts / "OFL.txt", folder / "FONT-LICENSE.txt")
                write_ass(
                    folder / overlay, captions if burn else [], options.layout, headline=headline,
                    duration=sum(section.duration for section in sections), caption_y=options.caption_y,
                    caption_style=options.caption_style,
                )
            render(source, recording, sections, folder, options, camera, game, overlay)
            phrases = [caption.text for caption in captions]
            title, description, x_line = write_post(folder, item, phrases, options, rank)
            prefix = folder.name
            manifest.append({
                "video": f"{prefix}/clip.mp4",
                "text": f"{prefix}/post.txt",
                "captions": f"{prefix}/captions.srt" if captions else None,
                "styled_captions": f"{prefix}/{overlay}" if burn else None,
                "overlay": f"{prefix}/{overlay}" if overlay else None,
                "headline": headline or None,
                "start_seconds": round(item.start, 3),
                "end_seconds": round(item.end, 3),
                "peak_seconds": item.peak,
                "score": round(item.score, 3) if item.score is not None else None,
                "signal": item.cue,
                "layout": options.layout,
                "speech_language": speech_language,
                "caption_style": options.caption_style if burn else None,
                "caption_position": list(caption_position(options.layout, options.caption_y)) if burn else None,
                "timeline": [
                    {"source_start": section.start, "source_end": section.end, "role": section.role}
                    for section in sections
                ],
                "title": title,
                "description": description,
                "x_text": x_line,
            })
        if options.require_speech and highlights and not manifest:
            raise ValueError("Не удалось получить субтитры исходной речи. Проверьте звуковую дорожку или предоставьте SRT.")
        (staged / "manifest.json").write_text(json.dumps({
            "source": source.name, "source_url": options.source_url, "clips": manifest
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.rename(output)
    return output


def parse_timestamp(raw):
    try:
        if not re.fullmatch(r"(?:\d+:){0,2}\d+(?:\.\d+)?", raw):
            raise ValueError
        parts = [float(value) for value in raw.split(":")]
        if len(parts) > 1 and any(part >= 60 for part in parts[1:]):
            raise ValueError
        seconds = sum(part * 60 ** index for index, part in enumerate(reversed(parts)))
        if not math.isfinite(seconds):
            raise ValueError
        return seconds
    except ValueError as error:
        raise argparse.ArgumentTypeError("Формат отметки: секунды, ММ:СС или ЧЧ:ММ:СС.") from error


def validate_headline(raw, label):
    headline = plain_text(raw) if isinstance(raw, str) else ""
    if not 3 <= len(headline_words(headline)) <= 5 or len(headline) > 50:
        raise ValueError(f"Проверьте {label}. Требуется от 3 до 5 слов и до 50 символов.")
    return headline


def parse_args():
    parser = argparse.ArgumentParser(description="Автоматический поиск и монтаж моментов из записи эфира.")
    parser.add_argument("source", type=Path, help="Локальный видеофайл")
    parser.add_argument("--output", type=Path, default=Path("clips"), help="Новая папка с результатами")
    parser.add_argument("--topic", default="GTA VI", help="Тема для заголовка и описания")
    parser.add_argument("--creator", default="", help="Имя автора эфира")
    parser.add_argument("--source-url", default="", help="Ссылка на исходный эфир для описания")
    parser.add_argument("--count", type=int, default=3, help="Максимум автоматически найденных клипов")
    parser.add_argument("--seconds", type=int, default=35, help="Желаемая длина клипа, от 5 до 60 секунд")
    parser.add_argument("--threshold", type=float, default=1.6, help="Порог поиска моментов")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--at", type=parse_timestamp, action="append", help="Ручная отметка момента, можно повторить")
    selection.add_argument("--ranges", type=Path, help="JSON-файл с точными границами фрагментов в секундах")
    parser.add_argument("--width", type=int, choices=[720, 1080], default=1080, help="Ширина MP4")
    parser.add_argument("--layout", choices=["fit", "crop", "split"], help="Целый кадр, обрезка или две зоны без полей")
    parser.add_argument("--facecam", type=parse_crop, help="Вебкамера в исходнике: x,y,ширина,высота")
    parser.add_argument("--gameplay", type=parse_crop, help="Область игры; по умолчанию выбирается рядом с камерой")
    parser.add_argument("--facecam-focus", type=parse_focus, help="Точка для кадрирования камеры, например 50%%,45%%")
    parser.add_argument("--gameplay-focus", type=parse_focus, help="Точка для кадрирования игры, например 60%%,50%%")
    parser.add_argument("--headline", help="Описание из 3–5 слов на стыке вебкамеры и геймплея")
    parser.add_argument("--hook", choices=["off", "tease", "move"], default="off", nargs="?", const="tease",
                        help="Яркий момент в начале: повтор в сюжете или перенос")
    parser.add_argument("--hook-seconds", type=float, default=1.6, help="Длительность хука, от 0.8 до 3 секунд")
    caption_source = parser.add_mutually_exclusive_group()
    caption_source.add_argument("--transcribe", action="store_true", help="Распознать речь локально и сохранить SRT")
    caption_source.add_argument("--srt", type=Path, help="Готовые субтитры с таймкодами исходной записи")
    parser.add_argument("--burn-captions", action="store_true", help="Встроить оформленные субтитры в MP4")
    parser.add_argument("--require-speech", action="store_true",
                        help="Требовать исходный звук и субтитры в готовом фрагменте")
    parser.add_argument("--caption-y", type=float,
                        help="Центр субтитров в %% высоты кадра (40–80); по умолчанию 72%%")
    parser.add_argument("--caption-style", choices=tuple(CAPTION_COLORS), default="neon",
                        help="Оформление: neon — лаймовый акцент, gold — жёлтый, clean — белый")
    parser.add_argument("--language", help="Язык модели, например ru или en. По умолчанию определяется автоматически")
    parser.add_argument("--model", default="base", help="Модель faster-whisper или путь к её локальной папке")
    options = parser.parse_args()
    if not 5 <= options.seconds <= 60 or options.count < 1:
        parser.error("--seconds должен быть от 5 до 60, а --count должен быть положительным.")
    if not math.isfinite(options.threshold) or options.threshold <= 0:
        parser.error("--threshold должен быть положительным конечным числом.")
    if not 0.8 <= options.hook_seconds <= 3:
        parser.error("--hook-seconds должен быть от 0.8 до 3.")
    if options.ranges and options.hook != "off":
        parser.error("--ranges требует --hook off, чтобы сохранить точные границы фрагментов.")
    if options.burn_captions and not (options.transcribe or options.srt):
        parser.error("--burn-captions требует --transcribe или --srt.")
    if options.require_speech and not options.burn_captions:
        parser.error("--require-speech требует --burn-captions.")
    options.layout = options.layout or ("split" if options.facecam else "fit")
    if (options.layout == "split") != bool(options.facecam) or (options.gameplay and not options.facecam):
        parser.error("Для раздельного макета укажите --facecam. --gameplay применяется вместе с --facecam.")
    if options.layout != "split" and any((options.headline, options.facecam_focus, options.gameplay_focus)):
        parser.error("Тема и точки кадрирования доступны в макете с --facecam.")
    if options.headline is not None:
        try:
            options.headline = validate_headline(options.headline, "--headline")
        except ValueError as error:
            parser.error(str(error))
    if options.caption_y is not None and not 40 <= options.caption_y <= 80:
        parser.error("--caption-y должен быть от 40 до 80.")
    options.topic = " ".join(options.topic.split())
    if not 1 <= len(options.topic) <= 60:
        parser.error("--topic должен содержать от 1 до 60 символов.")
    return options


def main():
    options = parse_args()
    try:
        source = options.source.resolve(strict=True)
        if not source.is_file():
            raise ValueError("Укажите путь к видеофайлу.")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("Установите FFmpeg и добавьте ffmpeg и ffprobe в PATH.")
        if options.output.exists():
            raise ValueError(f"Папка уже существует. Укажите другую через --output.\n{options.output}")
        recording = probe(source)
        if options.require_speech and recording.audio_index is None:
            raise ValueError("У исходника нет аудиодорожки со звуком реакции.")
        camera, game = get_crops(recording.width, recording.height, options.facecam, options.gameplay)
        source_captions = read_srt(options.srt) if options.srt else []
        print("Проверяю интервалы…" if options.ranges else "Подбираю фрагменты…", flush=True)
        highlights = find_highlights(source, recording, options, camera, game)
        output = export(source, recording, highlights, options, camera, game, source_captions)
        if highlights:
            recorded = json.loads((output / "manifest.json").read_text())["clips"]
            print(f"Готово: {len(recorded)}. Папка: {output}")
        else:
            print(f"Выраженные пики не найдены. Манифест: {output / 'manifest.json'}")
            print("Для нового запуска укажите другой --output и понизьте --threshold или задайте --at.")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
