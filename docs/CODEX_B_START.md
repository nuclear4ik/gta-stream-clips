# Первый промпт для второго чата Codex

Скопируйте весь блок ниже во второй чат.

```text
Работай над Streamcut в публичном GitHub-репозитории https://github.com/nuclear4ik/gta-stream-clips. Твоя задача — https://github.com/nuclear4ik/gta-stream-clips/issues/2. Первый чат ведёт авторизацию, маршрутизацию и развёртывание, поэтому не меняй его файлы.

Создай отдельную рабочую папку:
git clone https://github.com/nuclear4ik/gta-stream-clips.git streamcut-agent-b
cd streamcut-agent-b
git fetch origin
git switch -c codex-b/discovery-quality origin/main

Если клон или ветка уже есть, сначала проверь git status и переиспользуй их. Не удаляй незакоммиченные изменения. Прочитай AGENTS.md, README.md, docs/WORKSTREAMS.md и Issue #2.

Улучши автопоиск YouTube/Twitch по RU, EN и ESP. Измени только control-room/discovery.py, control-room/CREATORS.md и control-room/test_discovery.py. Проверь, что перезалив с именем автора только в заголовке попадает в кандидаты, а похожее постороннее имя или другая игра не проходит. Проверь повторный результат, эфир и последующую запись Twitch. Используй ответы официальных API в тестовых фикстурах. Не скачивай чужие записи и не включай реальные публикации.

Выполни (cd control-room && python3 -m unittest discover -q). Прочти diff и сверь origin/main перед отправкой. Сделай коммит с темой и телом «Что изменилось», «Почему», «Проверки». Запушь свою ветку командой git push -u origin codex-b/discovery-quality и открой отдельный PR на main со ссылкой на #2. Если нет права пушить в upstream, используй fork. Не пушь в main и не добавляй секреты, токены или реальные данные. В итоге дай ссылку на PR, результаты проверок и краткую заметку для первого чата.
```
