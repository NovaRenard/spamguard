"""Сервис: бот присылает QR -> вы сканируете его в Telegram -> бот подключается к вашему аккаунту (Telethon)
и анализирует сообщения новых собеседников.

Поток:  /start -> QR-код -> «✅ Подключено, анализ запущен» -> уведомления приходят от этого же бота.
Команды: /status, /live (включить блокировку), /dry (только уведомлять), /stop (отключить и стереть сессию).

Запуск: заполните .env (см. .env.example), затем  python service.py
"""
import asyncio
import hashlib
import io
import logging
import os
import sqlite3
import time
from collections import OrderedDict, deque
from pathlib import Path

import qrcode
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, WebAppInfo, Message)
from cryptography.fernet import Fernet
from telethon import TelegramClient, events, functions
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import Channel, Chat
from telethon.sessions import StringSession

from classifier import SpamGuard
from feedback import Feedback
from groupcheck import analyze_chat
import analytics
from links import LinkChecker
from whatsapp import Evolution, WhatsAppService
from senderprofile import profile_risk

log = logging.getLogger("service")


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env()
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
fernet = Fernet(os.environ["SESSION_KEY"].encode())
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x}
# Администраторы: им приходят заявки на доступ с кнопками «Одобрить / Отклонить»
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
MAX_ACCESS_REQUESTS_PER_HOUR = int(os.environ.get("MAX_ACCESS_REQUESTS_PER_HOUR", 5))
# Открытый доступ: пользоваться может каждый, одобрение не нужно (OPEN_ACCESS=0 вернёт заявки). Закрыть доступ конкретному человеку: /revoke
OPEN_ACCESS = os.environ.get("OPEN_ACCESS", "1") == "1"
# Дообучение: отметка «Это не спам» сразу действует для самого пользователя, а в общую модель попадает после подтверждения
# администратором или этим числом разных пользователей (защита от «отравления» модели)
FEEDBACK_MIN_USERS = int(os.environ.get("FEEDBACK_MIN_USERS", 2))
WARN_AT = float(os.environ.get("WARN_AT", 0.5))
BLOCK_AT = float(os.environ.get("BLOCK_AT", 0.85))
MAX_BLOCKS_PER_HOUR = int(os.environ.get("MAX_BLOCKS_PER_HOUR", 5))
# группы и каналы
GROUP_CHECK = os.environ.get("GROUP_CHECK", "1") == "1"
GROUP_WARN_AT = float(os.environ.get("GROUP_WARN_AT", 0.5))
GROUP_LEAVE_AT = float(os.environ.get("GROUP_LEAVE_AT", 0.85))
GROUP_LEAVE_LABELS = {x.strip() for x in os.environ.get("GROUP_LEAVE_LABELS", "gambling").split(",") if x.strip()}
MAX_LEAVES_PER_HOUR = int(os.environ.get("MAX_LEAVES_PER_HOUR", 10))
GROUP_RECHECK_DAYS = float(os.environ.get("GROUP_RECHECK_DAYS", 7))

TEST_MARK = "🛡 [тест]"

GROUP_REASONS = {
    "gambling": "азартные игры / ставки / казино",
    "scam": "мошенничество",
    "spam": "спам / реклама",
}

REASONS = {
    "gambling": "продвигает азартные игры / казино / ставки",
    "scam": "признаки мошенничества (быстрый заработок, фишинг, «вы выиграли»)",
    "spam": "рассылает спам / рекламу",
}

guard = SpamGuard()
# Проверка ссылок: локальные списки казино/фишинга + эвристики; на сайты по ссылкам бот не ходит
LINKS_ENABLED = os.environ.get("LINKS_ENABLED", "1") == "1"
LINK_WARN_KNOWN = float(os.environ.get("LINK_WARN_KNOWN", 0.8))  # порог предупреждения для знакомых
linkchk = LinkChecker(os.environ.get("LINKS_DIR", "lists"), rdap=os.environ.get("LINKS_RDAP", "1") == "1") if LINKS_ENABLED else None

# Аналитика: сайт на Vercel (потом Mini App). Наружу уходят только агрегаты, без текстов и реальных ID
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/")
INGEST_SECRET = os.environ.get("INGEST_SECRET", "")
SITE_SECRET = os.environ.get("SITE_SECRET", "")
PSEUDO_KEY = os.environ.get("ANALYTICS_PSEUDO_KEY") or hashlib.sha256((os.environ["SESSION_KEY"] + "|pseudo").encode()).hexdigest()
ANALYTICS_PUSH_MIN = float(os.environ.get("ANALYTICS_PUSH_MIN", 30))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", 180))  # срок хранения сырых событий (метаданные, без текстов)
TZ_HOURS = float(os.environ.get("ANALYTICS_TZ_HOURS", 5))
REPORT_AS_WEBAPP = os.environ.get("REPORT_AS_WEBAPP", "0") == "1"  # 1 = открывать отчёт как Telegram Mini App
ANALYTICS_ON = bool(SITE_URL and INGEST_SECRET and SITE_SECRET)
db = sqlite3.connect("state.db", check_same_thread=False)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(owner_id INT PRIMARY KEY, session BLOB, dry_run INT DEFAULT 1);
    CREATE TABLE IF NOT EXISTS trusted(owner_id INT, chat_id INT, PRIMARY KEY(owner_id, chat_id));
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INT, ts REAL, chat_id INT, sender_id INT,
        label TEXT, text_risk REAL, profile_risk REAL, risk REAL, action TEXT, reasons TEXT);
    CREATE TABLE IF NOT EXISTS access(user_id INT PRIMARY KEY, status TEXT, name TEXT, username TEXT, ts REAL);
    CREATE TABLE IF NOT EXISTS chat_checks(owner_id INT, chat_id INT, ts REAL, verdict TEXT, risk REAL, PRIMARY KEY(owner_id, chat_id));
    CREATE INDEX IF NOT EXISTS ev_owner_ts ON events(owner_id, ts);
    """
)
try:
    db.execute("ALTER TABLE users ADD COLUMN group_leave INT DEFAULT 0")
    db.commit()
except sqlite3.OperationalError:
    pass  # колонка уже есть
for _col in ("domains TEXT",):  # аналитика: домены опасных ссылок (только домены, без текста)
    try:
        db.execute(f"ALTER TABLE events ADD COLUMN {_col}")
        db.commit()
    except sqlite3.OperationalError:
        pass
try:
    db.execute("ALTER TABLE access ADD COLUMN last_seen REAL")
    db.commit()
except sqlite3.OperationalError:
    pass
feedback = Feedback(db, guard, ADMIN_IDS, min_users=FEEDBACK_MIN_USERS)
feedback.load()  # «память» об отметках «Это не спам» переживает перезапуск


def learn_texts(owner_id: int, texts: list[str], source: str) -> int:
    """Сохраняет отметки «Это не спам» и запускает дообучение в фоне. Возвращает число новых примеров."""
    n = feedback.add(owner_id, texts, source)
    if n:
        asyncio.get_running_loop().create_task(feedback.retrain_if_needed())
    return n
bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# WhatsApp (Evolution API) включается, только если задан EVOLUTION_API_KEY
wa: WhatsAppService | None = None
if os.environ.get("EVOLUTION_API_KEY"):
    wa = WhatsAppService(
        bot=bot, db=db, guard=guard,
        evo=Evolution(os.environ.get("EVOLUTION_URL", "http://evolution:8080"), os.environ["EVOLUTION_API_KEY"]),
        webhook_base=os.environ.get("WA_WEBHOOK_BASE", "http://spamguard:8081"),
        secret=os.environ["WA_WEBHOOK_SECRET"],
        warn_at=WARN_AT, block_at=BLOCK_AT, group_warn_at=GROUP_WARN_AT, group_leave_at=GROUP_LEAVE_AT,
        leave_labels=GROUP_LEAVE_LABELS,
        pair_ttl=int(os.environ.get("WA_PAIR_TTL_SEC", 300)),
        closed_ttl=int(os.environ.get("WA_CLOSED_TTL_SEC", 3600)),
        max_instances=int(os.environ.get("MAX_WA_INSTANCES", 10)),
        max_blocks_per_hour=MAX_BLOCKS_PER_HOUR, max_leaves_per_hour=MAX_LEAVES_PER_HOUR,
        recheck_days=GROUP_RECHECK_DAYS,
        links=linkchk, link_warn_known=LINK_WARN_KNOWN,
        learn=learn_texts,
    )
monitors: dict[int, "Monitor"] = {}
connecting: set[int] = set()
pending_pw: dict[int, asyncio.Future] = {}


def tg_urls(e) -> list[str]:
    """Адреса ссылок из разметки Telegram: «кликни сюда» может прятать совсем другой адрес."""
    out: list[str] = []
    try:
        for ent, _txt in e.message.get_entities_text():
            url = getattr(ent, "url", None)  # MessageEntityTextUrl
            if url:
                out.append(url)
    except Exception:
        pass
    return out


def defang(s: str) -> str:
    """Цитата спама в уведомлении не должна становиться кликабельной ссылкой."""
    return s.replace("http", "hxxp").replace("t.me/", "t[.]me/").replace("@", "＠")


class Monitor:
    def __init__(self, owner_id: int, client: TelegramClient, me_id: int, dry_run: bool):
        self.owner_id, self.client, self.me_id, self.dry_run = owner_id, client, me_id, dry_run
        self.block_times: deque[float] = deque()
        self.recent: dict[int, deque[float]] = {}
        self.known_link_alert: dict[int, float] = {}
        self._checks: dict[int, float] | None = None
        self.group_busy: set[int] = set()
        self.leave_times: deque[float] = deque()
        row = db.execute("SELECT group_leave FROM users WHERE owner_id=?", (owner_id,)).fetchone()
        self.group_leave = bool(row and row[0])  # автовыход из групп: отдельное согласие, /groupleave
        self.scanning = False
        self.group_sem = asyncio.Semaphore(1)
        self.test_mode = False  # /testmode; после перезапуска всегда выключен
        self.flagged: OrderedDict[int, list[str]] = OrderedDict()  # чат -> тексты, из-за которых подняли тревогу (только в памяти)
        self.stats = {"checked": 0, "warned": 0, "blocked": 0}
        client.add_event_handler(self.on_outgoing, events.NewMessage(outgoing=True))
        client.add_event_handler(self.on_incoming, events.NewMessage(incoming=True))
        client.add_event_handler(self.on_chat_action, events.ChatAction)

    # --- доверенные чаты
    def is_trusted(self, chat_id: int) -> bool:
        return db.execute("SELECT 1 FROM trusted WHERE owner_id=? AND chat_id=?", (self.owner_id, chat_id)).fetchone() is not None

    def trust(self, chat_id: int) -> None:
        db.execute("INSERT OR IGNORE INTO trusted VALUES (?,?)", (self.owner_id, chat_id))
        db.commit()

    async def on_outgoing(self, e):
        if (e.raw_text or "").startswith(TEST_MARK):
            return  # тестовый ответ бота не делает собеседника доверенным
        if e.chat_id != self.me_id:
            self.trust(e.chat_id)  # вы написали первым/ответили — собеседник доверенный

    async def is_new_sender(self, e, sender) -> bool:
        if self.is_trusted(e.chat_id):
            return False
        if e.is_private and (
            getattr(sender, "bot", False) or getattr(sender, "verified", False) or getattr(sender, "support", False)
            or getattr(sender, "contact", False) or getattr(sender, "mutual_contact", False) or e.chat_id == 777000
        ):
            self.trust(e.chat_id)
            return False
        if await self.client.get_messages(e.chat_id, limit=1, from_user="me"):
            self.trust(e.chat_id)
            return False
        return True

    def rate_limited(self) -> bool:
        now = time.time()
        while self.block_times and now - self.block_times[0] > 3600:
            self.block_times.popleft()
        return len(self.block_times) >= MAX_BLOCKS_PER_HOUR

    def remember(self, chat_id: int, texts: list[str]) -> None:
        """Запоминаем в памяти (не в базе), из-за чего тревога: если владелец нажмёт «Это не спам», на этом дообучимся."""
        self.flagged[chat_id] = [t for t in texts if t][:8]
        self.flagged.move_to_end(chat_id)
        while len(self.flagged) > 200:
            self.flagged.popitem(last=False)

    async def notify(self, text: str, chat_id: int | None = None, markup=None, hidden: bool = False) -> None:
        kb = markup
        if markup is None and chat_id is not None:
            row = [InlineKeyboardButton(text="✅ Это не спам, доверять", callback_data=f"trust:{chat_id}")]
            if hidden:
                row.append(InlineKeyboardButton(text="👁 Показать текст", callback_data=f"show:{chat_id}"))
            kb = InlineKeyboardMarkup(inline_keyboard=[row])
        await bot.send_message(self.owner_id, text, reply_markup=kb)

    # ---------------- группы и каналы
    def _load_checks(self) -> dict[int, float]:
        if self._checks is None:
            self._checks = dict(db.execute("SELECT chat_id, ts FROM chat_checks WHERE owner_id=?", (self.owner_id,)).fetchall())
        return self._checks

    def mark_checked(self, chat_id: int, verdict: str, risk: float) -> None:
        now = time.time()
        self._load_checks()[chat_id] = now
        db.execute("INSERT OR REPLACE INTO chat_checks VALUES (?,?,?,?,?)", (self.owner_id, chat_id, now, verdict, risk))
        db.commit()

    def record_chat(self, chat_id: int, label: str, risk: float, action: str, reasons: list[str]) -> None:
        db.execute(
            "INSERT INTO events(owner_id, ts, chat_id, sender_id, label, text_risk, profile_risk, risk, action, reasons) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (self.owner_id, time.time(), chat_id, 0, label, risk, 0.0, risk, action, "; ".join(reasons)),
        )
        db.commit()

    def leave_limited(self) -> bool:
        now = time.time()
        while self.leave_times and now - self.leave_times[0] > 3600:
            self.leave_times.popleft()
        return len(self.leave_times) >= MAX_LEAVES_PER_HOUR

    def schedule_group_check(self, chat_id: int, source: str) -> None:
        ts = self._load_checks().get(chat_id)
        if ts is not None and time.time() - ts < GROUP_RECHECK_DAYS * 86400:
            return  # недавно проверяли
        if chat_id in self.group_busy:
            return
        self.group_busy.add(chat_id)
        asyncio.create_task(self._check_task(chat_id, source))

    async def _check_task(self, chat_id: int, source: str) -> None:
        try:
            async with self.group_sem:
                await self.check_chat(chat_id, source)
                await asyncio.sleep(1.5)  # не частим запросами к Telegram
        except Exception:
            log.exception("group check failed for %s", chat_id)
        finally:
            self.group_busy.discard(chat_id)

    async def on_chat_action(self, e) -> None:
        # вас добавили в группу или вы вошли по ссылке
        if GROUP_CHECK and (e.user_added or e.user_joined) and self.me_id in (e.user_ids or []):
            self.schedule_group_check(e.chat_id, "joined")

    async def check_chat(self, chat_id: int, source: str, ent=None) -> dict | None:
        """Оценивает группу/канал и при необходимости выходит. Возвращает {'action': ..., 'risk': ...} или None, если пропущено."""
        ent = ent or await self.client.get_entity(chat_id)
        if not isinstance(ent, (Channel, Chat)):
            return None
        # свои чаты (вы создатель/админ или уже писали) никогда не трогаем
        if getattr(ent, "creator", False) or getattr(ent, "admin_rights", None) or self.is_trusted(chat_id):
            self.mark_checked(chat_id, "own", 0.0)
            return None
        try:
            if await self.client.get_messages(ent, limit=1, from_user="me"):
                self.trust(chat_id)
                self.mark_checked(chat_id, "own", 0.0)
                return None
        except Exception:
            pass

        res = await analyze_chat(self.client, ent, guard, owner=self.owner_id)
        risk, label = res["risk"], res["label"]
        self.mark_checked(chat_id, label, risk)
        self.remember(chat_id, res.get("samples", []))
        if risk < GROUP_WARN_AT:
            self.record_chat(chat_id, label, risk, "group_ok", res["reasons"])
            return {"action": "group_ok", "risk": risk}

        kind = res["kind"]
        kind_g = "канала" if kind == "канал" else "группы"
        pron, adj = ("он", "похож") if kind == "канал" else ("она", "похожа")
        name = defang(f"«{res['title']}»" + (f" @{res['username']}" if res["username"] else ""))
        why = "; ".join(res["reasons"]) or "признаки в сообщениях"
        reason = GROUP_REASONS.get(label, label)
        can_leave = label in GROUP_LEAVE_LABELS and risk >= GROUP_LEAVE_AT

        def kb(*rows):
            return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])

        trust_btn = InlineKeyboardButton(text="✅ Это не то, доверять", callback_data=f"gtrust:{chat_id}")
        leave_btn = InlineKeyboardButton(text="🚪 Выйти", callback_data=f"gleave:{chat_id}")
        if not can_leave:
            action = "group_warned"
            text = (f"⚠️ {kind.capitalize()} {name} {adj} на тему «{reason}» (риск {risk:.0%}).\nПочему: {why}.\n"
                    f"Автоматически не выхожу.")
            markup = kb([leave_btn, trust_btn])
        elif self.dry_run or not self.group_leave or self.leave_limited():
            action = "group_would_leave"
            why_mode = (
                "режим «только уведомления»: /live" if self.dry_run
                else "автовыход выключен: /groupleave" if not self.group_leave
                else "лимит выходов в час"
            )
            text = f"🚪 Я бы вышел из {kind_g} {name} ({why_mode}): {reason}, риск {risk:.0%}.\nПочему: {why}."
            markup = kb([leave_btn, trust_btn])
        else:
            try:
                await self.client.delete_dialog(ent)
            except Exception as ex:
                self.record_chat(chat_id, label, risk, "group_leave_failed", res["reasons"])
                await self.notify(f"❌ Не удалось выйти из {kind_g} {name}: {ex}")
                return {"action": "group_leave_failed", "risk": risk}
            self.leave_times.append(time.time())
            action = "group_left"
            text = (f"🚪 Бот вышел из {kind_g} {name}: {pron} про «{reason}» (риск {risk:.0%}).\nПочему: {why}.\n"
                    f"Ошибся? Нажми кнопку, чтобы этот чат больше не проверялся, и вернись в него сам.")
            markup = kb([trust_btn])
        self.record_chat(chat_id, label, risk, action, res["reasons"])
        await self.notify(text, markup=markup)
        return {"action": action, "risk": risk}

    async def scan_groups(self) -> None:
        """Проверяет все ваши группы и каналы (команда /scan). Идёт медленно, чтобы не словить лимиты Telegram."""
        counts = {"checked": 0, "group_left": 0, "group_would_leave": 0, "group_warned": 0, "failed": 0}
        try:
            async for d in self.client.iter_dialogs(limit=400):
                if not (d.is_group or d.is_channel):
                    continue
                ts = self._load_checks().get(d.id)
                if ts is not None and time.time() - ts < GROUP_RECHECK_DAYS * 86400:
                    continue
                try:
                    r = await self.check_chat(d.id, "scan", ent=d.entity)
                except Exception:
                    counts["failed"] += 1
                    continue
                if r:
                    counts["checked"] += 1
                    if r["action"] in counts:
                        counts[r["action"]] += 1
                await asyncio.sleep(2)
        finally:
            self.scanning = False
        await self.notify(
            f"🔎 Проверка групп и каналов завершена: проверено {counts['checked']}, вышел {counts['group_left']}, "
            f"«вышел бы» {counts['group_would_leave']}, предупреждений {counts['group_warned']}, ошибок {counts['failed']}."
        )

    def record(self, e, label, text_risk, prof_risk, risk, action, reasons, domains=None):
        """Пишем в статистику только метаданные — сам текст сообщения НЕ сохраняется."""
        db.execute(
            "INSERT INTO events(owner_id, ts, chat_id, sender_id, label, text_risk, profile_risk, risk, action, reasons, domains) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (self.owner_id, time.time(), e.chat_id, e.sender_id, label, text_risk, prof_risk, risk, action, "; ".join(reasons), ",".join(domains or [])),
        )
        db.commit()

    async def check_known_links(self, e, sender, urls: list[str]) -> None:
        """Знакомым ссылки тоже проверяем (вдруг взломали аккаунт). Предупреждаем только при явной опасности
        (порог LINK_WARN_KNOWN) и никогда не блокируем: это ваш друг, а не спамер."""
        if not linkchk or not e.raw_text:
            return
        lk = await linkchk.check_text(e.raw_text, urls)
        if lk.risk < LINK_WARN_KNOWN:
            return
        now = time.time()
        if now - self.known_link_alert.get(e.chat_id, 0) < 600:
            return  # не чаще раза в 10 минут на чат
        self.known_link_alert[e.chat_id] = now
        name = getattr(sender, "first_name", None) or "?"
        top = max(lk.results, key=lambda r: r.risk)
        self.record(e, lk.label, lk.risk, 0.0, lk.risk, "known_link", lk.reasons, domains=[r.host for r in lk.results if r.risk >= 0.3])
        await self.notify(
            f"⚠️ Знакомый {name} прислал опасную ссылку: {defang(top.host)} (риск {lk.risk:.0%}).\n"
            f"Почему: {'; '.join(lk.reasons)}.\n"
            "Возможно, его аккаунт взломали. Не открывайте ссылку и предупредите его другим способом."
        )

    async def on_incoming(self, e):
        if e.out:
            return
        if GROUP_CHECK and (e.is_group or e.is_channel):
            self.schedule_group_check(e.chat_id, "message")
            return
        if not e.is_private or not e.raw_text or e.raw_text.startswith(TEST_MARK):
            return
        # Тестовый режим: сообщения от ID из ALLOWED_IDS считаем «новым контактом», но НИКОГО не блокируем.
        test = self.test_mode and e.sender_id in allowed_ids() and e.sender_id != self.me_id
        sender = await e.get_sender()
        new = test or await self.is_new_sender(e, sender)
        urls = tg_urls(e)  # включая ссылки, спрятанные под текстом («кликни сюда»)
        if not new:
            await self.check_known_links(e, sender, urls)  # знакомым: только явная опасность
            return
        self.stats["checked"] += 1

        now = time.time()
        q = self.recent.setdefault(e.chat_id, deque(maxlen=10))
        q.append(now)
        burst = sum(1 for t in q if now - t <= 60)

        res = guard.predict(e.raw_text, owner=self.owner_id)
        lk = await linkchk.check_text(e.raw_text, urls) if linkchk else None
        doms = [r.host for r in lk.results if r.risk >= 0.3] if lk else []
        if lk and lk.risk > 0:  # ссылки — независимое свидетельство, усиливают оценку текста
            res = {**res, "risk": 1 - (1 - res["risk"]) * (1 - lk.risk), "label": res["label"] if res["label"] != "ok" else lk.label}
        p_risk, p_reasons, p_label = await profile_risk(self.client, e, sender, guard, burst, e.raw_text)
        risk = 1 - (1 - res["risk"]) * (1 - p_risk)  # текст и профиль — независимые свидетельства
        label = res["label"] if res["label"] != "ok" else (p_label or "spam")
        reason = REASONS.get(label, label)
        prof_line = f"\nПризнаки профиля: {', '.join(p_reasons)}." if p_reasons else ""
        if lk and lk.reasons:
            prof_line += f"\nСсылки: {'; '.join(lk.reasons)}."

        if test:
            if risk >= BLOCK_AT:
                txt = f"я бы вас заблокировал, если бы вы написали мне как новый контакт: {reason} (риск {risk:.0%})."
            elif risk >= WARN_AT:
                txt = f"я бы выдал предупреждение, но не блокировал: {reason} (риск {risk:.0%})."
            else:
                txt = f"пропустил бы, подозрений нет (риск {risk:.0%})."
            if p_reasons:
                txt += f" Профиль: {', '.join(p_reasons)}."
            if lk and lk.reasons:
                txt += f" Ссылки: {'; '.join(lk.reasons)}."
            await e.respond(f"{TEST_MARK} {txt}")
            self.record(e, label, res["risk"], p_risk, risk, "test", p_reasons, domains=doms)
            return
        if risk < WARN_AT:
            self.record(e, "ok", res["risk"], p_risk, risk, "ok", p_reasons, domains=doms)
            return

        name = getattr(sender, "first_name", None) or "?"
        uname = f" @{sender.username}" if getattr(sender, "username", None) else ""
        head = f"{name}{defang(uname)} (id {e.sender_id})"
        # Цель сервиса: меньше азартной рекламы перед глазами. Текст такой рекламы в уведомлении скрыт (можно показать кнопкой).
        hide = label == "gambling"
        quote = "🙈 Текст азартной рекламы скрыт, чтобы не провоцировать." if hide else f"«{defang(e.raw_text[:200])}»"
        self.remember(e.chat_id, [e.raw_text])

        if risk < BLOCK_AT:
            self.stats["warned"] += 1
            self.record(e, label, res["risk"], p_risk, risk, "warned", p_reasons, domains=doms)
            await self.notify(f"⚠️ Подозрительное сообщение от {head}: {reason}, риск {risk:.0%}.{prof_line}\n{quote}\nНе заблокирован.", e.chat_id, hidden=hide)
            return
        if self.dry_run or self.rate_limited():
            why = "режим «только уведомления», включить блокировку: /live" if self.dry_run else "лимит блокировок в час"
            self.stats["warned"] += 1
            self.record(e, label, res["risk"], p_risk, risk, "would_block", p_reasons, domains=doms)
            await self.notify(f"🛡 Был бы заблокирован ({why}): {head} — {reason}, риск {risk:.0%}.{prof_line}\n{quote}", e.chat_id, hidden=hide)
            return

        self.block_times.append(time.time())
        done = []
        try:
            try:
                await self.client(functions.messages.ReportSpamRequest(peer=await e.get_input_chat()))
                done.append("жалоба на спам отправлена")
            except Exception as ex:
                log.warning("report failed: %s", ex)
            await self.client(functions.contacts.BlockRequest(id=await e.get_input_sender()))
            done.append("контакт заблокирован")
            try:
                await self.client.delete_dialog(e.chat_id)
                done.append("чат удалён")
            except Exception as ex:
                log.warning("delete_dialog failed: %s", ex)
        except Exception as ex:
            self.record(e, label, res["risk"], p_risk, risk, "block_failed", p_reasons, domains=doms)
            await self.notify(f"❌ Не удалось заблокировать {head}: {ex}\n{quote}", e.chat_id, hidden=hide)
            return
        self.stats["blocked"] += 1
        self.record(e, label, res["risk"], p_risk, risk, "blocked", p_reasons, domains=doms)
        await self.notify(
            f"🛡 Защита: {head} заблокирован — {reason} (риск {risk:.0%}).{prof_line}\nСделано: {', '.join(done)}.\n{quote}",
            e.chat_id,
            hidden=hide,
        )


def allowed_ids() -> set[int]:
    """Постоянные из .env + одобренные администратором через кнопку."""
    approved = {r[0] for r in db.execute("SELECT user_id FROM access WHERE status='approved'").fetchall()}
    return ALLOWED | ADMIN_IDS | approved


def allowed(uid: int) -> bool:
    """Открытый доступ: можно всем, кроме тех, кому администратор закрыл доступ (/revoke). OPEN_ACCESS=0: только одобренные."""
    if uid in ALLOWED or uid in ADMIN_IDS:
        return True
    row = db.execute("SELECT status FROM access WHERE user_id=?", (uid,)).fetchone()
    status = row[0] if row else None
    if status in ("revoked", "rejected"):
        return False
    return OPEN_ACCESS or status == "approved"


def touch_user(u) -> None:
    """Запоминаем, кто пользуется ботом (имя, @username, id), чтобы администратор видел всех в панели. Тексты не сохраняются."""
    now = time.time()
    status = "approved" if (OPEN_ACCESS or u.id in ALLOWED or u.id in ADMIN_IDS) else None
    row = db.execute("SELECT status FROM access WHERE user_id=?", (u.id,)).fetchone()
    if row is None:
        if status is None:
            return
        db.execute("INSERT INTO access(user_id, status, name, username, ts, last_seen) VALUES (?,?,?,?,?,?)",
                   (u.id, status, u.full_name or "", u.username or "", now, now))
    else:
        db.execute("UPDATE access SET name=?, username=?, last_seen=?, status=CASE WHEN status IN ('pending') AND ? IS NOT NULL THEN ? ELSE status END WHERE user_id=?",
                   (u.full_name or "", u.username or "", now, status, status, u.id))
    db.commit()


access_requests: deque[float] = deque()


async def request_access(m: Message) -> None:
    """Незнакомец нажал /start: отправляем заявку администратору."""
    uid = m.from_user.id
    row = db.execute("SELECT status FROM access WHERE user_id=?", (uid,)).fetchone()
    status = row[0] if row else None
    if status == "pending":
        await m.answer("⏳ Заявка уже отправлена владельцу. Я напишу, когда её рассмотрят.")
        return
    if status in ("rejected", "revoked"):
        await m.answer("Доступ закрыт.")
        return
    if not ADMIN_IDS:
        log.warning("start from not-allowed user id=%s, ADMIN_IDS не задан", uid)
        await m.answer("Бот работает по приглашениям. Доступ закрыт.")
        return
    now = time.time()
    while access_requests and now - access_requests[0] > 3600:
        access_requests.popleft()
    if len(access_requests) >= MAX_ACCESS_REQUESTS_PER_HOUR:
        await m.answer("Сейчас слишком много заявок, попробуйте позже.")
        return
    access_requests.append(now)

    name = m.from_user.full_name or "?"
    uname = f" @{m.from_user.username}" if m.from_user.username else ""
    db.execute(
        "INSERT OR REPLACE INTO access(user_id, status, name, username, ts) VALUES (?,?,?,?,?)",
        (uid, "pending", name, m.from_user.username or "", now),
    )
    db.commit()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"acc_ok:{uid}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"acc_no:{uid}"),
        ]]
    )
    sent = 0
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(
                admin,
                f"🔔 Запрос доступа к SpamGuard\n{name}{defang(uname)} (id {uid})\n\n"
                "Одобрение позволит этому человеку подключить свой аккаунт: бот сможет читать его чаты.",
                reply_markup=kb,
            )
            sent += 1
        except Exception as ex:
            log.warning("не удалось отправить заявку админу %s: %s", admin, ex)
    if sent:
        await m.answer("🔒 Бот работает по приглашениям. Заявка отправлена владельцу, я напишу, когда её одобрят.")
    else:
        db.execute("DELETE FROM access WHERE user_id=?", (uid,))
        db.commit()
        await m.answer("Не удалось отправить заявку, попробуйте позже.")


def qr_png(url: str) -> BufferedInputFile:
    buf = io.BytesIO()
    qrcode.make(url).save(buf, format="PNG")
    return BufferedInputFile(buf.getvalue(), filename="login.png")


QR_CAPTION = (
    "Отсканируйте этот QR в Telegram на ДРУГОМ устройстве, где вы уже вошли:\n"
    "Настройки → Устройства → Подключить устройство.\n"
    "Код обновляется каждые ~30 секунд. Бот получит доступ к чтению ваших чатов; "
    "отключить можно в любой момент: /stop или Настройки → Устройства.\n\n"
    "Что видит администратор сервиса: ваше имя, @username, id, подключены ли Telegram/WhatsApp, режимы и цифры "
    "(сколько проверено и помечено). Тексты ваших сообщений и переписку он не видит."
)


async def register(uid: int, client: TelegramClient, dry_run: bool = True) -> Monitor:
    me = await client.get_me()
    enc = fernet.encrypt(client.session.save().encode())
    db.execute("INSERT OR REPLACE INTO users(owner_id, session, dry_run) VALUES (?,?,?)", (uid, enc, int(dry_run)))
    db.commit()
    mon = Monitor(uid, client, me.id, dry_run)
    monitors[uid] = mon
    return mon


@dp.message(Command("start"))
async def cmd_start(m: Message):
    uid = m.from_user.id
    if not allowed(uid):
        await request_access(m)
        return
    touch_user(m.from_user)
    if uid in monitors:
        await m.answer("Уже подключено ✅ Статус: /status")
        return
    if uid in connecting:
        return
    connecting.add(uid)
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    try:
        await client.connect()
        qr = await client.qr_login()
        msg = await m.answer_photo(qr_png(qr.url), caption=QR_CAPTION)
        for _ in range(8):  # ~4 минуты
            try:
                await qr.wait(30)
                break
            except (asyncio.TimeoutError, TimeoutError):
                await qr.recreate()
                await msg.edit_media(InputMediaPhoto(media=qr_png(qr.url), caption=QR_CAPTION))
            except SessionPasswordNeededError:
                await m.answer(
                    "На аккаунте включён облачный пароль (2FA). Пришлите его одним сообщением — "
                    "я сразу удалю сообщение и нигде не сохраню. (Либо отмените: /stop)"
                )
                for attempt in range(3):
                    fut = pending_pw[uid] = asyncio.get_running_loop().create_future()
                    pw = await asyncio.wait_for(fut, 180)
                    try:
                        await client.sign_in(password=pw)
                        break
                    except Exception as ex:
                        if attempt == 2:
                            raise
                        await m.answer(f"Не подошёл ({type(ex).__name__}). Попробуйте ещё раз.")
                pending_pw.pop(uid, None)
                break
        else:
            await m.answer("⌛ Время вышло. Отправьте /start, чтобы получить новый QR.")
            await client.disconnect()
            return
        mon = await register(uid, client, dry_run=True)
        me = await client.get_me()
        await m.answer(
            f"✅ Успешное подключение: {me.first_name}.\n"
            "🔍 Анализ запущен — проверяю сообщения от новых собеседников (не из контактов, которым вы не писали).\n\n"
            "Сейчас режим «только уведомления»: бот никого не блокирует. Через пару дней, убедившись, что ложных "
            "срабатываний нет, включите блокировку командой /live.\n\n"
            "Цель сервиса: чтобы вы видели как можно меньше азартной рекламы. Текст такой рекламы я в уведомлениях скрываю. "
            "Если вы уже сталкивались с азартными играми и хотите максимальной защиты, включите /strict.\n"
            "Если я ошибся, нажмите «✅ Это не спам»: я запомню и стану точнее."
        )
    except Exception as ex:
        log.exception("connect failed")
        await m.answer(f"❌ Не удалось подключиться: {type(ex).__name__}. Попробуйте /start ещё раз.")
        await client.disconnect()
    finally:
        connecting.discard(uid)
        pending_pw.pop(uid, None)


@dp.message(Command("status"))
async def cmd_status(m: Message):
    mon = monitors.get(m.from_user.id)
    if not mon:
        await m.answer("Не подключено. /start")
        return
    s = mon.stats
    mode = "🟢 блокировка включена" if not mon.dry_run else "🟡 только уведомления"
    await m.answer(f"Подключено ✅\nРежим: {mode}\nПроверено новых: {s['checked']}, предупреждений: {s['warned']}, заблокировано: {s['blocked']}")


async def set_mode(m: Message, dry: bool):
    mon = monitors.get(m.from_user.id)
    if not mon:
        await m.answer("Не подключено. /start")
        return
    mon.dry_run = dry
    db.execute("UPDATE users SET dry_run=? WHERE owner_id=?", (int(dry), mon.owner_id))
    db.commit()
    await m.answer("🟡 Только уведомления." if dry else f"🟢 Блокировка включена (порог {BLOCK_AT:.0%}, не больше {MAX_BLOCKS_PER_HOUR}/час).")


@dp.message(Command("live"))
async def cmd_live(m: Message):
    await set_mode(m, False)


@dp.message(Command("dry"))
async def cmd_dry(m: Message):
    await set_mode(m, True)


@dp.message(Command("strict"))
async def cmd_strict(m: Message):
    """Защитный режим для тех, кто уже сталкивался с азартными играми: блокировка + автовыход из гемблинг-групп."""
    mon = monitors.get(m.from_user.id)
    if not mon:
        await m.answer("Не подключено. /start")
        return
    mon.dry_run = False
    mon.group_leave = True
    db.execute("UPDATE users SET dry_run=0, group_leave=1 WHERE owner_id=?", (mon.owner_id,))
    db.commit()
    await m.answer(
        "🛡 Защитный режим включён: блокирую спамеров и казино-боты, выхожу из групп и каналов про азартные игры, "
        "текст такой рекламы в уведомлениях скрыт. Чтобы сразу очистить аккаунт, запустите /scan.\n"
        "Смягчить: /dry (только уведомления) и /groupleave (выключить автовыход).\n"
        "Если тяга к играм мешает жить, поговорите с близкими или обратитесь к специалисту: это лечится, вы не один."
    )


@dp.message(Command("allow"), lambda m: m.from_user.id in ADMIN_IDS)
async def cmd_allow(m: Message):
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await m.answer("Использование: /allow <id> (вернуть доступ, закрытый через /revoke)")
        return
    db.execute("UPDATE access SET status='approved' WHERE user_id=?", (int(parts[1]),))
    db.commit()
    await m.answer(f"✅ Доступ для {parts[1]} возвращён.")


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    uid = m.from_user.id
    if uid not in monitors and not (wa and wa.row(uid)):
        await m.answer("Не подключено. /start или /whatsapp")
        return
    lines = []
    for title, since in (("За 24 часа", time.time() - 86400), ("За 7 дней", time.time() - 7 * 86400), ("За всё время", 0)):
        rows = dict(db.execute("SELECT action, COUNT(*) FROM events WHERE owner_id=? AND ts>=? GROUP BY action", (uid, since)).fetchall())
        total = sum(v for k, v in rows.items() if k != "test" and k != "known_link" and not k.startswith("group_"))
        flagged = sum(rows.get(k, 0) for k in ("warned", "would_block", "blocked", "false_positive"))
        fp = rows.get("false_positive", 0)
        lines.append(
            f"📊 {title}: проверено новых {total}, подозрительных {flagged} "
            f"(заблокировано {rows.get('blocked', 0)}, «был бы заблокирован» {rows.get('would_block', 0)}, "
            f"предупреждений {rows.get('warned', 0)}), ложных {fp}"
        )
    by_label = db.execute(
        "SELECT label, COUNT(*) FROM events WHERE owner_id=? AND action IN ('warned','would_block','blocked','false_positive') GROUP BY label ORDER BY 2 DESC",
        (uid,),
    ).fetchall()
    if by_label:
        lines.append("По типам: " + ", ".join(f"{REASONS.get(l, l).split(' (')[0]}: {c}" for l, c in by_label))
    prof = db.execute(
        "SELECT COUNT(*) FROM events WHERE owner_id=? AND profile_risk>=0.5 AND text_risk<0.5 AND action IN ('warned','would_block','blocked')",
        (uid,),
    ).fetchone()[0]
    lines.append(f"Поймано только по профилю (текст был «чистым»): {prof}")
    g = dict(db.execute("SELECT action, COUNT(*) FROM events WHERE owner_id=? AND action LIKE 'group_%' GROUP BY action", (uid,)).fetchall())
    lines.append(
        f"Группы и каналы: проверено {sum(g.values())}, вышел {g.get('group_left', 0)}, «вышел бы» {g.get('group_would_leave', 0)}, "
        f"предупреждений {g.get('group_warned', 0)}"
    )
    kl = db.execute("SELECT COUNT(*) FROM events WHERE owner_id=? AND action='known_link'", (uid,)).fetchone()[0]
    lines.append(f"Опасных ссылок от знакомых: {kl}")
    if ANALYTICS_ON:
        lines.append("Графики и итоги: /report")
    if wa:
        wa_line = wa.stats_text(uid)
        if wa_line:
            lines.append(wa_line)
    await m.answer("\n".join(lines))


@dp.callback_query(F.data.startswith("acc_"))
async def on_access_decision(q: CallbackQuery):
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Нет прав", show_alert=True)
        return
    action, uid_s = q.data.split(":")
    uid = int(uid_s)
    approve = action == "acc_ok"
    row = db.execute("SELECT name, username FROM access WHERE user_id=?", (uid,)).fetchone()
    if not row:
        await q.answer("Заявка не найдена")
        return
    db.execute("UPDATE access SET status=? WHERE user_id=?", ("approved" if approve else "rejected", uid))
    db.commit()
    try:
        await q.message.edit_text(f"{q.message.text}\n\n{'✅ Одобрено' if approve else '❌ Отклонено'}")
    except Exception:
        await q.message.edit_reply_markup(reply_markup=None)
    await q.answer("Одобрено" if approve else "Отклонено")
    try:
        await bot.send_message(
            uid,
            "✅ Доступ одобрен. Нажмите /start, чтобы подключить аккаунт." if approve else "❌ Доступ отклонён.",
        )
    except Exception as ex:
        log.warning("не удалось уведомить %s: %s", uid, ex)


@dp.message(Command("users"), lambda m: m.from_user.id in ADMIN_IDS)
async def cmd_users(m: Message):
    rows = db.execute("SELECT user_id, status, name, username, ts, last_seen FROM access ORDER BY COALESCE(last_seen, ts) DESC LIMIT 50").fetchall()
    lines = [f"Всего пользователей: {len(rows)} · открытый доступ: {'да' if OPEN_ACCESS else 'нет'}",
             f"Постоянные (.env): {', '.join(map(str, sorted(ALLOWED | ADMIN_IDS))) or 'нет'}"]
    for uid, status, name, uname, ts, seen in rows:
        mark = {"approved": "✅", "pending": "⏳", "rejected": "❌", "revoked": "🚫"}.get(status, "•")
        mon = monitors.get(uid)
        conn = " · TG" if mon else ""
        conn += " · WA" if wa and wa.row(uid) else ""
        mode = (" · блок." if not mon.dry_run else " · уведомл.") if mon else ""
        n30 = db.execute("SELECT COUNT(*), SUM(action IN ('warned','would_block','blocked','false_positive')) FROM events WHERE owner_id=? AND ts>=? AND action NOT LIKE 'group_%' AND action NOT IN ('test','known_link')",
                         (uid, time.time() - 30 * 86400)).fetchone()
        lines.append(f"{mark} {name}{' @' + uname if uname else ''} (id {uid}){conn}{mode} · 30 дн.: проверено {n30[0] or 0}, помечено {n30[1] or 0}")
    fs = feedback.stats()
    lines.append(f"Дообучение: отметок «не спам» {fs['marks']} от {fs['owners']} польз., в общей модели {fs['promoted']}, версия модели {fs['model_version']}")
    lines.append("Закрыть доступ: /revoke <id> · вернуть: /allow <id> · полная панель: /report → «Админ»")
    await m.answer(defang("\n".join(lines)))


@dp.message(Command("revoke"), lambda m: m.from_user.id in ADMIN_IDS)
async def cmd_revoke(m: Message):
    parts = (m.text or "").split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        await m.answer("Использование: /revoke <id>")
        return
    uid = int(parts[1])
    if uid in ADMIN_IDS or uid in ALLOWED:
        await m.answer("Этот ID задан в .env (постоянный). Уберите его из ALLOWED_IDS/ADMIN_IDS вручную.")
        return
    db.execute(
        "INSERT INTO access(user_id, status, name, username, ts) VALUES (?, 'revoked', '', '', ?) "
        "ON CONFLICT(user_id) DO UPDATE SET status='revoked'",
        (uid, time.time()),
    )
    db.commit()
    mon = monitors.pop(uid, None)
    if mon:
        try:
            await mon.client.log_out()  # завершает сессию на стороне Telegram
        except Exception:
            await mon.client.disconnect()
        db.execute("DELETE FROM users WHERE owner_id=?", (uid,))
        db.execute("DELETE FROM trusted WHERE owner_id=?", (uid,))
        db.commit()
    if wa:
        await wa.stop(uid)  # отзываем и WhatsApp-подключение
    await m.answer(f"🚫 Доступ для {uid} отозван" + (", его сессия завершена, данные удалены." if mon else "."))
    try:
        await bot.send_message(uid, "Ваш доступ к боту отозван.")
    except Exception:
        pass


def snapshot_now() -> dict:
    extra = {
        "lists_age_h": round((time.time() - linkchk.loaded_at) / 3600, 1) if linkchk and linkchk.loaded_at else None,
        "lists_gambling": len(linkchk.sets["gambling"]) if linkchk else 0,
        "lists_scam": len(linkchk.sets["scam"]) if linkchk else 0,
        "whatsapp": wa is not None,
    }
    return analytics.build_snapshot(db, pseudo_key=PSEUDO_KEY, tz_hours=TZ_HOURS, extra=extra)


async def analytics_loop() -> None:
    """Раз в сутки чистит старые события, каждые N минут отправляет на сайт свежие агрегаты."""
    last_prune = 0.0
    while True:
        try:
            if time.time() - last_prune > 86400:
                log.info("очистка старых событий: %s", analytics.prune(db, RETENTION_DAYS))
                last_prune = time.time()
            if ANALYTICS_ON:
                await analytics.push(f"{SITE_URL}/api/ingest", INGEST_SECRET, snapshot_now())
        except Exception:
            log.exception("analytics_loop")
        await asyncio.sleep(max(60, ANALYTICS_PUSH_MIN * 60))


@dp.message(Command("report"))
async def cmd_report(m: Message):
    uid = m.from_user.id
    if not allowed(uid):
        await request_access(m)
        return
    touch_user(m.from_user)
    if not ANALYTICS_ON:
        await m.answer("Отчёты на сайте не настроены на этом сервере.")
        return
    fresh = await analytics.push(f"{SITE_URL}/api/ingest", INGEST_SECRET, snapshot_now(), timeout=12)
    role = "admin" if uid in ADMIN_IDS else "user"
    token = analytics.make_token(SITE_SECRET, analytics.pseudo(PSEUDO_KEY, uid), role, ttl=86400)
    url = f"{SITE_URL}/?t={token}"
    btn = (InlineKeyboardButton(text="📊 Открыть отчёт", web_app=WebAppInfo(url=url)) if REPORT_AS_WEBAPP
           else InlineKeyboardButton(text="📊 Открыть отчёт", url=url))
    await m.answer(
        "📊 Ваш отчёт готов. Ссылка действует 24 часа и только для вас, не пересылайте её.\n"
        "На сайт попадают только цифры: тексты сообщений и ваши контакты не передаются."
        + ("" if fresh else "\n⚠️ Свежие данные отправить не удалось, на сайте могут быть прошлые."),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn]]),
    )


# ---------------------------------------------------------------- WhatsApp
async def _wa_ready(m: Message) -> bool:
    if wa is None:
        await m.answer("WhatsApp на этом сервере не настроен.")
        return False
    if not allowed(m.from_user.id):
        await request_access(m)
        return False
    touch_user(m.from_user)
    return True


@dp.message(Command("whatsapp"))
async def cmd_whatsapp(m: Message):
    if await _wa_ready(m):
        await wa.connect(m.from_user.id)


@dp.message(Command("wastatus"))
async def cmd_wastatus(m: Message):
    if await _wa_ready(m):
        await m.answer(wa.status_text(m.from_user.id))


@dp.message(Command("wastop"))
async def cmd_wastop(m: Message):
    if await _wa_ready(m):
        stopped = await wa.stop(m.from_user.id)
        await m.answer("WhatsApp отключён, подключение и данные удалены." if stopped else "WhatsApp не был подключён.")


@dp.message(Command("walive"))
async def cmd_walive(m: Message):
    if await _wa_ready(m):
        ok = wa.set_live(m.from_user.id, True)
        await m.answer(
            "🟢 WhatsApp: блокировка и автовыход из гемблинг-групп ВКЛЮЧЕНЫ (порог 85%). Выключить: /wadry. "
            "Помните: WhatsApp строго относится к автоматизации, блокировки аккаунта возможны."
            if ok else "WhatsApp не подключён. /whatsapp"
        )


@dp.message(Command("wadry"))
async def cmd_wadry(m: Message):
    if await _wa_ready(m):
        ok = wa.set_live(m.from_user.id, False)
        await m.answer("🟡 WhatsApp: только уведомления." if ok else "WhatsApp не подключён. /whatsapp")


@dp.callback_query(F.data.regexp(r"^(wtrust|wgtrust|wleave):\d+$"))
async def on_wa_buttons(q: CallbackQuery):
    if wa is None or not allowed(q.from_user.id):
        await q.answer("Нет доступа", show_alert=True)
        return
    action, ref_s = q.data.split(":")
    ref = int(ref_s)
    if action == "wleave":
        await q.answer(await wa.cb_leave(q.from_user.id, ref), show_alert=True)
        return
    ok = await wa.cb_trust(q.from_user.id, ref, group=(action == "wgtrust"))
    await q.answer("Больше не проверяю" if ok else "Не найдено")
    if ok:
        await q.message.edit_reply_markup(reply_markup=None)


@dp.message(Command("groupleave"))
async def cmd_groupleave(m: Message):
    mon = monitors.get(m.from_user.id)
    if not mon:
        await m.answer("Не подключено. /start")
        return
    mon.group_leave = not mon.group_leave
    db.execute("UPDATE users SET group_leave=? WHERE owner_id=?", (int(mon.group_leave), mon.owner_id))
    db.commit()
    if mon.group_leave:
        extra = "" if not mon.dry_run else "\nСейчас режим «только уведомления», поэтому выход заработает после /live."
        await m.answer(
            f"🚪 Автовыход из групп и каналов про {', '.join(sorted(GROUP_LEAVE_LABELS))} ВКЛЮЧЁН (порог {GROUP_LEAVE_AT:.0%}, "
            f"не больше {MAX_LEAVES_PER_HOUR}/час). Свои группы и те, где вы писали, не трогаю. Выключить: /groupleave.{extra}"
        )
    else:
        await m.answer("Автовыход выключен: бот только предупреждает о подозрительных группах.")


@dp.message(Command("scan"))
async def cmd_scan(m: Message):
    mon = monitors.get(m.from_user.id)
    if not mon:
        await m.answer("Не подключено. /start")
        return
    if mon.scanning:
        await m.answer("Проверка уже идёт, дождитесь отчёта.")
        return
    mon.scanning = True
    asyncio.create_task(mon.scan_groups())
    await m.answer("🔎 Проверяю ваши группы и каналы: название, описание и последние сообщения. Это займёт несколько минут, пришлю отчёт.")


@dp.message(Command("testmode"))
async def cmd_testmode(m: Message):
    mon = monitors.get(m.from_user.id)
    if not mon:
        await m.answer("Не подключено. /start")
        return
    mon.test_mode = not mon.test_mode
    others = sorted(allowed_ids() - {mon.me_id})
    await m.answer(
        f"🧪 Тестовый режим {'ВКЛЮЧЁН' if mon.test_mode else 'выключен'}.\n"
        + (
            f"Когда вам пишет кто-то из ALLOWED_IDS {others}, бот ничего не блокирует, а отвечает в чат от вашего имени: "
            f"«{TEST_MARK} я бы вас заблокировал…». Выключить: /testmode. После перезапуска сервиса режим сбрасывается."
            if mon.test_mode
            else "Обычная работа."
        )
    )


@dp.message(Command("stop"))
async def cmd_stop(m: Message):
    uid = m.from_user.id
    fut = pending_pw.pop(uid, None)
    if fut and not fut.done():
        fut.cancel()
    mon = monitors.pop(uid, None)
    if mon:
        try:
            await mon.client.log_out()  # завершает сессию на стороне Telegram
        except Exception:
            await mon.client.disconnect()
    db.execute("DELETE FROM users WHERE owner_id=?", (uid,))
    db.execute("DELETE FROM trusted WHERE owner_id=?", (uid,))
    db.commit()
    feedback.forget(uid)  # тексты, которые вы отмечали «не спам», тоже стираются
    if wa:
        await wa.stop(uid)  # заодно отключаем WhatsApp, если был подключён
    await m.answer("Отключено. Сессия завершена, данные удалены.")


@dp.message(F.text, lambda m: m.from_user.id in pending_pw)
async def on_password(m: Message):
    fut = pending_pw.get(m.from_user.id)
    try:
        await m.delete()  # пароль не должен оставаться в чате
    except Exception:
        pass
    if fut and not fut.done():
        fut.set_result(m.text)


@dp.message(F.text, lambda m: allowed(m.from_user.id) and not m.text.startswith("/"))
async def on_test_text(m: Message):
    """Проверка без второго аккаунта: пришлите (или перешлите) боту любой текст — он покажет вердикт."""
    res = guard.predict(m.text, owner=m.from_user.id)
    verdict = (
        "🚫 заблокировал бы" if res["risk"] >= BLOCK_AT
        else "⚠️ предупредил бы" if res["risk"] >= WARN_AT
        else "✅ пропустил бы"
    )
    probs = ", ".join(f"{k} {v:.0%}" for k, v in sorted(res["probs"].items(), key=lambda kv: -kv[1]))
    await m.answer(
        f"{verdict}\nКласс: {res['label']} · риск {res['risk']:.0%}\nМодель: {probs}"
        + (f"\nПравила: {'; '.join(res['reasons'])}" if res["reasons"] else "")
    )


@dp.callback_query(F.data.startswith("gtrust:"))
async def on_group_trust(q: CallbackQuery):
    mon = monitors.get(q.from_user.id)
    if not mon:
        await q.answer("Не подключено")
        return
    chat_id = int(q.data.split(":")[1])
    mon.trust(chat_id)
    mon.mark_checked(chat_id, "trusted", 0.0)
    learned = learn_from_button(mon, chat_id, "group")
    db.execute(
        "UPDATE events SET action='group_false_positive' WHERE id=(SELECT MAX(id) FROM events WHERE owner_id=? AND chat_id=? AND action LIKE 'group_%')",
        (mon.owner_id, chat_id),
    )
    db.commit()
    await q.answer("Больше не проверяю эту группу" + (". Запомнил, учусь на этом" if learned else ""))
    await q.message.edit_reply_markup(reply_markup=None)


@dp.callback_query(F.data.startswith("gleave:"))
async def on_group_leave(q: CallbackQuery):
    mon = monitors.get(q.from_user.id)
    if not mon:
        await q.answer("Не подключено")
        return
    chat_id = int(q.data.split(":")[1])
    try:
        await mon.client.delete_dialog(await mon.client.get_entity(chat_id))
        await q.answer("Вышел")
    except Exception as ex:
        await q.answer(f"Не удалось: {type(ex).__name__}", show_alert=True)
        return
    await q.message.edit_reply_markup(reply_markup=None)


def learn_from_button(mon: "Monitor", chat_id: int, source: str) -> bool:
    """Отметка «Это не спам»: запоминаем текст и дообучаем модель в фоне."""
    return bool(learn_texts(mon.owner_id, mon.flagged.pop(chat_id, []), source))


@dp.callback_query(F.data.startswith("show:"))
async def on_show(q: CallbackQuery):
    mon = monitors.get(q.from_user.id)
    texts = mon.flagged.get(int(q.data.split(":")[1]), []) if mon else []
    await q.answer(defang(texts[0][:190]) if texts else "Текст уже не сохранён", show_alert=True)


@dp.callback_query(F.data.startswith("trust:"))
async def on_trust(q: CallbackQuery):
    mon = monitors.get(q.from_user.id)
    if not mon:
        await q.answer("Не подключено")
        return
    chat_id = int(q.data.split(":")[1])
    mon.trust(chat_id)
    learned = learn_from_button(mon, chat_id, "chat")
    db.execute(
        "UPDATE events SET action='false_positive' WHERE id=(SELECT MAX(id) FROM events WHERE owner_id=? AND chat_id=? AND action IN ('warned','would_block','blocked'))",
        (mon.owner_id, chat_id),
    )
    db.commit()
    try:
        await mon.client(functions.contacts.UnblockRequest(id=await mon.client.get_input_entity(chat_id)))
        await q.answer("Добавлено в доверенные и разблокировано" + (". Запомнил, учусь на этом" if learned else ""))
    except Exception:
        await q.answer("Добавлено в доверенные (разблокируйте вручную, если был блок)")
    await q.message.edit_reply_markup(reply_markup=None)


async def restore() -> None:
    for owner_id, enc, dry in db.execute("SELECT owner_id, session, dry_run FROM users").fetchall():
        try:
            client = TelegramClient(StringSession(fernet.decrypt(enc).decode()), API_ID, API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                raise RuntimeError("сессия недействительна")
            me = await client.get_me()
            monitors[owner_id] = Monitor(owner_id, client, me.id, bool(dry))
            log.info("restored session for %s", owner_id)
        except Exception as ex:
            log.warning("cannot restore %s: %s", owner_id, ex)
            db.execute("DELETE FROM users WHERE owner_id=?", (owner_id,))
            db.commit()
            try:
                await bot.send_message(owner_id, "Сессия аккаунта недействительна, подключитесь заново: /start")
            except Exception:
                pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    await restore()
    if feedback.promoted():  # подтверждённые отметки есть: подтягиваем их в модель при старте
        asyncio.create_task(feedback.retrain_if_needed(force=True))
    asyncio.create_task(analytics_loop())
    if linkchk:
        asyncio.create_task(linkchk.refresh_loop())  # загрузка и ежедневное обновление списков
        log.info("проверка ссылок включена (RDAP: %s)", linkchk.rdap)
    if wa:
        await wa.start_server(int(os.environ.get("WA_WEBHOOK_PORT", 8081)))
        asyncio.create_task(wa.janitor_loop())
        log.info("WhatsApp (Evolution) включён, уборщик экземпляров запущен")
    me = await bot.get_me()
    log.info("bot @%s started, allowed ids: %s", me.username, sorted(ALLOWED) or "NONE (все получат отказ)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
