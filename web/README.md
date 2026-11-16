# SpamGuard: сайт с аналитикой (Vercel и Telegram Mini App)

Страница с графиками: **«Мой обзор»** для каждого пользователя и **«Админ»** с общей статистикой и итогами.
Без сборки и зависимостей: `public/index.html` (графики на чистом SVG) и две функции Vercel в `api/`.

Каждый разворачивает свой экземпляр в своём аккаунте Vercel. Защита Vercel Auth на проекте должна быть
отключена (иначе Telegram не откроет страницу); доступ закрыт подписью ссылки или подписью Mini App.

## Как это работает

```
бот (ваш сервер) ──POST /api/ingest (раз в 30 мин, Bearer INGEST_SECRET)──► Vercel ──► приватный Vercel Blob (или Upstash Redis)
пользователь: /report в боте ──► подписанная ссылка (24 ч) ──► страница ──GET /api/data──► только его цифры (админу ещё и общие)
```

- На сайт уходят счётчики по дням, топы категорий и опасных доменов и **справочник пользователей для админа** (имя, @username,
  Telegram ID, статус, режимы, цифры; API отдаёт его только роли admin). Тексты сообщений, телефоны и данные собеседников
  на сайт **не передаются**; в графиках пользователи обозначены псевдонимом `HMAC(ключ, id)`.
- Доступ: подпись HMAC (ссылка из `/report`, живёт 24 часа) либо подпись Telegram Mini App (`initData`).
  Без валидной подписи данные не отдаются. Обычный пользователь не получает общую статистику ни при каких условиях
  (это проверено тестами `tests/test_api.js`).
- Ссылка с токеном убирается из адресной строки сразу после загрузки; заголовки: `Referrer-Policy: no-referrer`, `noindex`.

## Развёртывание на Vercel (≈10 минут)

1. **Проект.** Vercel → *Add New → Project* → импортируйте репозиторий `spamguard` → **Root Directory: `web`** →
   Framework Preset: *Other* → Deploy. Получите адрес вида `https://spamguard-xxxx.vercel.app`.
2. **Хранилище.** Подойдёт **приватный Vercel Blob** (используется сейчас): *Storage → Create → Blob → Private*, подключить к проекту
   (Vercel сам добавит `BLOB_READ_WRITE_TOKEN`). Альтернатива: Upstash Redis (`KV_REST_API_*` / `UPSTASH_REDIS_REST_*`).
3. **Секреты** (*Settings → Environment Variables*, Production). Сгенерируйте случайные значения: `openssl rand -hex 32`.

   | Переменная | Зачем |
   |---|---|
   | `INGEST_SECRET` | ключ, которым бот отправляет данные |
   | `SITE_SECRET` | подпись ссылок из `/report` |
   | `PSEUDO_KEY` | ключ псевдонимов (нужен для Mini App) |
   | `BOT_TOKEN` | проверка подписи Mini App (токен вашего бота) |
   | `ADMIN_IDS` | Telegram ID админа (для роли в Mini App) |

   Затем *Deployments → Redeploy*, чтобы переменные подхватились.
4. **Бот.** На сервере в `~/spamguard/.env` добавьте (те же значения):
   ```
   SITE_URL=https://spamguard-xxxx.vercel.app
   INGEST_SECRET=<тот же>
   SITE_SECRET=<тот же>
   ANALYTICS_PSEUDO_KEY=<тот же, что PSEUDO_KEY на Vercel>
   ```
   и перезапустите: `cd ~/spamguard && docker compose up -d`.
5. **Проверка.** В Telegram отправьте боту `/report` → кнопка «📊 Открыть отчёт». Данные появятся в течение 30 минут
   (команда `/report` отправляет свежий снимок сама).
6. **Отзыв всех выданных ссылок:** смените `SITE_SECRET` на Vercel и в `.env` бота.

## Telegram Mini App (позже)

1. @BotFather → ваш бот → *Bot Settings → Menu Button* (или `/newapp`) → URL сайта `https://spamguard-xxxx.vercel.app`.
2. В `.env` бота: `REPORT_AS_WEBAPP=1`, и `/report` будет открывать отчёт внутри Telegram.
   Страница сама подхватывает подпись Mini App, тему Telegram и роль (по `ADMIN_IDS`).

## Локальная проверка без Vercel

```bash
cd web
node dev-server.js          # http://localhost:3000/?demo=1  (демо-данные для предпросмотра)
node tests/test_api.js      # тесты API
```

Сквозная проверка с настоящим снимком: `DUMP_SNAPSHOT=snap.json python tests_analytics.py`, затем отправьте `snap.json`
на `/api/ingest` с заголовком `Authorization: Bearer dev-ingest-secret`.

## Срок хранения и приватность

Бот хранит сырые события (метаданные: риск, класс, действие, домены; **без текстов**) `RETENTION_DAYS=180` дней
и сам удаляет старые. На сайте лежит только последний снимок за 90 дней. Команда `/stop` стирает данные пользователя в боте;
на сайте его цифры исчезнут со следующим снимком.
