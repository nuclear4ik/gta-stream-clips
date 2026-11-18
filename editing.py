"""Plan clip order and build a portrait composition from the source video."""

import argparse
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rectangle:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    @property
    def area(self):
        return self.width * self.height

    def crop(self):
        return f"crop={self.width}:{self.height}:{self.x}:{self.y}:exact=1"

    def overlaps(self, other):
        return (
            max(self.x, other.x) < min(self.right, other.right)
            and max(self.y, other.y) < min(self.bottom, other.bottom)
        )


@dataclass(frozen=True)
class CropSpec:
    values: tuple[float, float, float, float]
    percent: bool

    def resolve(self, frame_width, frame_height, label):
        x, y, width, height = self.values
        if self.percent:
            left, top = round(x * frame_width / 100), round(y * frame_height / 100)
            right = round((x + width) * frame_width / 100)
            bottom = round((y + height) * frame_height / 100)
            pixels = left, top, right - left, bottom - top
        else:
            pixels = tuple(int(value) for value in self.values)
        rectangle = Rectangle(*pixels)
        if (
            rectangle.width < 16 or rectangle.height < 16
            or rectangle.right > frame_width or rectangle.bottom > frame_height
        ):
            raise ValueError(f"{label}: область должна быть не меньше 16×16 и входить в кадр {frame_width}×{frame_height}.")
        return rectangle


def parse_crop(raw):
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) == 4:
        if all(re.fullmatch(r"[0-9]{1,6}", part) for part in parts):
            return CropSpec(tuple(int(part) for part in parts), False)
        if all(re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,4})?%", part) for part in parts):
            return CropSpec(tuple(float(part[:-1]) for part in parts), True)
    raise argparse.ArgumentTypeError("Укажите x,y,ширину,высоту в пикселях или процентах, например 72%,69%,26%,28%.")


def parse_focus(raw):
    parts = [part.strip().removesuffix("%") for part in raw.split(",")]
    if len(parts) == 2 and all(re.fullmatch(r"[0-9]{1,3}(?:\.[0-9]{1,3})?", part) for part in parts):
        x, y = (float(part) / 100 for part in parts)
        if 0 <= x <= 1 and 0 <= y <= 1:
            return x, y
    raise argparse.ArgumentTypeError("Укажите центр кадрирования в процентах, например 50%,45%.")


def get_crops(frame_width, frame_height, facecam, gameplay):
    if facecam is None:
        return None, None
    camera = facecam.resolve(frame_width, frame_height, "--facecam")
    if gameplay is not None:
        game = gameplay.resolve(frame_width, frame_height, "--gameplay")
        if camera.overlaps(game):
            raise ValueError("--gameplay пересекается с --facecam. Выберите соседнюю область геймплея.")
        return camera, game
    remaining = [
        Rectangle(0, 0, camera.x, frame_height),
        Rectangle(camera.right, 0, frame_width - camera.right, frame_height),
        Rectangle(0, 0, frame_width, camera.y),
        Rectangle(0, camera.bottom, frame_width, frame_height - camera.bottom),
    ]
    game = max((rect for rect in remaining if min(rect.width, rect.height) >= 16),
               key=lambda rect: rect.area, default=None)
    if game is None:
        raise ValueError("Рядом с вебкамерой не осталось области для геймплея. Уточните --facecam.")
    return camera, game


@dataclass(frozen=True)
class Section:
    start: float
    end: float
    role: str = "story"

    @property
    def duration(self):
        return round(self.end - self.start, 3)


def plan_edit(start, end, peak, hook, hook_seconds):
    start, end = round(start, 3), round(end, 3)
    story = Section(start, end)
    if hook == "off" or peak - start < hook_seconds + 0.5 or end - peak < 0.12:
        return [story]
    hook_start = round(min(peak - 0.35, end - hook_seconds), 3)
    hook_end = round(hook_start + hook_seconds, 3)
    if end - hook_end < 0.12:
        hook_end = end
    opening = Section(hook_start, hook_end, "hook")
    if hook == "tease":
        return [opening, story]
    before = Section(start, hook_start)
    if hook_end >= end:
        return [opening, before]
    return [opening, before, Section(hook_end, end)]


SQUARE_PIXELS = "scale=trunc(iw*sar/2)*2:ih,setsar=1"
CAMERA_HEIGHT_1080 = 634
CENTER = (0.5, 0.5)


def fit_area(width, height, background):
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={background}"
    )


def fill_area(width, height, focus=CENTER):
    x, y = focus
    crop_x = f"max(0,min(iw-ow,iw*{x:.5f}-ow/2))"
    crop_y = f"max(0,min(ih-oh,ih*{y:.5f}-oh/2))"
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2,"
        f"crop={width}:{height}:x='{crop_x}':y='{crop_y}'"
    )


def composition(stream, options, camera, game, overlay):
    width, layout = options.width, options.layout
    height = width * 16 // 9
    if layout == "split":
        top = round(CAMERA_HEIGHT_1080 * width / 2160) * 2
        camera_fill = fill_area(width, top, options.facecam_focus or CENTER)
        game_fill = fill_area(width, height - top, options.gameplay_focus or CENTER)
        filters = [
            f"{stream}split=2[camera_src][game_src]",
            f"[camera_src]{camera.crop()},{SQUARE_PIXELS},{camera_fill}[camera_panel]",
            f"[game_src]{game.crop()},{SQUARE_PIXELS},{game_fill}[game_panel]",
            "[camera_panel][game_panel]vstack=inputs=2:shortest=1[portrait]",
        ]
    else:
        framing = fill_area(width, height) if layout == "crop" else fit_area(width, height, "0x161616")
        filters = [f"{stream}{SQUARE_PIXELS},{framing}[portrait]"]
    subtitles = f"ass={overlay}:fontsdir=fonts," if overlay else ""
    filters.append(f"[portrait]{subtitles}setsar=1,format=yuv420p[vout]")
    return filters
