# Стартовые списки авторов

Поиск идёт по трём целевым аудиториям. Это редактируемая подборка известных авторов, а не рейтинг по текущим подписчикам. Включение в список не означает, что автор уже делал контент о GTA 6.

- **RU · Россия.** [Бустер](https://www.twitch.tv/buster/videos), [Bratishkinoff](https://www.twitch.tv/bratishkinoff/videos), [Recrent](https://www.twitch.tv/recrent/videos).
- **EN · США.** [Kai Cenat](https://www.twitch.tv/kaicenat), [CaseOh](https://www.twitch.tv/caseoh_), [LIRIK](https://www.twitch.tv/lirik).
- **ESP · Испания.** [Ibai](https://www.twitch.tv/ibai), [AuronPlay](https://www.twitch.tv/auronplay), [IlloJuan](https://www.twitch.tv/illojuan/videos).

## Как ищет

**YouTube.** На каждого автора создаётся запрос с вариантами `GTA 6` и `GTA VI`. В результатах проверяется тема в заголовке и имя автора в заголовке **или** названии канала загрузчика. Поэтому «Бустер проходит GTA 6» подходит даже у канала с другим названием. Принимаются недавно добавленные видео и найденные текущие трансляции. Анонсы предстоящих эфиров пропускаются. Учитываются результаты первой страницы для каждого автора за последние три дня. [YouTube Data API](https://developers.google.com/youtube/v3/docs/search/list)

`regionCode` передаётся как `RU`, `US` или `ES` и учитывает доступность видео в регионе. `relevanceLanguage` указывает релевантный язык. Эти поля не доказывают страну автора или язык его речи, который проверяется перед автоотправкой. [Параметры поиска YouTube](https://developers.google.com/youtube/v3/docs/search/list#parameters)

**Twitch.** По списку логинов или числовых ID проверяются активные трансляции и до пяти свежих публичных записей каждого автора, не старше трёх дней. Для эфира подходит тема в заголовке или категория GTA 6. Для записи проверяется заголовок. В обоих случаях должен совпадать язык из метаданных Twitch. [Каналы](https://dev.twitch.tv/docs/api/reference/users#get-users), [эфиры](https://dev.twitch.tv/docs/api/reference/streams#get-streams), [записи](https://dev.twitch.tv/docs/api/reference/videos#get-videos)

Обе платформы отдают ссылки и метаданные. Для монтажа нужен отдельный исходный файл, который вы вправе использовать.

## Настройка

При заданных ключах используются стартовые списки. Чтобы заменить список, задайте переменную для соответствующей площадки и языка. Пустое значение отключает поиск этой площадки по данному языку.

- `STREAMCUT_YOUTUBE_KEY` включает запросы YouTube Data API. `STREAMCUT_YOUTUBE_RU`, `STREAMCUT_YOUTUBE_EN`, `STREAMCUT_YOUTUBE_ESP` принимают до пяти групп авторов. Разделитель групп — `|`, варианты имени одного автора разделяются запятыми. Пример значения: `Бустер,Buster|Bratishkinoff,Братишкин|Recrent,Рекрент`.
- `TWITCH_CLIENT_ID` и `TWITCH_CLIENT_SECRET` включают Twitch Helix. `STREAMCUT_TWITCH_RU`, `STREAMCUT_TWITCH_EN`, `STREAMCUT_TWITCH_ESP` принимают до пяти логинов или числовых ID через `|`. Пример значения: `buster|bratishkinoff|recrent`.

Ключи API задаются в окружении сервера. Они не нужны для ручного добавления ссылки или файла.
