'use strict';
// GET /api/data — данные для страницы. Обычный пользователь видит только свои цифры.
// Админ дополнительно получает общую статистику, справочник ВСЕХ пользователей (имя, @username, id, режимы, цифры)
// и по запросу ?u=<псевдоним> подробные графики любого пользователя.
const { authenticate } = require('./_lib/auth');
const { getSnapshot } = require('./_lib/store');

const send = (res, code, obj) => { res.statusCode = code; res.setHeader('Content-Type', 'application/json; charset=utf-8'); res.setHeader('Cache-Control', 'no-store'); res.end(JSON.stringify(obj)); };

module.exports = async (req, res) => {
  if (req.method !== 'GET') return send(res, 405, { error: 'method' });
  const who = authenticate(req);
  if (!who) return send(res, 401, { error: 'unauthorized' });
  const snap = await getSnapshot();
  if (!snap) return send(res, 200, { role: who.r, empty: true });
  const out = { role: who.r, generated_at: snap.generated_at, tz: snap.tz, days: snap.days, me: snap.users[who.p] || null, myp: who.p };
  if (who.r === 'admin') {  // общая статистика и справочник пользователей только админу
    out.global = snap.global;
    out.admin = snap.admin || { directory: [] };
    const u = new URL(req.url, 'http://x').searchParams.get('u');
    if (u && snap.users[u]) { out.user = snap.users[u]; out.userp = u; }
  }
  return send(res, 200, out);
};
