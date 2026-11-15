'use strict';
// POST /api/ingest — приём снимка аналитики от бота. Защита: Bearer INGEST_SECRET (сравнение за постоянное время).
const { safeEqual } = require('./_lib/auth');
const { setSnapshot } = require('./_lib/store');

const MAX = 2 * 1024 * 1024;

function readBody(req) {
  if (req.body !== undefined && req.body !== null) {  // Vercel уже разобрал тело
    return Promise.resolve(typeof req.body === 'string' ? req.body : JSON.stringify(req.body));
  }
  return new Promise((resolve, reject) => {
    let n = 0; const chunks = [];
    req.on('data', (c) => { n += c.length; if (n > MAX) { reject(Object.assign(new Error('big'), { code: 413 })); req.destroy(); } else chunks.push(c); });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

const send = (res, code, obj) => { res.statusCode = code; res.setHeader('Content-Type', 'application/json; charset=utf-8'); res.end(JSON.stringify(obj)); };

module.exports = async (req, res) => {
  if (req.method !== 'POST') return send(res, 405, { error: 'method' });
  const secret = process.env.INGEST_SECRET;
  const bearer = String(req.headers.authorization || '').replace(/^Bearer\s+/i, '');
  if (!secret || !safeEqual(bearer, secret)) return send(res, 401, { error: 'unauthorized' });
  try {
    const raw = await readBody(req);
    if (raw.length > MAX) return send(res, 413, { error: 'too large' });
    const snap = JSON.parse(raw);
    if (snap.v !== 1 || !snap.global || typeof snap.users !== 'object' || !Array.isArray(snap.days)) return send(res, 400, { error: 'bad snapshot' });
    await setSnapshot(snap);
    return send(res, 200, { ok: true, generated_at: snap.generated_at });
  } catch (e) {
    return send(res, e.code === 413 ? 413 : 400, { error: e.code === 413 ? 'too large' : 'bad request' });
  }
};
