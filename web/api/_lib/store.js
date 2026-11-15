'use strict';
// Хранилище одного JSON-снимка. Порядок выбора:
//  1) Upstash Redis (если заданы KV_REST_API_* / UPSTASH_REDIS_REST_*), 2) приватный Vercel Blob (BLOB_READ_WRITE_TOKEN),
//  3) файл во временной папке: локальная проверка без аккаунта.
const fs = require('fs');
const os = require('os');
const path = require('path');

const FILE = path.join(os.tmpdir(), 'spamguard-snapshot.json');
const KEY = 'spamguard:snapshot';

function redisConf(env) {
  const url = env.UPSTASH_REDIS_REST_URL || env.KV_REST_API_URL;
  const token = env.UPSTASH_REDIS_REST_TOKEN || env.KV_REST_API_TOKEN;
  return url && token ? { url, token } : null;
}

async function redis(conf, cmd) {
  const r = await fetch(conf.url, { method: 'POST', headers: { Authorization: `Bearer ${conf.token}` }, body: JSON.stringify(cmd) });
  if (!r.ok) throw new Error(`redis ${r.status}`);
  return (await r.json()).result;
}

const BLOB_PATH = 'spamguard/snapshot.json';

async function blobPut(snap) {
  const { put } = require('@vercel/blob');
  await put(BLOB_PATH, JSON.stringify(snap), { access: 'private', addRandomSuffix: false, allowOverwrite: true, contentType: 'application/json', cacheControlMaxAge: 60 });
}

async function blobGet() {
  const { get } = require('@vercel/blob');
  try {
    const r = await get(BLOB_PATH, { access: 'private' });
    if (!r || r.statusCode !== 200) return null;
    return JSON.parse(await new Response(r.stream).text());
  } catch (_) {
    return null;  // снимка ещё нет
  }
}

async function getSnapshot(env = process.env) {
  const c = redisConf(env);
  if (c) {
    const raw = await redis(c, ['GET', KEY]);
    return raw ? JSON.parse(raw) : null;
  }
  if (env.BLOB_READ_WRITE_TOKEN) return blobGet();
  try { return JSON.parse(fs.readFileSync(FILE, 'utf8')); } catch (_) { return null; }
}

async function setSnapshot(snap, env = process.env) {
  const c = redisConf(env);
  if (c) return redis(c, ['SET', KEY, JSON.stringify(snap)]);
  if (env.BLOB_READ_WRITE_TOKEN) return blobPut(snap);
  fs.writeFileSync(FILE, JSON.stringify(snap));
}

module.exports = { getSnapshot, setSnapshot, FILE };
