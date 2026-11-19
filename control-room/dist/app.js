(() => {
  'use strict';

  const languages = {RU: 'Русский', EN: 'English', ESP: 'Español'};
  const platforms = ['YouTube', 'TikTok', 'Reels'];
  const countries = {RU: 'Россия', US: 'США', ES: 'Испания', server: 'Прямой маршрут'};
  const markets = {
    RU: {country: 'RU', city: 'Москва', zone: 'Europe/Moscow'},
    EN: {country: 'US', city: 'Нью-Йорк', zone: 'America/New_York'},
    ESP: {country: 'ES', city: 'Мадрид', zone: 'Europe/Madrid'},
  };
  const platformName = platform => platform === 'Reels' ? 'Instagram' : platform;
  const sections = {queue: 'Очередь', sources: 'Источники', accounts: 'Аккаунты', connections: 'Подключения'};
  const defaults = {RU: 'After the Chase | GTA 6', EN: 'One More Job | GTA 6', ESP: 'Una más | GTA 6'};
  const editModes = {auto: 'Автоматически по видео', model: 'Нейросеть по SRT', manual: 'Точные интервалы'};
  const configurableSources = new Set(['waiting', 'waiting_layout', 'failed', 'empty']);
  const maxTranscriptBytes = 512 * 1024;
  const labels = {
    waiting: 'Нужен исходный файл', waiting_layout: 'Нужны границы камеры', queued: 'В работе', editing: 'Монтируется', ready: 'Готово',
    twitch_wait_archive: 'Ожидает архив Twitch', twitch_wait_check: 'Ожидает проверки записи',
    twitch_wait_access: 'Нужен доступ к исходнику', twitch_wait_media: 'Ожидает автополучения',
    twitch_media_missing: 'Файл пока недоступен', twitch_media_error: 'Ошибка автополучения',
    twitch_paused: 'Автополучение отложено',
    empty: 'Моменты не найдены', failed: 'Ошибка монтажа', review: 'Готов к планированию',
    scheduled: 'Ждёт слота YouTube', preloaded: 'Запланировано в YouTube',
    checking: 'Проверка подключения', sending: 'Загружается в YouTube', remote_check: 'YouTube проверяет расписание',
    blocked: 'Заблокировано', unknown: 'Проверьте Studio',
    uploaded: 'Опубликовано', cancelled: 'Отменено', route_wait: 'Ждёт подключения',
  };
  const healthLabels = {
    ok: 'Доступно', failed: 'Сбой', unconfigured: 'Не настроено', unchecked: 'Ожидает проверки',
    stale: 'Проверка устарела', preview: 'Нет данных',
  };
  const localFormatters = Object.fromEntries(Object.entries(markets).map(([lang, market]) => [
    lang, new Intl.DateTimeFormat('sv-SE', {timeZone: market.zone, year: 'numeric', month: '2-digit',
      day: '2-digit', hour: '2-digit', minute: '2-digit'}),
  ]));
  const timeFormatters = Object.fromEntries(Object.entries(markets).map(([lang, market]) => [
    lang, new Intl.DateTimeFormat('ru-RU', {timeZone: market.zone, day: 'numeric', month: 'short',
      hour: '2-digit', minute: '2-digit', timeZoneName: 'shortOffset'}),
  ]));
  const numberFormatter = new Intl.NumberFormat('ru-RU');
  const checkFormatter = new Intl.DateTimeFormat('ru-RU', {
    dateStyle: 'short', timeStyle: 'short', timeZone: 'Europe/Moscow',
  });
  const app = document.querySelector('#app');
  const dialog = document.querySelector('#dialog');
  const toast = document.querySelector('#toast');
  let page = 'queue', filter = '', info = '', timer, lastLoaded = '', connectedOnce = false;
  let accountPlatform = 'YouTube';
  const openGroups = new Set(['RU']);

  function escape(value) {
    const replacements = {'<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;', '&': '&amp;'};
    return String(value ?? '').replace(/[<>"'&]/g, char => replacements[char]);
  }

  const sample = {
    live: false, settings: {scan: true, paused: false}, accounts: [], sources: [],
    queue: [
      {id: 1, title: 'Погоня после прыжка', lang: 'RU', platform: 'YouTube', status: 'review'},
      {id: 2, title: 'Слишком ранний поворот', lang: 'RU', platform: 'YouTube', status: 'preloaded'},
      {id: 3, title: 'La primera reacción', lang: 'ESP', platform: 'YouTube', status: 'scheduled'},
    ].map((job, i) => ({...job, clip_id: job.id, account: defaults[job.lang],
      at: new Date(Date.now() + (i + 1) * 3600000).toISOString()})),
  };
  let data = sample;

  function notify(message) {
    clearTimeout(timer);
    toast.textContent = message;
    toast.classList.add('visible');
    timer = setTimeout(() => toast.classList.remove('visible'), 4500);
  }

  async function refresh() {
    if (location.protocol === 'file:') return;
    try {
      const res = await fetch('/api/state', {cache: 'no-store', signal: AbortSignal.timeout(3000)});
      if (!res.ok || !res.headers.get('content-type')?.includes('application/json')) throw new Error();
      const payload = await res.json();
      if (!payload.live) throw new Error();
      connectedOnce = true;
      const serialized = JSON.stringify(payload);
      if (serialized !== lastLoaded || info) {
        info = '';
        data = payload;
        lastLoaded = serialized;
        render();
      }
    } catch {
      if (connectedOnce && !info) {
        info = 'Связь с сервером потеряна';
        render();
      }
    }
  }

  async function post(path, payload) {
    if (!data.live || info) throw new Error('Запустите локальный сервис Streamcut.');
    const res = await fetch('/api/' + path, {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload),
    });
    const result = await res.json();
    if (!res.ok) throw new Error(result.error || 'Запрос не выполнен.');
    return result;
  }

  async function uploadSource(file, source) {
    if (!data.live || info) throw new Error('Откройте рабочую панель Streamcut.');
    const query = new URLSearchParams({
      name: file.name, lang: source.lang, url: source.url || '', title: source.title || '',
    });
    if (source.id != null) query.set('id', source.id);
    if (source.mark != null) query.set('mark', source.mark);
    const response = await fetch('/api/inbox?' + query, {
      method: 'POST', headers: {'Content-Type': 'application/octet-stream'}, body: file,
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || 'Не удалось отправить видео.');
  }

  const badge = lang => '<span class="language ' + escape(lang) + '">' + escape(lang) + '</span>';
  const status = state => '<span class="status ' + escape(state) + '">' + escape(labels[state] || state) + '</span>';
  const healthBadge = state => '<span class="status ' + escape(state) + '">' + escape(healthLabels[state] || healthLabels.unchecked) + '</span>';
  const time = (value, lang) => timeFormatters[lang].format(new Date(value));
  const metric = value => typeof value === 'number' && Number.isFinite(value) ? numberFormatter.format(value) : '—';
  const mainButton = (label, action, id = '') =>
    '<button class="primary" data-do="' + escape(action) + '" data-id="' + escape(id) + '">' + label + '</button>';
  const option = (value, name, selected) =>
    '<option value="' + escape(value) + '"' + (value === selected ? ' selected' : '') + '>' + escape(name) + '</option>';

  function jobAccount(job) {
    if (job.account_id != null) return data.accounts.find(account => account.id === job.account_id);
    return data.accounts.find(account => account.name === job.account &&
      account.platform === job.platform && account.lang === job.lang);
  }

  function jobTime(job) {
    return job.status === 'preloaded' ? job.publish_at || job.at : job.at;
  }

  function studioLink(job) {
    return typeof job.external_id === 'string' && /^[A-Za-z0-9_-]{11}$/.test(job.external_id)
      ? '<a href="https://studio.youtube.com/video/' + job.external_id +
        '/edit" target="_blank" rel="noopener noreferrer">YouTube Studio ↗</a>' : '';
  }

  function queueRows(jobs, clips = []) {
    const rows = jobs.map(job => {
      const canEdit = job.status === 'preloaded' || !job.external_id &&
        !['sending', 'checking', 'remote_check', 'unknown', 'uploaded', 'cancelled'].includes(job.status);
      const at = jobTime(job);
      return `<article class="job">
        <div class="job-time">${Number.isFinite(Date.parse(at))
          ? '<time datetime="' + escape(at) + '">' + escape(time(at, job.lang)) + '</time>'
          : 'Время не задано'}
          <small>${markets[job.lang].city}${job.status === 'preloaded' ? '' : ' · локальное время'}</small></div>
        <div class="job-title">${badge(job.lang)}<div><strong>${escape(job.title)}</strong>
          <small>${escape(platformName(job.platform))} · ${escape(job.account)}</small>
          ${job.detail ? '<small class="error-note">' + escape(job.detail) + '</small>' : ''}
          ${studioLink(job)}</div></div>
        <div class="job-actions">${status(job.status)}
          ${data.live && canEdit ? '<button class="quiet" data-do="edit" data-id="' + job.id + '">Открыть</button>' : ''}
          ${data.live && !canEdit && job.clip_id ? '<button class="quiet" data-do="preview" data-id="' + job.clip_id + '">Видео</button>' : ''}
        </div></article>`;
    });
    for (const clip of clips) {
      rows.push(`<article class="job job-ready"><div class="job-title">${badge(clip.lang)}
        <div><strong>${escape(clip.title)}</strong><small>Готовый MP4</small></div></div>
        <div class="job-actions">${status('ready')}
          <button class="quiet" data-do="preview" data-id="${escape(clip.id)}">Видео</button></div></article>`);
    }
    return rows.length ? '<div class="job-list">' + rows.join('') + '</div>' : '';
  }

  function publishingNotice() {
    if (!data.live) return '';
    if (!data.oauth) return '<div class="queue-notice">Для планирования нужны настройки Google OAuth на сервере. <a href="#accounts">Аккаунты →</a></div>';
    if (!data.accounts.some(account => account.platform === 'YouTube' && account.lang === 'RU' && account.connected)) {
      return '<div class="queue-notice">Подключите Google к каналу RU. Готовые ролики появятся в очереди сразу после монтажа. <a href="#accounts">Аккаунты →</a></div>';
    }
    if (!data.uploads) return '<div class="queue-notice">Отправка в YouTube выключена в настройках сервера.</div>';
    if (data.settings.paused) return '<div class="queue-notice">Новые отправки на паузе. YouTube опубликует уже запланированные ролики по их расписанию.</div>';
    return '';
  }

  function queuePage() {
    const jobs = data.queue.filter(job => job.platform === 'YouTube');
    const current = item => !filter || item.lang === filter;
    const assigned = new Set(jobs.map(job => job.clip_id));
    const unassigned = data.clips?.filter(clip => !assigned.has(clip.id)) || [];
    const backlog = jobs.filter(job => !['uploaded', 'cancelled', 'preloaded'].includes(job.status));
    const scheduled = jobs.filter(job => job.status === 'preloaded' && current(job))
      .sort((a, b) => Date.parse(jobTime(a)) - Date.parse(jobTime(b)));
    const history = jobs.filter(job => ['uploaded', 'cancelled'].includes(job.status) && current(job));
    const waiting = queueRows(backlog.filter(current), unassigned.filter(current));
    const shownLimit = Number.isInteger(data.inventory_limit) && data.inventory_limit > 0 ? data.inventory_limit : null;
    const counts = Object.fromEntries(Object.keys(markets).map(lang => [
      lang, data.queue_counts?.[lang] ?? new Set([
        ...backlog.filter(job => job.lang === lang).map(job => job.clip_id || job.id),
        ...unassigned.filter(clip => clip.lang === lang).map(clip => clip.id),
      ]).size,
    ]));
    return `<div class="heading"><div><h1>Очередь</h1><p>${data.live ? 'Готовые Shorts и расписание YouTube' : 'Пример очереди'}</p></div>
      ${mainButton('+ Источник', 'source')}</div>
      <div class="market-grid" role="group" aria-label="Видео к загрузке по аудиториям">
        ${Object.entries(markets).map(([lang, market]) => `<button class="market surface" data-do="filter"
          data-id="${lang}" aria-pressed="${filter === lang}"><span class="market-label">${badge(lang)}
          <span>${countries[market.country]}<small>${market.city}</small></span></span>
          <strong aria-label="Локальный запас: ${metric(counts[lang])}${shownLimit ? ' из ' + metric(shownLimit) : ''}">${metric(counts[lang])}${shownLimit ? '<span> / ' + metric(shownLimit) + '</span>' : ''}</strong></button>`).join('')}
      </div>
      <section class="surface">
        <div class="bar">
          <button data-do="filter" data-id="" aria-pressed="${!filter}">Все аудитории</button>
          ${data.live ? '<button class="quiet" data-do="pause">' + (data.settings.paused ? 'Возобновить отправку' : 'Пауза') + '</button>' : '<button class="quiet" data-do="preview">Пример видео</button>'}
        </div>
        ${publishingNotice()}
        <h2 class="queue-subhead">Запланировано в YouTube</h2>
        ${queueRows(scheduled) || '<p class="queue-empty">Подтверждённых дат пока нет.</p>'}
        <h2 class="queue-subhead">Локальная очередь</h2>
        ${waiting || '<p class="queue-empty">Готовые видео появятся после монтажа. <a href="#sources">Источники →</a></p>'}
      </section>
      ${history.length ? '<details class="surface history"><summary>История · ' + history.length + '</summary>' + queueRows(history) + '</details>' : ''}`;
  }

  function sourceCard(source, clips) {
    const id = escape(source.id);
    const detailClass = !source.media_state || source.media_state === 'twitch_media_error' ? ' class="error-note"' : '';
    return `<article class="job source">
      <div class="job-title">${badge(source.lang)}<div><strong>${escape(source.title)}</strong>
        ${source.url ? '<a class="muted" href="' + escape(source.url) + '" target="_blank" rel="noopener noreferrer">Ссылка ↗</a>' : ''}
        ${source.kind === 'live' ? '<small>Эфир при последней проверке</small>' : source.kind === 'vod' ? '<small>Запись эфира</small>' : ''}
        ${source.detail ? '<small' + detailClass + '>' + escape(source.detail) + '</small>' : ''}</div></div>
      <div class="job-actions">${status(labels[source.media_state] ? source.media_state : source.status)}
        ${configurableSources.has(source.status) ? '<button data-do="source" data-id="' + id + '">' +
          (source.status === 'waiting' ? 'Добавить исходник' : 'Настроить монтаж') + '</button>' : ''}
        ${source.status === 'waiting' ? '<button class="quiet" data-do="dismiss" data-id="' + id + '">Скрыть</button>' : ''}</div>
      ${clips.length ? `<div class="source-clips" aria-label="Готовые клипы">${clips.map(clip =>
        `<div><span>${escape(clip.title)}</span><button class="quiet" data-do="preview" data-id="${escape(clip.id)}">Смотреть</button></div>`
      ).join('')}</div>` : ''}</article>`;
  }

  function sourcesPage() {
    const roster = data.roster || {};
    const clipsBySource = new Map();
    for (const clip of data.clips || []) {
      if (!clipsBySource.has(clip.source_id)) clipsBySource.set(clip.source_id, []);
      clipsBySource.get(clip.source_id).push(clip);
    }
    return `<div class="heading"><div><h1>Источники</h1><p>Twitch · Grand Theft Auto VI</p></div>${mainButton('+ Источник', 'source')}</div>
      <section class="surface">
        <div class="bar"><div><strong>Автопоиск</strong>
          <small>${escape(data.search_status || (!data.live || data.search_configured
            ? 'До 10 каналов на язык · по числу зрителей'
            : 'Для поиска по категории нужны параметры API Twitch.'))}</small></div>
          ${data.live ? '<button class="quiet" data-do="scan" aria-pressed="' + data.settings.scan + '">' + (data.settings.scan ? 'Включён' : 'Выключен') + '</button>' : '<span class="status scheduled">Включён по умолчанию</span>'}</div>
        <div class="roster-grid">${Object.entries(markets).map(([lang, market]) =>
          `<div><strong>${badge(lang)} ${countries[market.country]}</strong>
            <small>${data.ranking_updated?.[lang] ? 'Активные каналы · по зрителям' : 'Ожидает проверки категории'}</small>
            ${data.ranking_updated?.[lang] && roster[lang]?.length
              ? '<ol>' + roster[lang].map(name => '<li>' + escape(name) + '</li>').join('') + '</ol>' : ''}</div>`).join('')}</div>
        ${data.sources.length ? '<div class="job-list">' + data.sources.map(source =>
          sourceCard(source, clipsBySource.get(source.id) || [])).join('') + '</div>' :
            '<div class="blank"><strong>Нет источников</strong><p>Добавьте ссылку или положите разрешённый файл в папку нужного языка.</p></div>'}
      </section>`;
  }

  function accountCard(account) {
    const state = account.connected ? (account.auto ? 'Планирует автоматически' : 'Подключён') : 'Не подключён';
    const stats = account.stats || {};
    const reportDate = typeof stats.day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(stats.day)
      ? stats.day.split('-').reverse().join('.') : '';
    return `<article class="account">
      <div class="account-label"><strong>${escape(account.name)}</strong>
        <small>${state} · ${escape(countries[account.route] || account.route)}</small>
        ${account.target ? '<small>' + escape(account.target) + '</small>' : ''}</div>
      <dl class="metrics">
        <div><dt>Просмотры за сутки</dt><dd>${metric(stats.views)}</dd></div>
        <div><dt>Новые подписчики за сутки</dt><dd>${metric(stats.followers_gained)}</dd></div>
        <div><dt>Всего подписчиков</dt><dd>${metric(stats.followers_total)}</dd></div>
      </dl>
      <button class="quiet" data-do="account" data-id="${escape(account.id)}"
        aria-label="Настроить канал ${escape(account.name)}">Настроить</button>
      ${reportDate || stats.note ? '<small class="report-note">' + [reportDate && 'Отчёт за ' + reportDate,
        stats.note && escape(stats.note)].filter(Boolean).join(' · ') + '</small>' : ''}
    </article>`;
  }

  function accountsPage() {
    return `<div class="heading"><div><h1>Аккаунты</h1><p>Каналы по площадкам и языкам</p></div>
      ${mainButton('+ Добавить канал', 'account-new')}</div>
      <div class="filters platform-filters" role="group" aria-label="Социальная сеть">${platforms.map(platform =>
        `<button data-do="platform" data-id="${platform}" aria-pressed="${accountPlatform === platform}">${platformName(platform)}</button>`).join('')}</div>
      <div class="account-groups">${Object.entries(markets).map(([lang, market]) => {
        const accounts = data.accounts.filter(row => row.platform === accountPlatform && row.lang === lang);
        const expanded = openGroups.has(lang);
        return `<section class="surface"><h2>
          <button class="account-head" data-do="account-group" data-id="${lang}"
            aria-expanded="${expanded}" aria-controls="channels-${lang}">
            ${badge(lang)}<span>${countries[market.country]}</span>
            <small>${numberFormatter.format(accounts.length)}</small><span class="chevron" aria-hidden="true">⌄</span>
          </button></h2>
          <div id="channels-${lang}" ${expanded ? '' : 'hidden'}>
            ${accounts.length ? accounts.map(accountCard).join('') : '<p class="account-empty">Каналов пока нет</p>'}
            <div class="account-footer"><button data-do="account-new" data-id="${lang}">+ Добавить канал</button>
              <small>Вариант названия: ${escape(defaults[lang])}</small></div>
          </div></section>`;
      }).join('')}</div>`;
  }

  function connectionsPage() {
    return `<div class="heading"><div><h1>Подключения</h1><p>${data.live ? 'Доступность API и страна выхода' : 'Проверки выполняются на рабочем сервере'}</p></div></div>
      <div class="route-grid">${Object.entries(countries).map(([route, title]) => {
        const rows = platforms.map(platform => {
          const item = data.connections?.find(entry => entry.route === route && entry.platform === platform);
          const fallback = !data.live ? 'preview' : data.routes?.[route] ? 'unchecked' : 'unconfigured';
          const state = info && item?.checked_at && item.state !== 'unconfigured' ? 'stale' : item?.state || fallback;
          return {route, platform, ...item, state};
        });
        return `<section class="surface route-card"><div class="route-head"><h2>${title}</h2>
          <small>${route === 'server' ? 'Страна определяется при проверке' : 'Ожидаемая страна: ' + route}</small></div>
          ${rows.map(item => {
            const checked = item.checked_at && Number.isFinite(Date.parse(item.checked_at))
              ? checkFormatter.format(new Date(item.checked_at)) + ' МСК' : '';
            const meta = [item.country && 'Выход: ' + escape(item.country), checked, escape(item.detail)].filter(Boolean).join(' · ');
            return `<div class="route-row"><div><strong>${platformName(item.platform)}</strong>
              ${meta ? '<small>' + meta + '</small>' : ''}</div>
              <div class="route-actions">${healthBadge(item.state)}
                <button class="quiet" data-do="check-route" data-id="${item.route}/${item.platform}"
                  aria-label="Проверить: ${title}, ${platformName(item.platform)}"
                  ${data.live && !info ? '' : 'disabled'}>Проверить</button></div></div>`;
          }).join('')}</section>`;
      }).join('')}</div>`;
  }

  function render() {
    const pages = {queue: queuePage, sources: sourcesPage, accounts: accountsPage, connections: connectionsPage};
    const connection = info || (!data.live ? 'Предпросмотр' : data.settings.paused ? 'Отправка на паузе' : 'Личная панель');
    app.innerHTML = `<header><div class="top wrap">
      <a class="brand" href="#queue" aria-label="Streamcut, очередь"><span>↗</span> streamcut</a>
      <nav aria-label="Разделы">${Object.entries(sections).map(([id, label]) =>
        '<a href="#' + id + '"' + (page === id ? ' aria-current="page"' : '') + '>' + label + '</a>').join('')}</nav>
      <span class="connected ${data.live && !info ? 'on' : ''}">${escape(connection)}</span>
      </div></header><main class="wrap">${pages[page]()}</main>`;
    app.setAttribute('aria-busy', 'false');
  }

  function open(title, body) {
    dialog.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">${escape(title)}</h2>
      <button class="quiet" data-do="close" aria-label="Закрыть">×</button></div>${body}`;
    if (!dialog.open) dialog.showModal();
  }

  function files(lang) {
    return '<option value="">Без файла</option>' + (data.files?.[lang] || []).map(name => option(name, name)).join('');
  }

  async function sourceForm(id) {
    const source = data.sources.find(item => item.id === id);
    let profile = null;
    if (source) {
      const response = await fetch('/api/source-edit?' + new URLSearchParams({id}), {
        cache: 'no-store', signal: AbortSignal.timeout(3000),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || 'Не удалось загрузить настройки монтажа.');
      profile = result.edit;
    }
    const mode = profile?.mode || 'auto';
    const moments = (profile?.moments || []).map(({start_seconds, end_seconds, headline}) =>
      [start_seconds, end_seconds, headline].filter(value => value != null && value !== '').join(' — ')).join('\n');
    open(source ? 'Настроить монтаж' : 'Добавить источник', `<form id="source-form" data-id="${source?.id || ''}">
      ${['facecam_focus', 'gameplay_focus'].filter(field => profile?.[field]).map(field =>
        `<input name="${field}" type="hidden" value="${escape(profile[field])}">`).join('')}
      ${!data.live ? '<p class="muted">В предпросмотре доступен только интерфейс. Обработку запускает локальный сервис.</p>' : ''}
      <div class="form-columns">
        <label>Язык<select name="lang" ${source ? 'disabled' : ''}>${Object.entries(languages).map(([lang, text]) => option(lang, text, source?.lang)).join('')}</select></label>
        <label>Название источника<input name="title" maxlength="140" value="${escape(source?.title)}"></label>
      </div>
      ${!source || source.url ? `<label>Ссылка YouTube или Twitch<input name="url" type="url" maxlength="2048" placeholder="https://…" value="${escape(source?.url)}" ${source ? 'readonly' : ''}></label>` : ''}
      ${source?.rel_file ? `<small>Исходник: ${escape(source.rel_file)}</small>` : `
        <label>Видеофайл<input name="upload" type="file" accept=".mp4,.mkv,.mov,.webm,.ts" ${data.live ? '' : 'disabled'}></label>
        <details class="form-details"><summary>Выбрать файл на сервере</summary>
          <label>Входящий файл<select name="file">${files(source?.lang || 'RU')}</select></label>
        </details>`}
      <details class="form-details" ${data.live ? 'open' : ''}><summary>Монтаж реакции</summary>
        <div class="source-fields">
          <div class="form-columns">
            <label>Веб-камера<input name="facecam" maxlength="100" placeholder="74%,70%,25%,29%" value="${escape(profile?.facecam)}" aria-describedby="region-help"></label>
            <label>Геймплей (по желанию)<input name="gameplay" maxlength="100" placeholder="0%,0%,74%,100%" value="${escape(profile?.gameplay)}" aria-describedby="region-help"></label>
          </div>
          <small id="region-help">Отступ слева, отступ сверху, ширина и высота в пикселях или процентах.</small>
          <label>Заголовок на видео · 3–5 слов<input name="headline" maxlength="50" placeholder="Погоня через весь город" value="${escape(profile?.headline)}"></label>
          <label>Выбор моментов<select name="mode">${Object.entries(editModes).map(([value, label]) => option(value, label, mode)).join('')}</select></label>
          <div data-manual-moments ${mode === 'manual' ? '' : 'hidden'}>
            <label>Начало и конец клипов<textarea name="moments" rows="3" maxlength="800" placeholder="00:12:15 — 00:12:43 — Погоня через весь город" aria-describedby="moments-help">${escape(moments)}</textarea></label>
            <small id="moments-help">От 1 до 6 строк. Время в секундах, ММ:СС или ЧЧ:ММ:СС. Третьим полем можно указать заголовок.</small>
          </div>
          <label>Расшифровка с таймкодами (SRT)<textarea name="transcript" rows="4" maxlength="524288" placeholder="Вставьте сюда текст SRT">${escape(profile?.transcript)}</textarea></label>
          <label>Или выберите файл SRT<input name="transcript_file" type="file" accept=".srt"></label>
          <small>Для точных интервалов и нейросети нужна расшифровка SRT.</small>
          <details data-auto-mark ${mode === 'auto' ? '' : 'hidden'}><summary>Начать поиск рядом с таймкодом</summary>
            <label>Секунда исходника<input name="mark" type="number" min="0" max="86400" value="${escape(source?.mark)}"></label>
          </details>
        </div>
      </details>
      <p class="form-error" role="status"></p>
      <div class="footer"><button class="primary" ${data.live ? '' : 'disabled'}>Сохранить источник</button></div></form>`);
  }

  function parseMomentTime(timecode) {
    const parts = timecode.replace(',', '.').split(':').map(Number);
    if (parts.slice(1).some(part => part >= 60)) throw new Error('Минуты и секунды таймкода должны быть меньше 60.');
    return parts.reduce((time, part) => time * 60 + part, 0);
  }

  function parseMoments(raw) {
    const lines = raw.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
    if (!lines.length || lines.length > 6) throw new Error('Укажите от 1 до 6 интервалов, по одному на строку.');
    const timecode = '[0-9]+(?::[0-9]{1,2}){0,2}(?:[.,][0-9]+)?';
    const interval = new RegExp('^(' + timecode + ')\\s*[-–—]\\s*(' + timecode + ')(?:\\s*[-–—]\\s*(.+))?$');
    return lines.map((line, index) => {
      const match = line.match(interval);
      if (!match) throw new Error('Интервал ' + (index + 1) + ' нужен в формате «начало — конец».');
      const [, start, end, headline] = match;
      const start_seconds = parseMomentTime(start);
      const end_seconds = parseMomentTime(end);
      if (start_seconds >= end_seconds || end_seconds > 86400) throw new Error('Укажите начало раньше конца и время в пределах суток.');
      return {start_seconds, end_seconds, ...(headline ? {headline} : {})};
    });
  }

  async function sourceProfile(payload) {
    const {mode, transcript_file, moments, facecam_focus, gameplay_focus} = payload;
    const facecam = payload.facecam.trim();
    const gameplay = payload.gameplay.trim();
    const headline = payload.headline.trim();
    let transcript = payload.transcript.trim();
    if (transcript_file?.name) {
      if (!transcript_file.size || transcript_file.size > maxTranscriptBytes) throw new Error('Размер файла SRT должен быть от 1 байта до 512 КиБ.');
      try {
        transcript = new TextDecoder('utf-8', {fatal: true}).decode(await transcript_file.arrayBuffer()).trim();
      } catch {
        throw new Error('Сохраните SRT в кодировке UTF-8.');
      }
    }
    if (!facecam && !gameplay && !headline && !transcript && mode === 'auto') return null;
    if (!facecam || !headline) throw new Error('Для монтажа реакции укажите область веб-камеры и заголовок.');
    if (mode !== 'auto' && !transcript) throw new Error('Добавьте расшифровку SRT для выбранного способа монтажа.');
    if (new TextEncoder().encode(transcript).length > maxTranscriptBytes) throw new Error('Расшифровка SRT должна быть не больше 512 КиБ.');
    return {facecam, headline, mode, ...(gameplay ? {gameplay} : {}), ...(transcript ? {transcript} : {}),
      ...(facecam_focus ? {facecam_focus} : {}), ...(gameplay_focus ? {gameplay_focus} : {}),
      ...(mode === 'manual' ? {moments: parseMoments(moments)} : {})};
  }

  async function saveSource(form, payload, feedback) {
    const {title, upload, file, mark} = payload;
    const id = Number(form.dataset.id);
    const lang = form.elements.lang.value;
    const url = payload.url || '';
    const sourceTitle = title.trim() || upload?.name?.replace(/\.[^.]+$/, '').slice(0, 140) || '';
    const metadata = {lang, url, title: sourceTitle, mark: payload.mode === 'auto' && mark !== '' ? Number(mark) : null,
      ...(id ? {id} : {}), ...(file ? {file} : {})};
    const profile = await sourceProfile(payload);
    if (profile) metadata.edit = profile;
    if (!upload?.name) {
      await post('sources', metadata);
      return;
    }
    if (file) throw new Error('Выберите один видеофайл.');
    if (!upload.size || upload.size > 2 * 1024 ** 3) throw new Error('Размер видео должен быть от 1 байта до 2 ГиБ.');
    const prepared = await post('sources', {...metadata, prepare_upload: true, name: upload.name});
    if (!prepared.id) throw new Error('Сервис не подготовил загрузку исходника. Обновите версию панели.');
    form.dataset.id = prepared.id;
    feedback.textContent = 'Загружаю видео…';
    await uploadSource(upload, {...metadata, id: prepared.id});
  }

  function routeOptions(selected) {
    return Object.entries(countries).map(([id, name]) => option(
      id, name + (data.routes && !data.routes[id] ? ' · не настроен' : ''), selected,
    )).join('');
  }

  function targetLabel(platform) {
    return platform === 'YouTube' ? 'ID канала' : 'Имя аккаунта';
  }

  function accountForm(account, initialLang = 'RU') {
    const lang = account?.lang || initialLang;
    const platform = account?.platform || accountPlatform;
    const available = platform === 'YouTube' && data.oauth && account;
    open(account ? 'Настроить канал' : 'Добавить канал', `<form id="account-form" data-id="${escape(account?.id)}">
      ${!data.live ? '<p class="muted">Каналы сохраняются в рабочей панели.</p>' : ''}
      <div class="form-columns">
        <label>Площадка<select name="platform" ${account ? 'disabled' : ''}>
          ${platforms.map(value => option(value, platformName(value), platform)).join('')}</select></label>
        <label>Аудитория<select name="lang" ${account ? 'disabled' : ''}>
          ${Object.entries(markets).map(([code, market]) => option(code, countries[market.country] + ' · ' + code, lang)).join('')}</select></label>
      </div>
      ${account ? '<small>Карточка № ' + escape(account.id) + '</small>' : ''}
      <label>Название<input name="name" required maxlength="100" value="${escape(account?.name || defaults[lang])}"></label>
      <label><span data-label="target">${targetLabel(platform)}</span><input name="target" required maxlength="100" placeholder="${platform === 'YouTube' ? 'UC…' : '@имя'}" value="${escape(account?.target)}"></label>
      <label>Маршрут отправки<select name="route">${routeOptions(account?.route || 'server')}</select></label>
      <div class="button-row"><button type="button" data-do="check" ${data.live ? '' : 'disabled'}>Проверить соединение</button>
        <button class="primary" ${data.live ? '' : 'disabled'}>Сохранить</button></div>
      <p class="form-error" role="status"></p>
      ${account && platform === 'YouTube' ? `<div class="integration">
        <button type="button" data-do="oauth" data-id="${account?.id || ''}" ${available ? '' : 'disabled'}>${account?.connected ? 'Переподключить Google' : 'Подключить Google'}</button>
        ${account?.connected && data.uploads ? '<button type="button" data-do="auto" data-id="' + account.id + '">' + (account.auto ? 'Выключить автопланирование' : 'Включить автопланирование') + '</button>' : ''}
        <small>${!data.oauth ? 'Для подключения нужны настройки Google OAuth на сервере.' : 'Видео доступно в YouTube Studio после предварительной загрузки.'}</small>
        ${data.live && !account.connected ? '<details><summary>Настройки входа Google</summary><p>Адрес возврата <code>' +
          escape(location.origin + '/oauth/youtube') + '</code></p><small>Для проекта в режиме Testing добавьте владельца канала в Test users.</small></details>' : ''}
        </div>` : account ? '<small>Готовое видео можно скачать из очереди для ручной публикации.</small>' : ''}
      </form>`);
  }

  function preview(id) {
    const url = data.live ? '/media/' + id : 'assets/clip-preview.mp4';
    const clip = data.clips?.find(row => row.id === id);
    open(clip?.title || 'Видеофайл', `<div class="preview"><video controls playsinline src="${escape(url)}"></video>
      <a class="button" href="${escape(url)}" download="streamcut-${id || 'example'}.mp4">Скачать MP4</a>
      ${clip?.description ? '<p class="clip-description">' + escape(clip.description) + '</p>' : ''}
      ${Array.isArray(clip?.hashtags) && clip.hashtags.length ? '<small>' + clip.hashtags.map(escape).join(' ') + '</small>' : ''}
      </div>`);
  }

  function edit(id) {
    const job = data.queue.find(row => row.id === id);
    if (!job) return;
    const onYouTube = job.status === 'preloaded';
    const ready = job.platform === 'YouTube' && data.uploads && data.oauth && jobAccount(job)?.connected;
    const instant = new Date(jobTime(job));
    const current = instant > new Date() ? instant : new Date(Date.now() + 3600000);
    open(job.title, `<form id="job-form" data-id="${id}">
      <p class="muted">${escape(platformName(job.platform))} · [${job.lang}] ${escape(job.account)}</p>
      <div class="button-row"><button type="button" data-do="preview" data-id="${job.clip_id}">Смотреть</button>
        <a class="button" href="/media/${job.clip_id}" download="streamcut-${job.clip_id}.mp4">Скачать</a></div>
      <label>Дата и время · ${markets[job.lang].city}<input name="at" type="datetime-local" required value="${localFormatters[job.lang].format(current).replace(' ', 'T')}"></label>
      ${!ready ? '<small>Для переноса или планирования на YouTube нужно подключить Google и включить отправку.</small>' : ''}
      <p class="form-error" role="status"></p>
      <div class="button-row">
        ${onYouTube ? '<button class="primary" name="action" value="save"' + (ready ? '' : ' disabled') + '>Перенести в YouTube</button>' :
          '<button type="button" class="quiet" data-do="cancel-job" data-id="' + id + '">Снять с очереди</button>' +
          '<button name="action" value="save">Сохранить время</button>' +
          (ready ? '<button class="primary" name="action" value="send">Запланировать на YouTube</button>' : '')}
      </div>
      </form>`);
  }

  async function perform(button) {
    const action = button.dataset.do;
    const value = button.dataset.id;
    const id = Number(value);
    if (action === 'filter') {
      filter = value;
      render();
      app.querySelector(`.${button.classList.contains('market') ? 'market-grid' : 'bar'} [data-do="filter"][data-id="${value}"]`)?.focus();
    } else if (action === 'platform') {
      accountPlatform = value;
      render();
      app.querySelector(`[data-do="platform"][data-id="${value}"]`)?.focus();
    } else if (action === 'account-group') {
      if (openGroups.has(value)) openGroups.delete(value);
      else openGroups.add(value);
      button.setAttribute('aria-expanded', openGroups.has(value));
      document.getElementById('channels-' + value).hidden = !openGroups.has(value);
    } else if (action === 'source') await sourceForm(id);
    else if (action === 'account') {
      const account = data.accounts.find(row => row.id === id);
      if (account) accountForm(account);
    } else if (action === 'account-new') accountForm(null, value || 'RU');
    else if (action === 'edit') edit(id);
    else if (action === 'preview') preview(id);
    else if (action === 'close') dialog.close();
    else if (action === 'pause' || action === 'scan') {
      const name = action === 'pause' ? 'paused' : 'scan';
      await post('settings', {name, enabled: !data.settings[name]});
    } else if (action === 'check' || action === 'check-route') {
      const form = action === 'check' ? dialog.querySelector('form') : null;
      const [route, platform] = form ? [form.elements.route.value, form.elements.platform.value] : value.split('/');
      const result = await post('check', {route, platform});
      const message = result.state && result.state !== 'ok' ? result.detail || healthLabels[result.state]
        : 'Выход: ' + result.country + ' · API отвечает';
      if (form) form.querySelector('.form-error').textContent = message;
      else notify(message);
    } else if (action === 'oauth') {
      const response = await post('oauth', {id});
      location.assign(response.url);
    } else if (action === 'auto') {
      const account = data.accounts.find(row => row.id === id);
      if (!account.auto && !confirm('Включить автоматическое планирование для новых клипов этого языка?')) return;
      await post('auto', {id, enabled: !account.auto});
      dialog.close();
    } else if (action === 'cancel-job') {
      await post('queue', {id, action: 'cancel'});
      dialog.close();
    } else if (action === 'dismiss') await post('sources/dismiss', {id});
  }

  document.addEventListener('click', async event => {
    const button = event.target.closest('button[data-do]');
    if (!button) return;
    if (button.dataset.do === 'check') dialog.querySelector('.form-error').textContent = 'Проверяю маршрут…';
    if (button.dataset.do === 'check-route') notify('Проверяю маршрут…');
    button.disabled = true;
    try {
      await perform(button);
      if (data.live) await refresh();
    } catch (error) {
      const feedback = dialog.open && dialog.querySelector('.form-error');
      if (feedback) feedback.textContent = error.message;
      else notify(error.message);
      if (data.live && ['check', 'check-route'].includes(button.dataset.do)) await refresh();
    } finally {
      button.disabled = false;
    }
  });

  dialog.addEventListener('change', event => {
    const form = event.target.form;
    if (form?.id === 'source-form') {
      if (event.target.name === 'lang' && form.elements.file) {
        form.elements.file.innerHTML = files(event.target.value);
      } else if (event.target.name === 'mode') {
        form.querySelector('[data-manual-moments]').hidden = event.target.value !== 'manual';
        form.querySelector('[data-auto-mark]').hidden = event.target.value !== 'auto';
      }
    }
    if (form?.id === 'account-form') {
      if (event.target.name === 'lang') {
        if (Object.values(defaults).includes(form.elements.name.value)) {
          form.elements.name.value = defaults[event.target.value];
        }
      } else if (event.target.name === 'platform') {
        form.querySelector('[data-label="target"]').textContent = targetLabel(event.target.value);
        form.elements.target.placeholder = event.target.value === 'YouTube' ? 'UC…' : '@имя';
      }
    }
  });
  dialog.addEventListener('close', () => dialog.querySelector('video')?.pause());

  dialog.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.target;
    const payload = Object.fromEntries(new FormData(form));
    const error = form.querySelector('.form-error');
    const enabled = [...form.querySelectorAll('button')].filter(button => !button.disabled);
    for (const button of enabled) button.disabled = true;
    try {
      if (form.id === 'source-form') {
        await saveSource(form, payload, error);
        location.hash = 'sources';
      } else if (form.id === 'account-form') {
        const {lang, platform} = form.elements;
        await post('accounts', {
          ...payload, lang: lang.value, platform: platform.value,
          ...(form.dataset.id ? {id: Number(form.dataset.id)} : {}),
        });
        accountPlatform = platform.value;
        openGroups.add(lang.value);
      } else if (form.id === 'job-form') {
        await post('queue', {...payload, id: Number(form.dataset.id), action: event.submitter?.value || 'save'});
      }
      dialog.close();
      await refresh();
      notify('Сохранено');
    } catch (problem) {
      error.textContent = problem.message;
    } finally {
      for (const button of enabled) button.disabled = false;
      if (form.id === 'account-form') {
        const oauth = form.querySelector('[data-do="oauth"]');
        if (oauth) oauth.disabled = !data.oauth || !oauth.dataset.id;
      }
    }
  });

  function go() {
    const requested = location.hash.slice(1);
    page = Object.hasOwn(sections, requested) ? requested : 'queue';
    render();
  }

  window.addEventListener('hashchange', go);
  go();
  const oauth = new URLSearchParams(location.search).get('oauth');
  if (oauth) {
    history.replaceState(null, '', location.pathname + '#accounts');
    notify(oauth === 'ok' ? 'Google подключён' : oauth);
  }
  refresh();
  if (location.protocol !== 'file:') setInterval(() => { if (connectedOnce) refresh(); }, 4000);
})();
