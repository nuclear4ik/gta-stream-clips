#!/usr/bin/env python3
"""Run Streamcut from a provisioned Python environment under a macOS LaunchAgent."""

import argparse
import os
from pathlib import Path
import re
import shlex
import shutil
import sys


ROOM = Path(__file__).resolve().parents[1] / "control-room"
CAFFEINATE = "/usr/bin/caffeinate"
EXTRA_ENV = {
    "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET",
    "HF_HOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE",
    "HF_HUB_DISABLE_TELEMETRY", "XDG_CACHE_HOME",
}


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("Укажите абсолютный путь.")
    return path


def load_environment(path):
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError("Не удалось прочитать указанный .env в UTF-8.") from error
    environment = {}
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^export\s+", "", line)
        key, delimiter, raw = line.partition("=")
        key = key.strip()
        if not delimiter or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            raise ValueError(f"Строка {number} в .env должна иметь формат ИМЯ=значение.")
        if not (key.startswith("STREAMCUT_") or key in EXTRA_ENV):
            continue
        try:
            parts = shlex.split(raw, comments=True)
        except ValueError:
            raise ValueError(f"Проверьте кавычки в строке {number} файла .env.") from None
        if len(parts) > 1 or "\0" in raw:
            raise ValueError(f"Проверьте значение в строке {number} файла .env.")
        environment[key] = parts[0] if parts else ""
    return environment


def prepend_paths(environment, name, paths):
    entries = [str(path) for path in paths]
    if environment.get(name):
        entries.append(environment[name])
    if entries:
        environment[name] = os.pathsep.join(entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=absolute_path, required=True, help="Папка существующей очереди Streamcut")
    parser.add_argument("--env-file", type=absolute_path, help="Файл конфигурации. По умолчанию .env в папке данных")
    parser.add_argument("--port", type=int, default=8766, help="Локальный порт панели")
    parser.add_argument("--bin-dir", type=absolute_path, action="append", default=[],
                        help="Папка FFmpeg и ffprobe. Можно указать несколько раз")
    parser.add_argument("--python-path", type=absolute_path, action="append", default=[],
                        help="Дополнительная папка пакетов Python. Можно указать несколько раз")
    parser.add_argument("--keep-awake", action="store_true", help="Запускать через caffeinate -i")
    parser.add_argument("--check", action="store_true", help="Проверить конфигурацию без запуска панели")
    options = parser.parse_args()
    if not 1 <= options.port <= 65535:
        parser.error("Номер порта должен быть от 1 до 65535.")
    if not options.data.is_dir():
        parser.error("Папка данных не найдена. Проверьте путь к существующей очереди.")
    if not (ROOM / "studio.py").is_file():
        parser.error("Файл control-room/studio.py не найден. Используйте скрипт из рабочего репозитория.")
    if any(not path.is_dir() for path in (*options.bin_dir, *options.python_path)):
        parser.error("Одна из дополнительных папок FFmpeg или Python недоступна.")
    try:
        environment = {**os.environ, **load_environment(options.env_file or options.data / ".env")}
    except ValueError as error:
        parser.error(str(error))
    prepend_paths(environment, "PATH", options.bin_dir)
    prepend_paths(environment, "PYTHONPATH", options.python_path)
    if options.keep_awake and not os.access(CAFFEINATE, os.X_OK):
        parser.error("Не найден /usr/bin/caffeinate.")
    missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name, path=environment.get("PATH", os.defpath))]
    if options.check:
        if missing:
            parser.error(f"Не найдено: {', '.join(missing)}. Укажите --bin-dir.")
        print(f"Конфигурация читается. Панель будет доступна на http://127.0.0.1:{options.port}/.")
        return
    if missing:
        print(f"Монтаж ждёт установки: {', '.join(missing)}. Укажите --bin-dir.", file=sys.stderr, flush=True)
    command = [sys.executable, "-u", "studio.py", "--port", str(options.port), "--data", str(options.data)]
    if options.keep_awake:
        command = [CAFFEINATE, "-i", *command]
    os.chdir(ROOM)
    try:
        os.execve(command[0], command, environment)
    except OSError:
        sys.exit("Не удалось запустить Streamcut в указанном окружении Python.")


if __name__ == "__main__":
    main()
