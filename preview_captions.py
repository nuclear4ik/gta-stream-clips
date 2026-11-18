#!/usr/bin/env python3
"""Render caption samples on a generated background or an authorized reference."""

import argparse
import shutil
import subprocess
from pathlib import Path

from captions import CAPTION_COLORS, Caption, Word, write_ass


ROOT = Path(__file__).resolve().parent


def sample_captions():
    result = []
    for start, texts in [(0.3, ["Вот", "это", "прыжок!"]),
                         (2.7, ["Только", "не", "тормози!"]),
                         (5.1, ["Успел", "в", "последний", "момент!"])]:
        words = tuple(Word(start + i * 0.48, start + (i + 1) * 0.48, text)
                      for i, text in enumerate(texts))
        result.append(Caption(start, start + 2.1, " ".join(texts), words))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "previews")
    parser.add_argument("--source", type=Path, help="Разрешённое видео для фона")
    parser.add_argument("--at", type=float, default=0, help="Секунда для извлечения кадра")
    args = parser.parse_args()
    if not shutil.which("ffmpeg"):
        parser.error("Установите FFmpeg с libass и добавьте ffmpeg в PATH.")
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "assets" / "fonts", folder / "fonts", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("*.txt", "*.md"))
    shutil.copy2(ROOT / "assets" / "fonts" / "OFL.txt", folder / "FONT-LICENSE.txt")

    def run(*args):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                       cwd=folder, check=True)

    if args.source:
        run("-ss", str(args.at), "-i", str(args.source.resolve()), "-frames:v", "1", "background.png")
    else:
        visual = ("color=c=0x121a20:s=1080x1920,"
                  "drawbox=x=72:y=104:w=936:h=520:color=0x263849:t=fill,"
                  "drawbox=x=72:y=660:w=936:h=900:color=0x24382e:t=fill,"
                  "drawbox=x=106:y=720:w=868:h=780:color=0x364d3b:t=6")
        run("-f", "lavfi", "-i", visual, "-frames:v", "1", "background.png")
    for style in CAPTION_COLORS:
        ass = f"captions-{style}.ass"
        write_ass(folder / ass, sample_captions(), "split", caption_style=style)
        filters = f"ass={ass}:fontsdir=fonts"
        run("-loop", "1", "-framerate", "30", "-i", "background.png", "-vf", filters,
            "-ss", "1.4", "-frames:v", "1", "-q:v", "2", f"captions-{style}.jpg")
        if style == "neon":
            run("-loop", "1", "-framerate", "30", "-i", "background.png", "-vf", filters,
                "-t", "7.6", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-crf", "20", "-preset", "fast", "-movflags", "+faststart", f"captions-{style}.mp4")
    print(folder)


if __name__ == "__main__":
    main()
