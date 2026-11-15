'use strict';
// Проверка доступа. Два способа:
//  1) подписанный токен из бота (/report): base64url(payload).base64url(HMAC-SHA256(SITE_SECRET, base64url(payload)))
//  2) Telegram Mini App: initData, подписанная токеном бота (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)
const crypto = require('crypto');

const b64u = (buf) => Buffer.from(buf).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const fromB64u = (s) => Buffer.from(s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4), 'base64');

function safeEqual(a, b) {
  const x = Buffer.from(String(a)), y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

function verifyToken(secret, token, now = Date.now() / 1000) {
  try {
    if (!secret || typeof token !== 'string') return null;
    const [body, sig] = token.split('.');
    if (!body || !sig) return null;
    const good = b64u(crypto.createHmac('sha256', secret).update(body).digest());
    if (!safeEqual(sig, good)) return null;
    const data = JSON.parse(fromB64u(body).toString('utf8'));
    return data.e >= now ? data : null;
  } catch (_) {
    return null;
  }
}

// псевдоним = HMAC(PSEUDO_KEY, id), как в analytics.pseudo() на стороне бота
function pseudo(key, id) {
  return crypto.createHmac('sha256', key).update(String(id)).digest('hex').slice(0, 16);
}

function verifyInitData(botToken, initData, now = Date.now() / 1000, maxAge = 86400) {
  try {
    if (!botToken || !initData) return null;
    const params = new URLSearchParams(initData);
    const hash = params.get('hash');
    if (!hash) return null;
    params.delete('hash');
    const dcs = [...params.entries()].map(([k, v]) => `${k}=${v}`).sort().join('\n');
    const secretKey = crypto.createHmac('sha256', 'WebAppData').update(botToken).digest();
    const calc = crypto.createHmac('sha256', secretKey).update(dcs).digest('hex');
    if (!safeEqual(calc, hash)) return null;
    if (now - Number(params.get('auth_date') || 0) > maxAge) return null;
    const user = JSON.parse(params.get('user') || 'null');
    return user && user.id ? user : null;
  } catch (_) {
    return null;
  }
}

/** Возвращает {p, r} (псевдоним и роль) или null. */
function authenticate(req, env = process.env) {
  const url = new URL(req.url, 'http://x');
  const token = req.headers['x-token'] || url.searchParams.get('t');
  if (token) {
    const d = verifyToken(env.SITE_SECRET, token);
    return d ? { p: d.p, r: d.r === 'admin' ? 'admin' : 'user' } : null;
  }
  const init = req.headers['x-tg-init-data'];
  if (init && env.BOT_TOKEN && env.PSEUDO_KEY) {
    const u = verifyInitData(env.BOT_TOKEN, init);
    if (!u) return null;
    const admins = String(env.ADMIN_IDS || '').split(',').map((x) => x.trim()).filter(Boolean);
    return { p: pseudo(env.PSEUDO_KEY, u.id), r: admins.includes(String(u.id)) ? 'admin' : 'user' };
  }
  return null;
}

module.exports = { verifyToken, verifyInitData, pseudo, authenticate, safeEqual, b64u };
