export const LANGS = ['RU','EN','ESP'];
export const PLATFORMS = ['YouTube','TikTok','Reels'];
export const ZONES = ['Europe/Moscow','Europe/Madrid','America/New_York'];
export function parseSource(raw) {
  let url;
  try { url = new URL(raw.trim()); } catch { throw new Error('Введите полную ссылку на YouTube или Twitch.'); }
  if (url.protocol !== 'https:' || url.username || url.password) throw new Error('Нужна HTTPS-ссылка без логина и пароля.');
  const host = url.hostname.toLowerCase().replace(/^www\./,'');
  let id;
  if (['youtube.com','m.youtube.com','youtu.be'].includes(host)) {
    id = host === 'youtu.be' ? url.pathname.slice(1) : url.pathname === '/watch' ? url.searchParams.get('v') : url.pathname.match(/^\/(?:live|shorts|embed)\/([^/]+)\/?$/)?.[1];
    if (!id || !/^[\w-]{11}$/.test(id)) throw new Error('Укажите ссылку на конкретное видео YouTube.');
    return {platform:'YouTube',url:`https://www.youtube.com/watch?v=${id}`,key:`youtube:${id}`,kind:'Видео'};
  }
  if (host === 'twitch.tv') {
    const vod = url.pathname.match(/^\/videos\/(\d+)\/?$/);
    const channel = url.pathname.match(/^\/([a-zA-Z0-9_]{3,25})\/?$/);
    if (vod) return {platform:'Twitch',url:`https://www.twitch.tv/videos/${vod[1]}`,key:`twitch:vod:${vod[1]}`,kind:'Запись эфира'};
    if (channel && !['directory','settings','downloads','subscriptions','inventory','wallet','search','videos'].includes(channel[1].toLowerCase())) return {platform:'Twitch',url:`https://www.twitch.tv/${channel[1].toLowerCase()}`,key:`twitch:live:${channel[1].toLowerCase()}`,kind:'Канал / эфир'};
    throw new Error('Укажите канал Twitch или ссылку вида twitch.tv/videos/12345.');
  }
  throw new Error('Сейчас поддерживаются ссылки YouTube и Twitch.');
}
export function assertLanguage(clip, account) {
  if (!LANGS.includes(clip) || clip !== account) throw new Error('Язык клипа должен совпадать с языком аккаунта.');
}
export function scheduleISO(date,time,offset) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(time) || !/^[+-]\d{2}:\d{2}$/.test(offset)) throw new Error('Укажите дату и время.');
  const local = new Date(`${date}T${time}:00Z`);
  if (!Number.isFinite(local.getTime()) || local.toISOString().slice(0,16) !== `${date}T${time}`) throw new Error('Проверьте дату и время.');
  return new Date(`${date}T${time}:00${offset}`).toISOString();
}
export function dayInZone(iso,zone='Europe/Moscow') {
  return new Intl.DateTimeFormat('en-CA',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(iso));
}
export function sortedQueue(items,filter={}) {
  return items.filter(x=>(!filter.lang || x.lang===filter.lang) && (!filter.platform || x.platform===filter.platform) && (!filter.date || dayInZone(x.at,filter.zone)===filter.date)).sort((a,b)=>a.at.localeCompare(b.at));
}
export function nextDay() { return dayInZone(new Date(Date.now()+86400000).toISOString()); }
export function esc(value) { return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
