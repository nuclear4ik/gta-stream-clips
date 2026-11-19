# Фоновый запуск Streamcut на Mac

Пользовательский LaunchAgent запускает панель при входе в macOS и перезапускает её после выхода процесса. Панель доступна только на [127.0.0.1:8766](http://127.0.0.1:8766/). После выхода из учётной записи работа возобновляется при следующем входе. [Документация Apple](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)

## Подготовка

Используйте абсолютный путь к Python из окружения, в котором работает монтаж. Для автомонтажа Ollama должна быть запущена отдельно с установленной моделью. Параметры монтажа и авторизации описаны в [инструкции панели](../control-room/README.md).

Укажите папку работающей очереди из прежнего аргумента `studio.py --data`. Скрипт запустит версию кода из своей рабочей копии и откроет ту же SQLite, входящую папку и экспорты. Для проверки без запуска выполните следующую команду с вашими путями.

```sh
"/absolute/venv/bin/python" "/absolute/worktree/scripts/launch-macos.py" \
  --data "/absolute/existing/.streamcut-data" \
  --env-file "/absolute/existing/.streamcut-data/.env" \
  --bin-dir "/opt/homebrew/bin" --check
```

По умолчанию читается `.env` внутри папки данных. Скрипт принимает строки `KEY=value` и `export KEY=value`. Значения с пробелами или символом `#` заключайте в кавычки. Скрипт читает конфигурацию Streamcut, Twitch и кэша Whisper без выполнения команд оболочки. OAuth и Twitch-секреты храните в `.env`.

## LaunchAgent

Создайте каталог журналов с правами владельца и файл `$HOME/Library/LaunchAgents/local.streamcut.studio.plist`. Подставьте абсолютные пути в элементы XML. `--bin-dir` и `--python-path` допускают несколько папок и нужны, когда бинарники или Python-пакеты установлены вне окружения по умолчанию.

```sh
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p -m 700 "$HOME/Library/Logs/Streamcut"
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.streamcut.studio</string>
    <key>ProgramArguments</key>
    <array>
        <string>/absolute/venv/bin/python</string>
        <string>/absolute/worktree/scripts/launch-macos.py</string>
        <string>--data</string>
        <string>/absolute/existing/.streamcut-data</string>
        <string>--bin-dir</string>
        <string>/opt/homebrew/bin</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>20</integer>
    <key>StandardOutPath</key>
    <string>/absolute/home/Library/Logs/Streamcut/stdout.log</string>
    <key>StandardErrorPath</key>
    <string>/absolute/home/Library/Logs/Streamcut/stderr.log</string>
</dict>
</plist>
```

Перенесите настройки запуска из прежней команды в `.env` или в `EnvironmentVariables` LaunchAgent. Если переменная указана в обоих местах, действует значение из `.env`. Добавьте `<string>--keep-awake</string>` в `ProgramArguments`, если хотите удерживать Mac от сна по бездействию, пока работает Streamcut. После остановки процесса это удержание снимается.

Перед подключением агента дождитесь завершения активной отправки и остановите прежний процесс панели. Одновременно должен работать один экземпляр с этой папкой данных.

```sh
agent="$HOME/Library/LaunchAgents/local.streamcut.studio.plist"
domain="gui/$(id -u)"
plutil -lint "$agent"
launchctl bootstrap "$domain" "$agent"
launchctl print "$domain/local.streamcut.studio"
curl -fsS -o /dev/null http://127.0.0.1:8766/api/state
```

После изменения `.env` перезапустите агент, чтобы перечитать настройки.

```sh
launchctl kickstart -k "gui/$(id -u)/local.streamcut.studio"
```

Чтобы остановить фоновый запуск в текущем сеансе, выполните следующую команду. SQLite и медиафайлы остаются в папке данных.

```sh
launchctl bootout "gui/$(id -u)/local.streamcut.studio"
```
