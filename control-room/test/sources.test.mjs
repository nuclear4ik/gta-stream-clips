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
  const location = {protocol: 'http:', hash: '#sources', search: ''};
  const data = {live: true, settings: {scan: true}, clips: [], queue: [], sources: [], accounts: [], ...state};
  runInNewContext(script, {
    document, location, window: element(), AbortSignal, URLSearchParams, TextEncoder, TextDecoder,
    clearTimeout() {}, setInterval() {}, setTimeout() {},
    FormData: class extends Map {
      constructor(form) { super(Object.entries(form.fields)); }
    },
    fetch: (path, options) => path === '/api/state' ? Promise.resolve(response(data)) : api(path, options),
  });
  await new Promise(setImmediate);
  return {app, dialog, document, toast};
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
