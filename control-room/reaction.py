"""Use the existing split editing mode for source recordings with a facecam."""

import json


class MissingReactionProfile(ValueError):
    """The source should wait for the actual camera geometry."""


def profile_path(video):
    return video.with_suffix(".reaction.json")


def has_profile(video):
    profile = profile_path(video)
    return profile.is_file() and not profile.is_symlink()


def editor_arguments(video, *, required, transcribe):
    if not has_profile(video):
        if required:
            raise MissingReactionProfile(
                f"Добавьте {profile_path(video).name} с областью веб-камеры и описанием из 3–5 слов."
            )
        args = ["--layout", "crop"]
        if transcribe:
            args.extend(("--transcribe", "--burn-captions"))
        return args

    profile = profile_path(video)
    if profile.stat().st_size > 8192:
        raise ValueError("Профиль реакции слишком большой.")
    data = json.loads(profile.read_text(encoding="utf-8"))
    fields = {"facecam", "gameplay", "headline", "facecam_focus", "gameplay_focus"}
    if (not isinstance(data, dict) or not data.keys() <= fields or
            not {"facecam", "headline"} <= data.keys() or
            any(not isinstance(value, str) or not value.strip() or len(value) > 100 for value in data.values())):
        raise ValueError("Профиль реакции должен содержать facecam и headline. Область gameplay необязательна.")
    args = ["--layout", "split", "--require-speech", "--burn-captions"]
    for field in ("facecam", "gameplay", "headline", "facecam_focus", "gameplay_focus"):
        if field in data:
            args.append(f"--{field.replace('_', '-')}={data[field]}")
    srt = video.with_suffix(".srt")
    if srt.is_file() and not srt.is_symlink():
        args.extend(("--srt", str(srt)))
    else:
        args.append("--transcribe")
    return args
