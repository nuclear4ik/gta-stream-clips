"""Keep short captions on the same timeline as the edited video."""

import re
import unicodedata
from dataclasses import dataclass

from editing import CAMERA_HEIGHT_1080


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str
    words: tuple[Word, ...] = ()


TIME = r"([0-9]+):([0-5][0-9]):([0-5][0-9])[,.]([0-9]{3})"
SRT_RANGE = re.compile(rf"^{TIME}\s*-->\s*{TIME}(?:\s.*)?$")


def plain_text(text):
    text = re.sub(r"</?(?:b|i|u|font)(?:\s[^>]*)?>", "", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def parse_clock(groups):
    hours, minutes, seconds, milliseconds = map(int, groups)
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def read_srt(path):
    source = path.read_text(encoding="utf-8-sig")
    captions = []
    for block in re.split(r"\n\s*\n", source.strip()):
        lines = block.splitlines()
        first_line = 1 if lines and lines[0].strip().isdigit() else 0
        if len(lines) <= first_line:
            continue
        timing = SRT_RANGE.fullmatch(lines[first_line].strip())
        if timing is None:
            raise ValueError(f"Некорректная строка времени в SRT: {lines[first_line]}")
        start, end = parse_clock(timing.groups()[:4]), parse_clock(timing.groups()[4:])
        text = plain_text(" ".join(lines[first_line + 1:]))
        if text and end > start:
            captions.extend(make_phrases(start, end, text))
    return sorted(captions, key=lambda caption: (caption.start, caption.end))


def display_width(text):
    return sum(
        1.0 if unicodedata.east_asian_width(char) in ("W", "F") or char in "МШЩЮЫФЖWM"
        else 0.8 if char.isupper()
        else 0.35 if char in " iIl.,!?:;"
        else 0.67
        for char in text
    )


def choose_headline(explicit, fallback, captions, peak):
    if explicit:
        return explicit
    nearby = [
        caption for caption in captions
        if len(caption.text.split()) >= 3 and caption.start <= peak + 1.8 and caption.end >= peak - 1.2
    ]
    if not nearby:
        return fallback
    caption = min(nearby, key=lambda cue: max(cue.start - peak, peak - cue.end, 0))
    return caption.text.strip(" ,.!?…«»") or fallback


def short_groups(tokens):
    group = []
    width = 0
    for token in tokens:
        size = display_width(token.text.upper())
        if group and (len(group) >= 4 or width + size + 0.35 > 12):
            yield group
            group, width = [], 0
        group.append(token)
        width += size + 0.35
    if group:
        yield group


def make_phrases(start, end, text):
    tokens = [Word(0, 0, token) for token in text.split()]
    length = sum(len(token.text) for token in tokens)
    elapsed = 0
    for group in short_groups(tokens):
        begin = start + (end - start) * elapsed / length
        elapsed += sum(len(token.text) for token in group)
        finish = start + (end - start) * elapsed / length
        yield Caption(begin, finish, " ".join(token.text for token in group))


def from_transcription(segments, origin, duration):
    captions = []
    for segment in segments:
        start = max(0, segment.start)
        end = min(duration, segment.end)
        phrase = plain_text(segment.text)
        if not phrase or end - start < 0.05:
            continue
        words = []
        for word in segment.words or ():
            word_start = max(start, word.start)
            word_end = min(end, word.end)
            text = plain_text(word.word)
            if text and word_end > word_start:
                words.append(Word(origin + word_start, origin + word_end, text))
        if words:
            for group in short_groups(words):
                captions.append(Caption(group[0].start, group[-1].end, " ".join(word.text for word in group),
                                        tuple(group)))
        else:
            captions.extend(make_phrases(origin + start, origin + end, phrase))
    return captions


def trim_caption(caption, from_time, until_time, offset=0):
    start, end = max(from_time, caption.start), min(until_time, caption.end)
    if end - start < 0.08:
        return None
    words = tuple(
        Word(max(start, word.start) + offset, min(end, word.end) + offset, word.text)
        for word in caption.words
        if min(end, word.end) - max(start, word.start) >= 0.04
    )
    if caption.words and not words:
        return None
    text = " ".join(word.text for word in words) if words else caption.text
    return Caption(start + offset, end + offset, text, words)


def follow_edit(captions, sections):
    elapsed = 0.0
    result = []
    for section in sections:
        for caption in captions:
            retimed = trim_caption(caption, section.start, section.end, elapsed - section.start)
            if retimed:
                result.append(retimed)
        elapsed += section.duration
    result.sort(key=lambda caption: (caption.start, caption.end))
    # Positioned ASS events would draw on top of each other if their source cues overlapped.
    visible = []
    for index, caption in enumerate(result):
        new_end = result[index + 1].start if index + 1 < len(result) else caption.end
        trimmed = trim_caption(caption, caption.start, new_end)
        if trimmed:
            visible.append(trimmed)
    return visible


def srt_timestamp(seconds):
    total = max(0, round(1000 * seconds))
    hours, total = divmod(total, 3600000)
    minutes, total = divmod(total, 60000)
    whole_seconds, millis = divmod(total, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def write_srt(path, captions):
    path.write_text("\n\n".join(
        f"{index}\n{srt_timestamp(caption.start)} --> {srt_timestamp(caption.end)}\n{caption.text}"
        for index, caption in enumerate(captions, 1)
    ) + "\n", encoding="utf-8")


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,Montserrat ExtraBold,78,&H00FFFFFF,&H008BFFCE,&H0014110D,&H80000000,0,0,0,0,100,100,0,0,1,4.5,3,5,96,96,0,1
Style: Headline,DejaVu Sans,66,&H005DDAFF,&H00FFFFFF,&H0812161D,&H90000000,-1,0,0,0,100,100,0.55,0,1,6.5,3.0,5,90,90,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_timestamp(seconds):
    total = max(0, round(100 * seconds))
    hours, total = divmod(total, 360000)
    minutes, total = divmod(total, 6000)
    whole_seconds, centis = divmod(total, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centis:02d}"


def escape_ass(text):
    return text.translate(str.maketrans({"\\": "∖", "{": "(", "}": ")"}))


def fit_text(escaped, limit=14.4):
    if display_width(escaped) <= limit:
        return escaped
    lines = [""]
    for char in escaped:
        if display_width(lines[-1] + char) > limit - 0.7:
            if len(lines) == 2:
                lines[-1] = lines[-1].rstrip() + "…"
                break
            lines.append("")
        lines[-1] += char
    return r"\N".join(line.strip() for line in lines)


def format_headline(headline):
    text = escape_ass(headline.upper())
    limit = 12.8
    if display_width(text) <= limit:
        return text
    words = text.split()
    possible = [(" ".join(words[:index]), " ".join(words[index:])) for index in range(1, len(words))]
    fits = [(first, second) for first, second in possible
            if max(display_width(first), display_width(second)) <= limit]
    if not fits:
        return fit_text(text, limit)
    first, second = min(fits, key=lambda pair: abs(display_width(pair[0]) - display_width(pair[1])))
    return first + r"\N{\1c&HDAF5FF&}" + second


CAPTION_COLORS = {"neon": "8BFFCE", "gold": "68D4FF", "clean": "FFFFFF"}
CAPTION_SIZE = 78
DEFAULT_CAPTION_Y = 72


def caption_position(layout, caption_y=None):
    # All coordinates use the ASS reference canvas, including 720p exports.
    # Lower than the gameplay midpoint, with room below a two-line caption
    # for collapsed social UI. Expanded descriptions still need a preview.
    center_y = 19.2 * (DEFAULT_CAPTION_Y if caption_y is None else caption_y)
    return 540, round(center_y)


def caption_parts(tokens):
    """Keep word boundaries stable during highlighting; never discard speech."""
    texts = [token.upper() for token in tokens]
    if not texts:
        return [], CAPTION_SIZE
    limit = 12
    if display_width(" ".join(texts)) <= limit:
        return [escape_ass(text) for text in texts], CAPTION_SIZE
    if len(texts) == 1:
        text = texts[0]
        cut = min(range(1, len(text)), key=lambda i: abs(
            display_width(text[:i]) - display_width(text[i:])
        ))
        rows = [text[:cut], text[cut:]]
        parts = [escape_ass(rows[0]) + r"\N" + escape_ass(rows[1])]
    else:
        cut = min(range(1, len(texts)), key=lambda i: max(
            display_width(" ".join(texts[:i])), display_width(" ".join(texts[i:]))
        ))
        rows = [" ".join(texts[:cut]), " ".join(texts[cut:])]
        parts = [escape_ass(text) for text in texts]
        parts[cut] = r"\N" + parts[cut]
    size = min(CAPTION_SIZE, CAPTION_SIZE * limit / max(display_width(row) for row in rows))
    return parts, round(size, 2)


def write_ass(path, captions, layout, *, headline="", duration=0, caption_y=None,
              caption_style="neon"):
    center_x, center_y = caption_position(layout, caption_y)
    accent = CAPTION_COLORS[caption_style]
    anchor = rf"{{\an5\pos({center_x},{center_y})\q2\blur0.4}}"
    lines = [ASS_HEADER]

    if headline:
        position = rf"{{\an5\pos(540,{CAMERA_HEIGHT_1080})\fad(100,0)}}"
        lines.append(
            f"Dialogue: 0,0:00:00.00,{ass_timestamp(duration)},Headline,,0,0,0,,"
            f"{position}{format_headline(headline)}\n"
        )

    def add_line(start, end, text, size):
        first, last = ass_timestamp(start), ass_timestamp(end)
        if first != last:
            lines.append(f"Dialogue: 1,{first},{last},Caption,,0,0,0,,{anchor}{{\\fs{size:g}}}{text}\n")

    def join_parts(parts):
        return " ".join(parts).replace(r" \N", r"\N")

    for caption in captions:
        if not caption.words:
            parts, size = caption_parts(caption.text.split())
            add_line(caption.start, caption.end, join_parts(parts), size)
            continue

        current = caption.start
        plain, size = caption_parts([word.text for word in caption.words])
        for index, word in enumerate(caption.words):
            start = max(current, word.start)
            if start > current:
                add_line(current, start, join_parts(plain), size)
            if word.end > start:
                highlighted = list(plain)
                # Put line breaks outside color tags so spacing is identical
                # in highlighted events and in pauses between spoken words.
                prefix = r"\N" if plain[index].startswith(r"\N") else ""
                token = plain[index][2:] if prefix else plain[index]
                highlighted[index] = prefix + rf"{{\1c&H{accent}&}}" + token + r"{\1c&HFFFFFF&}"
                add_line(start, word.end, join_parts(highlighted), size)
                current = word.end
        if current < caption.end:
            add_line(current, caption.end, join_parts(plain), size)
    path.write_text("".join(lines), encoding="utf-8")
