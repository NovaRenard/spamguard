// Локальный сервер для проверки сайта без аккаунта Vercel:  node dev-server.js  (порт 3000, PORT=... меняет)
// Раздаёт public/ и подключает те же обработчики /api/ingest и /api/data. Снимок хранится в файле (см. api/_lib/store.js).
'use strict';
const http = require('http');
const fs = require('fs');
const path = require('path');

process.env.SITE_SECRET = process.env.SITE_SECRET || 'dev-site-secret';
process.env.INGEST_SECRET = process.env.INGEST_SECRET || 'dev-ingest-secret';
const routes = { '/api/ingest': require('./api/ingest'), '/api/data': require('./api/data') };
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };

http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  res.setHeader('X-Robots-Tag', 'noindex'); res.setHeader('Referrer-Policy', 'no-referrer');
  if (routes[url.pathname]) return routes[url.pathname](req, res);
  const file = path.join(__dirname, 'public', url.pathname === '/' ? 'index.html' : path.normalize(url.pathname).replace(/^(\.\.[/\\])+/, ''));
  if (!file.startsWith(path.join(__dirname, 'public'))) { res.statusCode = 403; return res.end(); }
  fs.readFile(file, (err, buf) => {
    if (err) { res.statusCode = 404; return res.end('not found'); }
    res.setHeader('Content-Type', TYPES[path.extname(file)] || 'application/octet-stream'); res.end(buf);
  });
}).listen(process.env.PORT || 3000, '127.0.0.1', () => console.log(`SpamGuard web: http://localhost:${process.env.PORT || 3000}/?demo=1`));
