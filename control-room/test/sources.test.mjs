import assert from 'node:assert/strict';
import {File} from 'node:buffer';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {runInNewContext} from 'node:vm';

const script = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
const transcript = '1\n00:00:01,000 --> 00:00:12,000\nМомент в центре города.';
const response = (body, ok = true) => ({
  ok, headers: new Headers({'Content-Type': 'application/json'}), json: async () => body,
});

function element(properties = {}) {
  const handlers = new Map();
  return {
    addEventListener: (type, handler) => handlers.set(type, handler),
    fire: (type, event) => handlers.get(type)(event),
    ...properties,
  };
}

async function panel(api, state = {}) {
  const app = element({setAttribute() {}});
  const dialog = element({
    open: false,
    showModal() { this.open = true; },
    close() { this.open = false; },
    querySelector: () => null,
  });
  const toast = {classList: {add() {}}};
  const document = element({querySelector: selector => ({'#app': app, '#dialog': dialog, '#toast': toast})[selector]});
  const location = {protocol: 'http:', hash: '#sources', search: '', origin: 'http://127.0.0.1:8766'};
  const window = element();
  const data = {live: true, settings: {scan: true}, clips: [], queue: [], sources: [], accounts: [], ...state};
  runInNewContext(script, {
    document, location, window, AbortSignal, URLSearchParams, TextEncoder, TextDecoder,
    clearTimeout() {}, setInterval() {}, setTimeout() {},
    FormData: class extends Map {
      constructor(form) { super(Object.entries(form.fields)); }
    },
    fetch: (path, options) => path === '/api/state' ? Promise.resolve(response(data)) : api(path, options),
  });
  await new Promise(setImmediate);
  const navigate = page => {
    location.hash = '#' + page;
    window.fire('hashchange');
  };
  return {app, dialog, document, toast, navigate};
}

function sourceForm(fields, id = '') {
  const feedback = {};
  return {
    id: 'source-form',
    dataset: {id},
    fields: {url: '', title: '', facecam: '', gameplay: '', headline: '', transcript: '', mode: 'auto', mark: '', ...fields},
    elements: {lang: {value: 'RU'}},
    querySelector: () => feedback,
    querySelectorAll: () => [],
    feedback,
  };
}

const submit = (dialog, form) => dialog.fire('submit', {target: form, preventDefault() {}});

test('the source form saves pasted SRT, camera, and exact intervals as JSON', async () => {
  const focus = {facecam_focus: '55%,40%', gameplay_focus: '60%,50%'};
  const profile = {facecam: '74%,70%,25%,29%', headline: 'Погоня через весь город', ...focus};
  const url = 'https://www.youtube.com/watch?v=ABC12345678';
  const source = {id: 7, lang: 'RU', title: 'Эфир', url, status: 'waiting_layout', rel_file: 'source.mp4'};
  let saved;
  const {dialog, document} = await panel(async (path, options) => {
    if (path === '/api/source-edit?id=7') return response({edit: {...profile, mode: 'auto'}});
    assert.equal(path, '/api/sources');
    saved = JSON.parse(options.body);
    return response({id: 7});
  }, {sources: [source]});
  const button = {dataset: {do: 'source', id: '7'}};
  await document.fire('click', {target: {closest: () => button}});
  assert.match(dialog.innerHTML, /name="facecam_focus" type="hidden" value="55%,40%"/);
  const form = sourceForm({
    ...profile, url, title: 'Эфир', transcript, mode: 'manual', mark: '2',
    moments: '00:00:01,25 — 00:00:10 — Погоня в центре',
  }, '7');
  await submit(dialog, form);
  assert.deepEqual(saved, {id: 7, url, lang: 'RU', title: 'Эфир', mark: null, edit: {
    ...profile, transcript, mode: 'manual',
    moments: [{start_seconds: 1.25, end_seconds: 10, headline: 'Погоня в центре'}],
  }});
  assert.equal(dialog.open, false);
});

test('binary upload keeps SRT in JSON and retries against the same source', async () => {
  const preparations = [];
  const uploads = [];
  const {dialog} = await panel(async (path, options) => {
    if (path === '/api/sources') {
      preparations.push(JSON.parse(options.body));
      return response({id: 21});
    }
    const endpoint = new URL(path, 'http://127.0.0.1');
    assert.equal(endpoint.pathname, '/api/inbox');
    uploads.push({query: Object.fromEntries(endpoint.searchParams), body: options.body});
    return uploads.length === 1 ? response({error: 'Загрузка прервалась.'}, false) : response({id: 21});
  });
  const upload = new File(['video bytes'], 'stream.mp4');
  const form = sourceForm({
    upload, transcript_file: new File([transcript], 'stream.srt'),
    mode: 'model', facecam: '74%,70%,25%,29%', headline: 'Погоня через весь город',
  });
  await submit(dialog, form);
  assert.equal(form.feedback.textContent, 'Загрузка прервалась.');
  assert.equal(form.dataset.id, 21);
  await submit(dialog, form);
  assert.equal(preparations.length, 2);
  assert.equal(preparations[0].prepare_upload, true);
  assert.equal(preparations[0].edit.transcript, transcript);
  assert.equal(preparations[0].edit.mode, 'model');
  assert.equal(preparations[1].id, 21);
  for (const attempt of uploads) {
    assert.deepEqual(attempt.query, {id: '21', name: 'stream.mp4', lang: 'RU', title: 'stream', url: ''});
    assert.equal(attempt.body, upload);
  }
});

test('a source with no accounts can expose its clips and the download link', async () => {
  const {app, dialog, document} = await panel(() => {
    assert.fail('preview should reuse the loaded clip metadata');
  }, {
    sources: [{id: 1, lang: 'RU', title: 'Эфир', status: 'ready'}],
    clips: [{id: 2, source_id: 1, lang: 'RU', title: 'Погоня через весь город'}],
  });
  assert.match(app.innerHTML, /Погоня через весь город/);
  assert.match(app.innerHTML, /data-do="preview" data-id="2"/);
  await document.fire('click', {target: {closest: () => ({dataset: {do: 'preview', id: '2'}})}});
  assert.match(dialog.innerHTML, /<video controls playsinline src="\/media\/2"/);
  assert.match(dialog.innerHTML, /href="\/media\/2" download="streamcut-2\.mp4"/);
});

test('discovered Twitch sources show their acquisition stage', async () => {
  const media = [
    ['twitch_wait_archive', 'Ожидает архив Twitch'],
    ['twitch_wait_access', 'Нужен доступ к исходнику'],
    ['twitch_wait_media', 'Ожидает автополучения'],
    ['twitch_media_error', 'Ошибка автополучения'],
  ];
  const sources = media.map(([media_state], index) => ({
    id: index + 1, lang: 'RU', title: 'Запись', status: 'waiting', media_state, detail: 'Помощь <пользователю>',
  }));
  const {app} = await panel(() => assert.fail('source display does not fetch media'), {sources});
  for (const [state, label] of media) {
    assert.match(app.innerHTML, new RegExp('status ' + state + '">' + label));
  }
  assert.match(app.innerHTML, /Помощь &lt;пользователю&gt;/);
  assert.doesNotMatch(app.innerHTML, /Нужен исходный файл/);
});

test('strict RU Sources shows only files accepted for automatic editing', async () => {
  const fixtures = [
    ['Скрытый эфир', 'twitch_wait_archive'],
    ['Скрытая запись без доступа', 'twitch_wait_access'],
    ['Скрытая запись без проверки', 'twitch_wait_check'],
    ['Ссылка добавлена вручную', undefined, {manual: true}],
    ['Ожидает настройки монтажа', undefined, {status: 'waiting_layout', rel_file: 'manual.mp4'}],
    ['Готовый источник', undefined, {status: 'ready', rel_file: 'ready.mp4'}],
    ['Файл ещё не получен', undefined, {status: 'queued'}],
    ['Файл в очереди', undefined, {status: 'queued', rel_file: 'queue.mp4'}],
    ['Файл в монтаже', undefined, {status: 'editing', rel_file: 'editing.mp4'}],
  ];
  const {app, navigate} = await panel(() => assert.fail('source filtering is display only'), {
    strict_ru_sources: true,
    roster: {RU: ['Проверенный канал'], EN: ['Foreign channel']},
    ranking_updated: {RU: '2026-11-19T08:00:00+00:00'},
    sources: fixtures.map(([title, media_state, extra], index) =>
      ({id: index + 1, lang: 'RU', title, media_state, status: 'waiting', ...extra})),
    clips: [{id: 2, source_id: 6, lang: 'RU', title: 'Готовый клип'}],
    queue: [{id: 3, clip_id: 2, status: 'preloaded', lang: 'RU', platform: 'YouTube',
      title: 'Клип в расписании', at: '2026-11-19T12:00:00+00:00'}],
  });
  for (const [title] of fixtures.slice(0, -2)) assert.doesNotMatch(app.innerHTML, new RegExp(title));
  for (const [title] of fixtures.slice(-2)) assert.match(app.innerHTML, new RegExp(title));
  assert.match(app.innerHTML, /В обработке · 2/);
  assert.doesNotMatch(app.innerHTML, /Проверенный канал|Foreign channel|Готовый клип|Настроить монтаж|Добавить исходник|\+ Источник/);
  navigate('queue');
  assert.match(app.innerHTML, /Клип в расписании/);
  assert.match(app.innerHTML, /\+ Источник/);
});

test('strict RU Sources reports an empty workflow with the last Twitch check', async () => {
  const {app, navigate} = await panel(() => assert.fail('local display only'), {
    strict_ru_sources: true,
    search_configured: true,
    ranking_updated: {RU: '2026-11-19T08:00:00+00:00'},
    source_health: {archives: 63, accessible: 0, active: 0},
    queue_counts: {RU: 30, EN: 0, ESP: 0},
    inventory_limit: 30,
  });
  assert.match(app.innerHTML, /Последняя проверка Twitch · 19\.11\.2026, 11:00/);
  assert.match(app.innerHTML, /Пока нет материалов для монтажа/);
  assert.match(app.innerHTML, /Автопоиск продолжается/);
  assert.doesNotMatch(app.innerHTML, /Новых записей|доступ к файлу|настроено получение/);
  navigate('queue');
  assert.match(app.innerHTML, /Локальный запас: 30 из 30/);
  assert.doesNotMatch(app.innerHTML, /Локальный запас: 0 из 30/);
});

test('Sources shows only Russian media and rankings while Queue keeps other languages', async () => {
  const languages = [
    ['RU', 'Городской эфир', 'Первый русский клип', 'Быстрый стример'],
    ['EN', 'Foreign Stream', 'Foreign Clip', 'Foreign Creator'],
    ['ESP', 'Directo Español', 'Corte Español', 'Creador Español'],
  ];
  const {app, dialog, document, navigate} = await panel(() => assert.fail('local display only'), {
    search_configured: true,
    sources: languages.map(([lang, title], index) => ({id: index + 1, lang, title, status: 'ready'})),
    clips: languages.map(([lang, , title], index) => ({source_id: index + 1, id: index + 10, lang, title})),
    roster: Object.fromEntries(languages.map(([lang, , , name]) => [lang, [name]])),
    ranking_updated: Object.fromEntries(languages.map(([lang]) => [lang, '2026-11-19T08:00:00+00:00'])),
  });
  assert.match(app.innerHTML, /Городской эфир/);
  assert.match(app.innerHTML, /Первый русский клип/);
  assert.match(app.innerHTML, /Быстрый стример/);
  assert.doesNotMatch(app.innerHTML, /Foreign|Español/);

  await document.fire('click', {target: {closest: () => ({dataset: {do: 'source', id: ''}})}});
  assert.match(dialog.innerHTML, /<option value="RU" selected>Русский<\/option>/);
  assert.doesNotMatch(dialog.innerHTML, /<option value="(?:EN|ESP)"/);

  navigate('queue');
  assert.match(app.innerHTML, /Foreign Clip/);
  assert.match(app.innerHTML, /Corte Español/);
});

test('the upload gateway has a country-neutral check and explicit account selection', async () => {
  const account = {id: 1, name: 'Test account', lang: 'RU', platform: 'YouTube', route: 'server', connected: true};
  const {app, dialog, document, toast, navigate} = await panel(async (path, options) => {
    assert.equal(path, '/api/check');
    assert.deepEqual(JSON.parse(options.body), {route: 'gateway', platform: 'YouTube'});
    return response({state: 'ok', country: null});
  }, {
    accounts: [account], oauth: true, uploads: true, youtube_gateway_required: true,
    routes: {server: true, gateway: true},
    connections: [{route: 'gateway', platform: 'YouTube', state: 'ok', country: null}],
  });

  navigate('queue');
  assert.match(app.innerHTML, /Выберите доверенный шлюз в настройках канала YouTube RU/);
  navigate('connections');
  assert.match(app.innerHTML, /Доверенный шлюз<\/h2>\s*<small>Проверка доступа к API по HTTPS/);
  assert.doesNotMatch(app.innerHTML, /Ожидаемая страна: gateway|Выход: null/);
  await document.fire('click', {target: {closest: () => ({dataset: {do: 'check-route', id: 'gateway/YouTube'}})}});
  assert.equal(toast.textContent, 'API отвечает по HTTPS');

  navigate('accounts');
  await document.fire('click', {target: {closest: () => ({dataset: {do: 'account', id: '1'}})}});
  assert.match(dialog.innerHTML, /<option value="server" selected>Прямой маршрут<\/option>/);
  assert.match(dialog.innerHTML, /<option value="gateway">Доверенный шлюз<\/option>/);
  assert.match(dialog.innerHTML, /Для отправки в YouTube требуется доверенный шлюз/);
});
