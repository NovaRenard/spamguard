'use strict';
// GET /api/data — данные для страницы. Обычный пользователь видит только свои цифры, админ ещё и общую статистику.
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
  if (who.r === 'admin') out.global = snap.global;  // общая статистика только админу
  return send(res, 200, out);
};
