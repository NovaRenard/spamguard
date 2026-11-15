// Тесты API сайта: node tests/test_api.js
'use strict';
const crypto = require('crypto');
const fs = require('fs');
const { EventEmitter } = require('events');

process.env.SITE_SECRET = 'site-secret';
process.env.INGEST_SECRET = 'ingest-secret';
process.env.BOT_TOKEN = '123456:TESTTOKENTESTTOKENTESTTOKENTESTTOKE';
process.env.PSEUDO_KEY = 'pseudo-key';
process.env.ADMIN_IDS = '555';
delete process.env.UPSTASH_REDIS_REST_URL; delete process.env.KV_REST_API_URL;

const auth = require('../api/_lib/auth');
const store = require('../api/_lib/store');
const ingest = require('../api/ingest');
const data = require('../api/data');

let fails = 0;
const check = (name, cond, extra = '') => { console.log(`  ${cond ? '✔' : '✘ ПРОВАЛ'} ${name} ${extra}`); if (!cond) fails++; };

function b64u(b) { return Buffer.from(b).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''); }
function mkToken(secret, p, r, ttl = 600) {
  const body = b64u(JSON.stringify({ p, r, e: Math.floor(Date.now() / 1000) + ttl }));
  return `${body}.${b64u(crypto.createHmac('sha256', secret).update(body).digest())}`;
}
function mkInitData(botToken, user, authDate = Math.floor(Date.now() / 1000)) {
  const params = new URLSearchParams({ auth_date: String(authDate), query_id: 'AAH', user: JSON.stringify(user) });
  const dcs = [...params.entries()].map(([k, v]) => `${k}=${v}`).sort().join('\n');
  const sk = crypto.createHmac('sha256', 'WebAppData').update(botToken).digest();
  params.set('hash', crypto.createHmac('sha256', sk).update(dcs).digest('hex'));
  return params.toString();
}
function call(handler, { method = 'GET', url = '/', headers = {}, body } = {}) {
  return new Promise((resolve) => {
    const req = new EventEmitter(); req.method = method; req.url = url; req.headers = headers;
    if (body !== undefined) req.body = body;
    const res = { statusCode: 200, headers: {}, setHeader(k, v) { this.headers[k.toLowerCase()] = v; }, end(txt) { resolve({ status: this.statusCode, json: txt ? JSON.parse(txt) : null, headers: this.headers, raw: txt }); } };
    handler(req, res);
  });
}

const PA = auth.pseudo(process.env.PSEUDO_KEY, 111), PB = auth.pseudo(process.env.PSEUDO_KEY, 222), PADMIN = auth.pseudo(process.env.PSEUDO_KEY, 555);
const day = (i) => ({ date: `2026-11-${10 + i}`, checked: 5, flagged: 2, blocked: 1, would_block: 1, warned: 0, fp: 0, known_link: 0, gambling: 1, scam: 1, spam: 0, tg: 5, wa: 0, group_left: 0 });
const snap = {
  v: 1, generated_at: 1700000000, tz: 5, days: ['2026-11-10', '2026-11-11'],
  global: { daily: [day(0), day(1)], top: { 30: { domains: [['1win-bonus.com', 3, 'gambling']] } }, accounts: { users: 3 }, table: [{ p: PA }, { p: PB }] },
  users: { [PA]: { daily: [day(0), day(1)], connected: { tg: true, wa: false } }, [PB]: { daily: [day(0)], connected: { tg: true, wa: false } }, [PADMIN]: { daily: [day(1)], connected: { tg: true, wa: false } } },
};

(async () => {
  try { fs.unlinkSync(store.FILE); } catch (_) {}

  console.log('\n[Токены и подпись Mini App]');
  check('валидный токен принимается', auth.verifyToken('site-secret', mkToken('site-secret', PA, 'user'))?.p === PA);
  check('чужой секрет отвергается', auth.verifyToken('site-secret', mkToken('other', PA, 'user')) === null);
  check('истёкший токен отвергается', auth.verifyToken('site-secret', mkToken('site-secret', PA, 'user', -5)) === null);
  check('мусор и пустое не роняют проверку', auth.verifyToken('site-secret', 'abc') === null && auth.verifyToken('site-secret', undefined) === null && auth.verifyToken('', 'a.b') === null);
  const good = mkInitData(process.env.BOT_TOKEN, { id: 111, first_name: 'A' });
  check('подпись Telegram Mini App принимается', auth.verifyInitData(process.env.BOT_TOKEN, good)?.id === 111);
  check('подделанная initData отвергается', auth.verifyInitData(process.env.BOT_TOKEN, good.replace('111', '112')) === null);
  check('initData с чужим токеном бота отвергается', auth.verifyInitData('999:OTHER', good) === null);
  check('устаревшая initData (>24 ч) отвергается', auth.verifyInitData(process.env.BOT_TOKEN, mkInitData(process.env.BOT_TOKEN, { id: 111 }, 1000)) === null);

  console.log('\n[POST /api/ingest]');
  let r = await call(ingest, { method: 'GET' });
  check('GET не разрешён (405)', r.status === 405);
  r = await call(ingest, { method: 'POST', body: snap });
  check('без ключа: 401', r.status === 401);
  r = await call(ingest, { method: 'POST', headers: { authorization: 'Bearer wrong' }, body: snap });
  check('с неверным ключом: 401', r.status === 401);
  r = await call(ingest, { method: 'POST', headers: { authorization: 'Bearer ingest-secret' }, body: { v: 2 } });
  check('кривой снимок: 400', r.status === 400);
  r = await call(ingest, { method: 'POST', headers: { authorization: 'Bearer ingest-secret' }, body: snap });
  check('верный ключ и снимок: 200', r.status === 200 && r.json.ok === true);
  r = await call(ingest, { method: 'POST', headers: { authorization: 'Bearer ingest-secret' }, body: 'x'.repeat(2.2 * 1024 * 1024) });
  check('слишком большое тело: 413', r.status === 413);

  console.log('\n[GET /api/data: кто что видит]');
  r = await call(data, {});
  check('без токена: 401', r.status === 401);
  r = await call(data, { headers: { 'x-token': 'bad.token' } });
  check('плохой токен: 401', r.status === 401);
  r = await call(data, { headers: { 'x-token': mkToken('site-secret', PA, 'user') } });
  check('пользователь: видит только свои данные', r.status === 200 && r.json.me && r.json.me.daily.length === 2 && r.json.role === 'user');
  check('пользователь: общей статистики НЕТ', r.json.global === undefined && !r.raw.includes(PB), '');
  r = await call(data, { url: `/api/data?t=${mkToken('site-secret', PA, 'user')}` });
  check('токен можно передать в адресе (?t=)', r.status === 200);
  r = await call(data, { headers: { 'x-token': mkToken('site-secret', PA, 'admin') } });
  check('админ: видит общую статистику', r.status === 200 && r.json.global && r.json.global.accounts.users === 3);
  r = await call(data, { headers: { 'x-token': mkToken('site-secret', 'unknownpseudo0001', 'user') } });
  check('неизвестный пользователь: пустые данные, не ошибка', r.status === 200 && r.json.me === null);
  r = await call(data, { headers: { 'x-tg-init-data': mkInitData(process.env.BOT_TOKEN, { id: 111 }) } });
  check('Mini App: пользователь определяется по подписи Telegram', r.status === 200 && r.json.myp === PA && r.json.role === 'user');
  r = await call(data, { headers: { 'x-tg-init-data': mkInitData(process.env.BOT_TOKEN, { id: 555 }) } });
  check('Mini App: администратор из ADMIN_IDS получает общую статистику', r.status === 200 && r.json.role === 'admin' && r.json.global);
  r = await call(data, { headers: { 'x-tg-init-data': mkInitData('999:OTHER', { id: 555 }) } });
  check('Mini App: подпись чужого бота отвергается', r.status === 401);
  r = await call(data, { method: 'POST', headers: { 'x-token': mkToken('site-secret', PA, 'user') } });
  check('POST на /api/data не разрешён', r.status === 405);
  check('ответ не кэшируется', (await call(data, { headers: { 'x-token': mkToken('site-secret', PA, 'user') } })).headers['cache-control'] === 'no-store');

  try { fs.unlinkSync(store.FILE); } catch (_) {}
  r = await call(data, { headers: { 'x-token': mkToken('site-secret', PA, 'user') } });
  check('данных ещё нет: понятный пустой ответ', r.status === 200 && r.json.empty === true);

  console.log('\n' + (fails ? `ПРОВАЛЕНО: ${fails}` : 'ВСЕ ТЕСТЫ ПРОШЛИ'));
  process.exit(fails ? 1 : 0);
})();
