import test from 'node:test';
import assert from 'node:assert/strict';
import {parseSource,assertLanguage,scheduleISO,sortedQueue,dayInZone,esc} from '../dist/core.mjs';

test('YouTube URL variants deduplicate to the same key',()=>{
 const urls=['https://youtu.be/abcdefghijk?t=3','https://www.youtube.com/watch?v=abcdefghijk&feature=share','https://youtube.com/live/abcdefghijk','https://m.youtube.com/shorts/abcdefghijk'];
 assert.equal(new Set(urls.map(u=>parseSource(u).key)).size,1);
 assert.equal(parseSource(urls[0]).url,'https://www.youtube.com/watch?v=abcdefghijk');
});
test('Reject spoofed hosts, credentials and unsupported inputs',()=>{
 for(const u of ['https://youtube.com.evil.test/watch?v=abcdefghijk','https://youtube.com@evil.test/watch?v=abcdefghijk','http://youtube.com/watch?v=abcdefghijk','javascript:alert(1)','https://twitch.tv/directory','https://youtube.com/@someone','https://youtu.be/short'])assert.throws(()=>parseSource(u));
});
test('Twitch VOD and live channel have distinct normalized keys',()=>{
 assert.equal(parseSource('https://twitch.tv/videos/1234567').key,'twitch:vod:1234567');
 assert.equal(parseSource('https://www.twitch.tv/RockstarGames/').key,'twitch:live:rockstargames');
});
test('Language routing fails closed',()=>{
 for(const lang of ['RU','EN','ESP'])assert.doesNotThrow(()=>assertLanguage(lang,lang));
 assert.throws(()=>assertLanguage('RU','EN'));assert.throws(()=>assertLanguage('ES','ESP'));assert.throws(()=>assertLanguage('',''));
});
test('Schedule normalizes Moscow time and rejects invalid calendar dates',()=>{
 assert.equal(scheduleISO('2026-11-18','20:45','+03:00'),'2026-11-18T17:45:00.000Z');
 for(const [d,t] of [['2026-02-30','10:00'],['2026-11-18','25:00'],['bad','12:00']])assert.throws(()=>scheduleISO(d,t,'+03:00'));
});
test('Queue filters date in selected timezone and sorts without mutating',()=>{
 const items=[{id:1,lang:'EN',platform:'YouTube',at:'2026-11-19T01:00:00.000Z'},{id:2,lang:'RU',platform:'Reels',at:'2026-11-18T17:00:00.000Z'},{id:3,lang:'EN',platform:'YouTube',at:'2026-11-18T15:00:00.000Z'}];
 const result=sortedQueue(items,{lang:'EN',platform:'YouTube',date:'2026-11-18',zone:'America/New_York'});
 assert.deepEqual(result.map(x=>x.id),[3,1]);assert.deepEqual(items.map(x=>x.id),[1,2,3]);
 assert.equal(dayInZone(items[0].at,'Europe/Moscow'),'2026-11-19');
});
test('User-controlled labels are escaped for HTML',()=>assert.equal(esc('<img src=x onerror="x"> &'), '&lt;img src=x onerror=&quot;x&quot;&gt; &amp;'));
