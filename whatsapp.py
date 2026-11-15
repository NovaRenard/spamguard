"""WhatsApp через Evolution API (Baileys): подключение по QR из Telegram, анализ чужих чатов и групп.

Схема:
  Telegram /whatsapp -> создаём экземпляр Evolution `sg_<tg_id>` -> присылаем QR -> пользователь сканирует
  -> Evolution шлёт вебхуки (qrcode.updated, connection.update, messages.upsert, group-participants.update)
  -> анализ тем же классификатором -> предупреждения приходят в Telegram.

Чтобы экземпляры не копились, работает «уборщик» (janitor): раз в минуту сверяет Evolution с нашей базой.
  • экземпляр без подключения старше WA_PAIR_TTL_SEC (по умолчанию 5 мин) удаляется;
  • экземпляр, которого нет в нашей базе (сирота), удаляется;
  • подключённый, но отключившийся дольше WA_CLOSED_TTL_SEC (по умолчанию 1 час), удаляется.

Важно (проверено на живом Evolution 2.3.7): вызовы к неподключённому экземпляру зависают, поэтому все обращения
к WhatsApp идут только при состоянии `open`, с таймаутом и в try/except.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections import deque

import aiohttp
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from aiohttp import web

from classifier import has_link, normalize, rule_score
from groupcheck import _short_text_signal, score_chat

log = logging.getLogger("whatsapp")

GROUP_REASONS = {"gambling": "азартные игры / ставки / казино", "scam": "мошенничество", "spam": "спам / реклама"}
REASONS = {
    "gambling": "продвигает азартные игры / казино / ставки",
    "scam": "признаки мошенничества",
    "spam": "рассылает спам / рекламу",
}


def defang(s: str) -> str:
    return s.replace("http", "hxxp").replace("t.me/", "t[.]me/").replace("@", "＠")


def _noisy_or(ps: list[float]) -> float:
    out = 1.0
    for p in ps:
        out *= 1 - min(max(p, 0.0), 1.0)
    return 1 - out


def extract_text(message: dict) -> str:
    """Достаёт текст из разных типов сообщений WhatsApp."""
    if not isinstance(message, dict):
        return ""
    for k in ("conversation",):
        if isinstance(message.get(k), str):
            return message[k]
    for k in ("extendedTextMessage", "imageMessage", "videoMessage", "documentMessage"):
        v = message.get(k)
        if isinstance(v, dict):
            t = v.get("text") or v.get("caption")
            if t:
                return t
    return ""


def _ts(v) -> int | None:
    """messageTimestamp бывает числом, строкой или {low, high, unsigned}."""
    try:
        if isinstance(v, dict):
            v = v.get("low")
        return int(v)
    except (TypeError, ValueError):
        return None


def jid_number(jid: str) -> str:
    return jid.split("@")[0]


class EvolutionError(Exception):
    pass


class Evolution:
    """Тонкий клиент Evolution API. Ключ глобальный, у него полный доступ: наружу он не выходит."""

    def __init__(self, base: str, key: str):
        self.base, self.key = base.rstrip("/"), key
        self._session: aiohttp.ClientSession | None = None

    async def _req(self, method: str, path: str, body=None, timeout: float = 20):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        try:
            async with self._session.request(
                method, self.base + path, json=body, headers={"apikey": self.key}, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as r:
                text = await r.text()
                try:
                    data = json.loads(text) if text else None
                except ValueError:
                    data = text
                if r.status >= 400:
                    raise EvolutionError(f"{method} {path.split('?')[0]} -> {r.status}: {str(data)[:160]}")
                return data
        except asyncio.TimeoutError as e:
            raise EvolutionError(f"{method} {path.split('?')[0]}: таймаут {timeout}с") from e
        except aiohttp.ClientError as e:
            raise EvolutionError(f"{method} {path.split('?')[0]}: {e}") from e

    async def create(self, name: str) -> dict:
        return await self._req(
            "POST", "/instance/create",
            {"instanceName": name, "integration": "WHATSAPP-BAILEYS", "qrcode": True, "groupsIgnore": False,
             "readMessages": False, "alwaysOnline": False, "rejectCall": False, "syncFullHistory": False},
            timeout=40,
        )

    async def set_webhook(self, name: str, url: str) -> None:
        # В create сохраняется только событие QR, поэтому вебхук задаём отдельно (проверено)
        await self._req(
            "POST", f"/webhook/set/{name}",
            {"webhook": {"enabled": True, "url": url, "webhookByEvents": False, "webhookBase64": False,
                         "events": ["QRCODE_UPDATED", "CONNECTION_UPDATE", "MESSAGES_UPSERT", "GROUP_PARTICIPANTS_UPDATE"]}},
        )

    async def connect(self, name: str, number: str | None = None) -> dict:
        q = f"?number={number}" if number else ""
        return await self._req("GET", f"/instance/connect/{name}{q}", timeout=30)

    async def state(self, name: str) -> str:
        r = await self._req("GET", f"/instance/connectionState/{name}", timeout=10)
        return ((r or {}).get("instance") or {}).get("state", "unknown")

    async def instances(self) -> list[dict]:
        r = await self._req("GET", "/instance/fetchInstances", timeout=15)
        return r if isinstance(r, list) else []

    async def delete(self, name: str) -> None:
        await self._req("DELETE", f"/instance/delete/{name}", timeout=20)

    async def logout(self, name: str) -> None:
        await self._req("DELETE", f"/instance/logout/{name}", timeout=15)

    async def find_messages(self, name: str, jid: str, limit: int = 30) -> list[dict]:
        r = await self._req("POST", f"/chat/findMessages/{name}", {"where": {"key": {"remoteJid": jid}}, "limit": limit}, timeout=15)
        return ((r or {}).get("messages") or {}).get("records") or []

    async def group_info(self, name: str, gjid: str) -> dict:
        return await self._req("GET", f"/group/findGroupInfos/{name}?groupJid={gjid}", timeout=15)

    async def leave_group(self, name: str, gjid: str) -> None:
        await self._req("DELETE", f"/group/leaveGroup/{name}?groupJid={gjid}", timeout=20)

    async def block(self, name: str, jid: str) -> None:
        await self._req("POST", f"/chat/updateBlockStatus/{name}", {"number": jid, "status": "block"}, timeout=20)

    async def profile(self, name: str, number: str) -> dict:
        return await self._req("POST", f"/chat/fetchProfile/{name}", {"number": number}, timeout=12)

    async def picture(self, name: str, number: str) -> dict:
        return await self._req("POST", f"/chat/fetchProfilePictureUrl/{name}", {"number": number}, timeout=12)


SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_instances(
    owner_id INTEGER PRIMARY KEY, instance TEXT UNIQUE, status TEXT, created_at REAL, connected_at REAL, closed_at REAL,
    phone TEXT, wa_live INT DEFAULT 0, qr_msg INT);
CREATE TABLE IF NOT EXISTS wa_trusted(owner_id INT, jid TEXT, PRIMARY KEY(owner_id, jid));
CREATE TABLE IF NOT EXISTS wa_checks(owner_id INT, jid TEXT, ts REAL, verdict TEXT, risk REAL, PRIMARY KEY(owner_id, jid));
CREATE TABLE IF NOT EXISTS wa_events(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INT, ts REAL, jid TEXT, kind TEXT,
    label TEXT, text_risk REAL, profile_risk REAL, risk REAL, action TEXT, reasons TEXT);
CREATE TABLE IF NOT EXISTS wa_refs(id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INT, jid TEXT, ts REAL);
"""


class WhatsAppService:
    def __init__(self, *, bot, db, guard, evo: Evolution, webhook_base: str, secret: str, warn_at=0.5, block_at=0.85,
                 group_warn_at=0.5, group_leave_at=0.85, leave_labels=frozenset({"gambling"}), pair_ttl=300,
                 closed_ttl=3600, max_instances=10, max_blocks_per_hour=5, max_leaves_per_hour=10, recheck_days=7.0,
                 links=None, link_warn_known=0.8):
        self.bot, self.db, self.guard, self.evo = bot, db, guard, evo
        self.links, self.link_warn_known, self.known_alert = links, link_warn_known, {}
        self.webhook_url = f"{webhook_base.rstrip('/')}/wa/{secret}"
        self.secret = secret
        self.warn_at, self.block_at = warn_at, block_at
        self.group_warn_at, self.group_leave_at, self.leave_labels = group_warn_at, group_leave_at, set(leave_labels)
        self.pair_ttl, self.closed_ttl, self.max_instances = pair_ttl, closed_ttl, max_instances
        self.max_blocks, self.max_leaves, self.recheck = max_blocks_per_hour, max_leaves_per_hour, recheck_days
        self.block_times: dict[int, deque] = {}
        self.leave_times: dict[int, deque] = {}
        self.burst: dict[tuple, deque] = {}
        self.seen_ids: deque = deque(maxlen=3000)
        self.group_busy: set[tuple] = set()
        self.group_sem = asyncio.Semaphore(1)
        self.my_jid: dict[int, str] = {}
        db.executescript(SCHEMA)
        try:  # аналитика: домены опасных ссылок
            db.execute("ALTER TABLE wa_events ADD COLUMN domains TEXT")
            db.commit()
        except Exception:
            pass

    # ------------------------------------------------------------------ база
    def row(self, owner_id: int) -> dict | None:
        cur = self.db.execute("SELECT * FROM wa_instances WHERE owner_id=?", (owner_id,))
        r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r)) if r else None

    def row_by_instance(self, name: str) -> dict | None:
        cur = self.db.execute("SELECT * FROM wa_instances WHERE instance=?", (name,))
        r = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], r)) if r else None

    def is_trusted(self, owner_id: int, jid: str) -> bool:
        return self.db.execute("SELECT 1 FROM wa_trusted WHERE owner_id=? AND jid=?", (owner_id, jid)).fetchone() is not None

    def trust(self, owner_id: int, jid: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO wa_trusted VALUES (?,?)", (owner_id, jid))
        self.db.commit()

    def ref(self, owner_id: int, jid: str) -> int:
        """Короткий номер для callback_data (в Telegram лимит 64 байта)."""
        cur = self.db.execute("INSERT INTO wa_refs(owner_id, jid, ts) VALUES (?,?,?)", (owner_id, jid, time.time()))
        self.db.commit()
        return cur.lastrowid

    def ref_jid(self, owner_id: int, ref: int) -> str | None:
        r = self.db.execute("SELECT jid FROM wa_refs WHERE id=? AND owner_id=?", (ref, owner_id)).fetchone()
        return r[0] if r else None

    def record(self, owner_id, jid, kind, label, text_risk, prof_risk, risk, action, reasons, domains=None):
        self.db.execute(
            "INSERT INTO wa_events(owner_id, ts, jid, kind, label, text_risk, profile_risk, risk, action, reasons, domains) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (owner_id, time.time(), jid, kind, label, text_risk, prof_risk, risk, action, "; ".join(reasons), ",".join(domains or [])),
        )
        self.db.commit()

    async def notify(self, owner_id: int, text: str, markup=None) -> None:
        try:
            await self.bot.send_message(owner_id, text, reply_markup=markup)
        except Exception as ex:
            log.warning("не удалось написать %s: %s", owner_id, ex)

    # ------------------------------------------------------------------ подключение
    @staticmethod
    def _qr_file(b64: str) -> BufferedInputFile:
        raw = b64.split(",", 1)[1] if b64.startswith("data:") else b64
        return BufferedInputFile(base64.b64decode(raw), filename="whatsapp_qr.png")

    def _caption(self) -> str:
        mins = max(1, int(self.pair_ttl // 60))
        return (
            "Привязка WhatsApp: на телефоне откройте WhatsApp → Настройки → Связанные устройства → "
            "Привязка устройства и отсканируйте этот QR с другого экрана.\n"
            f"QR обновляется сам. Если не подключиться за {mins} мин., экземпляр удалится, тогда отправьте /whatsapp снова."
        )

    async def connect(self, owner_id: int) -> None:
        row = self.row(owner_id)
        if row and row["status"] == "connected":
            await self.notify(owner_id, f"✅ WhatsApp уже подключён. Статус: /wastatus. Отключить: /wastop")
            return
        if row and row["status"] == "pending":
            try:  # новый QR для уже созданного экземпляра
                r = await self.evo.connect(row["instance"])
                if r and r.get("base64"):
                    await self._show_qr(owner_id, r["base64"], force_new=True)
                    return
            except EvolutionError:
                pass
            await self._drop(owner_id, "pending пересоздаём")  # не получилось — пересоздадим ниже
        count = self.db.execute("SELECT COUNT(*) FROM wa_instances").fetchone()[0]
        if count >= self.max_instances:
            await self.notify(owner_id, "Сейчас слишком много подключений WhatsApp, попробуйте позже.")
            return
        name = f"sg_{owner_id}"
        try:
            await self.evo.delete(name)  # остатки от прошлых попыток
        except EvolutionError:
            pass
        try:
            res = await self.evo.create(name)
            await self.evo.set_webhook(name, self.webhook_url)
        except EvolutionError as ex:
            log.warning("create failed: %s", ex)
            try:
                await self.evo.delete(name)
            except EvolutionError:
                pass
            await self.notify(owner_id, "❌ Не удалось создать подключение WhatsApp. Попробуйте позже.")
            return
        self.db.execute(
            "INSERT OR REPLACE INTO wa_instances(owner_id, instance, status, created_at, wa_live) VALUES (?,?,?,?,0)",
            (owner_id, name, "pending", time.time()),
        )
        self.db.commit()
        qr = ((res or {}).get("qrcode") or {}).get("base64")
        if qr:
            await self._show_qr(owner_id, qr, force_new=True)
        else:
            await self.notify(owner_id, "Подключение создано, жду QR…")

    async def _show_qr(self, owner_id: int, b64: str, force_new: bool = False) -> None:
        row = self.row(owner_id)
        file, cap = self._qr_file(b64), self._caption()
        if row and row.get("qr_msg") and not force_new:
            try:
                await self.bot.edit_message_media(chat_id=owner_id, message_id=row["qr_msg"], media=InputMediaPhoto(media=file, caption=cap))
                return
            except Exception:
                pass  # сообщение удалено или не изменилось: пришлём новое
        try:
            msg = await self.bot.send_photo(owner_id, file, caption=cap)
            self.db.execute("UPDATE wa_instances SET qr_msg=? WHERE owner_id=?", (msg.message_id, owner_id))
            self.db.commit()
        except Exception as ex:
            log.warning("не удалось отправить QR: %s", ex)

    async def _drop(self, owner_id: int, why: str, wipe_data: bool = False) -> None:
        """Удаляет экземпляр в Evolution и строку в базе."""
        row = self.row(owner_id)
        if not row:
            return
        try:
            await self.evo.delete(row["instance"])
        except EvolutionError as ex:
            log.warning("delete %s: %s", row["instance"], ex)
        self.db.execute("DELETE FROM wa_instances WHERE owner_id=?", (owner_id,))
        if wipe_data:
            for t in ("wa_trusted", "wa_checks", "wa_events", "wa_refs"):
                self.db.execute(f"DELETE FROM {t} WHERE owner_id=?", (owner_id,))
        self.db.commit()
        self.my_jid.pop(owner_id, None)
        log.info("экземпляр владельца %s удалён: %s", owner_id, why)

    async def stop(self, owner_id: int) -> bool:
        """Отключение по команде: выходим из устройства в WhatsApp, удаляем экземпляр и данные."""
        row = self.row(owner_id)
        if not row:
            return False
        try:
            await self.evo.logout(row["instance"])
        except EvolutionError:
            pass
        await self._drop(owner_id, "по команде", wipe_data=True)
        return True

    def set_live(self, owner_id: int, live: bool) -> bool:
        if not self.row(owner_id):
            return False
        self.db.execute("UPDATE wa_instances SET wa_live=? WHERE owner_id=?", (int(live), owner_id))
        self.db.commit()
        return True

    def status_text(self, owner_id: int) -> str:
        row = self.row(owner_id)
        if not row:
            return "WhatsApp не подключён. Подключить: /whatsapp"
        if row["status"] == "pending":
            left = max(0, int(row["created_at"] + self.pair_ttl - time.time()))
            return f"⏳ WhatsApp ждёт сканирования QR ещё ~{left} с. Новый QR: /whatsapp"
        mode = "🟢 блокировка и выход из групп включены" if row["wa_live"] else "🟡 только уведомления"
        phone = f" ({row['phone']})" if row.get("phone") else ""
        closed = " ⚠️ сейчас нет связи, жду восстановления" if row.get("closed_at") else ""
        return f"WhatsApp подключён{phone}{closed}\nРежим: {mode}"

    def stats_text(self, owner_id: int) -> str | None:
        if not self.row(owner_id):
            return None
        a = dict(self.db.execute("SELECT action, COUNT(*) FROM wa_events WHERE owner_id=? GROUP BY action", (owner_id,)).fetchall())
        chats = sum(v for k, v in a.items() if not k.startswith("group_") and k != "known_link")
        groups = sum(v for k, v in a.items() if k.startswith("group_"))
        return (
            f"WhatsApp: чатов проверено {chats}, предупреждений {a.get('warned', 0)}, "
            f"заблокировано {a.get('blocked', 0)}, «был бы заблокирован» {a.get('would_block', 0)}; "
            f"групп проверено {groups}, вышел {a.get('group_left', 0)}, «вышел бы» {a.get('group_would_leave', 0)}"
        )

    # ------------------------------------------------------------------ вебхуки
    async def handle_webhook(self, request: web.Request) -> web.Response:
        if request.match_info.get("secret") != self.secret:
            return web.Response(status=404)
        try:
            payload = await request.json()
        except Exception:
            return web.Response(status=400)
        asyncio.create_task(self._dispatch(payload))
        return web.Response(text="ok")  # Evolution повторяет запрос при ошибках, поэтому отвечаем сразу

    async def _dispatch(self, payload: dict) -> None:
        try:
            event = re.sub(r"[-_]", ".", str(payload.get("event", "")).lower())
            row = self.row_by_instance(payload.get("instance", ""))
            if not row:
                return
            owner, data = row["owner_id"], payload.get("data")
            if event == "qrcode.updated":
                qr = (data or {}).get("qrcode") or data or {}
                if row["status"] == "pending" and qr.get("base64"):
                    await self._show_qr(owner, qr["base64"])
            elif event == "connection.update":
                await self._on_connection(row, data or {})
            elif event == "messages.upsert":
                for m in (data if isinstance(data, list) else [data]):
                    if isinstance(m, dict):
                        await self.on_message(row, m)
            elif event == "group.participants.update":
                await self._on_participants(row, data or {})
        except Exception:
            log.exception("ошибка обработки вебхука")

    async def _on_connection(self, row: dict, data: dict) -> None:
        owner, state = row["owner_id"], data.get("state")
        if state == "open":
            was_pending = row["status"] != "connected"
            phone = None
            try:
                for i in await self.evo.instances():
                    if i.get("name") == row["instance"]:
                        phone = jid_number(i.get("ownerJid") or "") or None
                        if i.get("ownerJid"):
                            self.my_jid[owner] = i["ownerJid"]
            except EvolutionError:
                pass
            self.db.execute(
                "UPDATE wa_instances SET status='connected', connected_at=COALESCE(connected_at, ?), closed_at=NULL, phone=COALESCE(?, phone) WHERE owner_id=?",
                (time.time(), phone, owner),
            )
            self.db.commit()
            if was_pending:
                await self.notify(
                    owner,
                    "✅ WhatsApp подключён. Анализ запущен: проверяю сообщения от новых собеседников и группы.\n\n"
                    "Сейчас режим «только уведомления»: ничего не блокирую. Включить блокировку и выход из гемблинг-групп: /walive. "
                    "Отключить: /wastop.",
                )
        elif state == "close":
            if data.get("statusReason") == 401 and row["status"] == "connected":
                await self._drop(owner, "устройство удалено в телефоне")
                await self.notify(owner, "⚠️ WhatsApp отключён (вы удалили устройство в телефоне). Данные удалены. Подключить снова: /whatsapp")
            elif row["status"] == "connected" and not row.get("closed_at"):
                self.db.execute("UPDATE wa_instances SET closed_at=? WHERE owner_id=?", (time.time(), owner))
                self.db.commit()

    # ------------------------------------------------------------------ личные чаты
    async def is_new_chat(self, row: dict, jid: str) -> bool:
        """Новый = вы туда не писали и в синхронизированной истории чата нет сообщений до подключения."""
        try:
            recs = await self.evo.find_messages(row["instance"], jid, limit=30)
        except EvolutionError:
            return False  # не уверены, поэтому не трогаем (лучше пропуск, чем ложная тревога)
        connected_at = row.get("connected_at") or 0
        for r in recs:
            if (r.get("key") or {}).get("fromMe"):
                self.trust(row["owner_id"], jid)
                return False
            ts = _ts(r.get("messageTimestamp"))
            if ts and connected_at and ts < connected_at - 5:
                return False  # переписка существовала до подключения бота
        return True

    async def profile_risk(self, row: dict, jid: str, push_name: str, text: str, burst: int) -> tuple[float, list[str], str | None]:
        strong, reasons, label, weak = [], [], None, 0.0
        r, lab, _ = rule_score(normalize(push_name or ""))
        if r > 0:
            strong.append(0.8 * r)
            reasons.append(f"имя похоже на «{lab}»")
            label = lab
        about, has_pic_key, pic = "", False, None
        number = jid_number(jid) if jid.endswith("@s.whatsapp.net") else jid
        try:
            prof = await self.evo.profile(row["instance"], number)
            if isinstance(prof, dict):
                st = prof.get("status")
                about = (st.get("status") if isinstance(st, dict) else st) or prof.get("about") or prof.get("description") or ""
                if "picture" in prof or "profilePictureUrl" in prof:
                    has_pic_key, pic = True, prof.get("picture") or prof.get("profilePictureUrl")
        except EvolutionError:
            pass  # профиль не прочитался — просто без этих признаков
        if about and isinstance(about, str):
            a_risk, a_lab = _short_text_signal(self.guard, about, 0.7)
            if a_risk > 0:
                strong.append(0.85 * a_risk)
                reasons.append(f"в «О себе»: {a_lab}")
                label = label or a_lab
            elif has_link(about):
                weak += 0.10
                reasons.append("ссылка в «О себе»")
        if has_pic_key and not pic:
            weak += 0.08
            reasons.append("нет фото")
        if has_link(text):
            weak += 0.10
            reasons.append("ссылка в сообщении")
        if burst >= 3:
            weak += 0.15
            reasons.append(f"{burst} сообщений за минуту")
        return _noisy_or(strong + [min(weak, 0.35)]), reasons, label

    def _limited(self, store: dict, owner_id: int, limit: int) -> bool:
        q = store.setdefault(owner_id, deque())
        now = time.time()
        while q and now - q[0] > 3600:
            q.popleft()
        return len(q) >= limit

    async def on_message(self, row: dict, m: dict) -> None:
        owner = row["owner_id"]
        key = m.get("key") or {}
        jid = key.get("remoteJid") or ""
        mid = key.get("id")
        if mid:
            if mid in self.seen_ids:
                return  # повтор вебхука
            self.seen_ids.append(mid)
        if jid.endswith("@g.us"):
            kind = "group"
        elif jid.endswith(("@s.whatsapp.net", "@lid")):
            kind = "chat"
        else:
            return  # статусы, рассылки, каналы
        if key.get("fromMe"):
            self.trust(owner, jid)  # вы написали: собеседник/группа доверенные
            return
        if row["status"] != "connected":
            return
        ts = _ts(m.get("messageTimestamp"))
        if ts and time.time() - ts > 600:
            return  # старое сообщение (доставилось с опозданием)
        if kind == "group":
            self.schedule_group_check(row, jid)
            return
        text = extract_text(m.get("message") or {})
        if not text:
            return
        if self.is_trusted(owner, jid) or not await self.is_new_chat(row, jid):
            await self.check_known_links(row, jid, m, text)  # знакомым: только явная опасность
            return

        q = self.burst.setdefault((owner, jid), deque(maxlen=10))
        now = time.time()
        q.append(now)
        burst = sum(1 for t in q if now - t <= 60)
        res = self.guard.predict(text)
        lk = await self.links.check_text(text) if self.links else None
        doms = [r.host for r in lk.results if r.risk >= 0.3] if lk else []
        if lk and lk.risk > 0:  # ссылки усиливают оценку текста
            res = {**res, "risk": 1 - (1 - res["risk"]) * (1 - lk.risk), "label": res["label"] if res["label"] != "ok" else lk.label}
        p_risk, p_reasons, p_label = await self.profile_risk(row, jid, m.get("pushName") or "", text, burst)
        risk = 1 - (1 - res["risk"]) * (1 - p_risk)
        label = res["label"] if res["label"] != "ok" else (p_label or "spam")
        if risk < self.warn_at:
            self.record(owner, jid, "chat", "ok", res["risk"], p_risk, risk, "ok", p_reasons, domains=doms)
            return

        alt = key.get("remoteJidAlt") or jid
        head = defang(f"{m.get('pushName') or '?'} (+{jid_number(alt)})")
        quote = f"«{defang(text[:200])}»"
        reason = REASONS.get(label, label)
        prof = f"\nПризнаки профиля: {', '.join(p_reasons)}." if p_reasons else ""
        if lk and lk.reasons:
            prof += f"\nСсылки: {'; '.join(lk.reasons)}."
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Это не спам, доверять", callback_data=f"wtrust:{self.ref(owner, jid)}")]])

        if risk < self.block_at:
            self.record(owner, jid, "chat", label, res["risk"], p_risk, risk, "warned", p_reasons, domains=doms)
            await self.notify(owner, f"⚠️ WhatsApp: подозрительное сообщение от {head}: {reason}, риск {risk:.0%}.{prof}\n{quote}\nНе заблокирован.", markup)
            return
        if not row["wa_live"] or self._limited(self.block_times, owner, self.max_blocks):
            why = "режим «только уведомления», включить: /walive" if not row["wa_live"] else "лимит блокировок в час"
            self.record(owner, jid, "chat", label, res["risk"], p_risk, risk, "would_block", p_reasons, domains=doms)
            await self.notify(owner, f"🛡 WhatsApp: был бы заблокирован ({why}): {head} — {reason}, риск {risk:.0%}.{prof}\n{quote}", markup)
            return
        try:
            await self.evo.block(row["instance"], jid)
        except EvolutionError as ex:
            self.record(owner, jid, "chat", label, res["risk"], p_risk, risk, "block_failed", p_reasons, domains=doms)
            await self.notify(owner, f"❌ WhatsApp: не удалось заблокировать {head}: {ex}\n{quote}", markup)
            return
        self.block_times.setdefault(owner, deque()).append(time.time())
        self.record(owner, jid, "chat", label, res["risk"], p_risk, risk, "blocked", p_reasons, domains=doms)
        await self.notify(owner, f"🛡 WhatsApp: {head} заблокирован — {reason} (риск {risk:.0%}).{prof}\n{quote}", markup)

    async def check_known_links(self, row: dict, jid: str, m: dict, text: str) -> None:
        """Знакомому тоже проверяем ссылки (вдруг взломали). Только предупреждение при явной опасности, без блокировки."""
        if not self.links:
            return
        lk = await self.links.check_text(text)
        if lk.risk < self.link_warn_known:
            return
        owner, now = row["owner_id"], time.time()
        if now - self.known_alert.get((owner, jid), 0) < 600:
            return
        self.known_alert[(owner, jid)] = now
        top = max(lk.results, key=lambda r: r.risk)
        alt = (m.get("key") or {}).get("remoteJidAlt") or jid
        head = defang(f"{m.get('pushName') or '?'} (+{jid_number(alt)})")
        self.record(owner, jid, "chat", lk.label, lk.risk, 0.0, lk.risk, "known_link", lk.reasons, domains=[r.host for r in lk.results if r.risk >= 0.3])
        await self.notify(
            owner,
            f"⚠️ WhatsApp: знакомый {head} прислал опасную ссылку: {defang(top.host)} (риск {lk.risk:.0%}).\n"
            f"Почему: {'; '.join(lk.reasons)}.\nВозможно, его аккаунт взломали. Не открывайте ссылку и предупредите его другим способом.",
        )

    # ------------------------------------------------------------------ группы
    def schedule_group_check(self, row: dict, gjid: str) -> None:
        owner = row["owner_id"]
        r = self.db.execute("SELECT ts FROM wa_checks WHERE owner_id=? AND jid=?", (owner, gjid)).fetchone()
        if r and time.time() - r[0] < self.recheck * 86400:
            return
        if self.is_trusted(owner, gjid) or (owner, gjid) in self.group_busy:
            return
        self.group_busy.add((owner, gjid))
        asyncio.create_task(self._group_task(row, gjid))

    async def _group_task(self, row: dict, gjid: str) -> None:
        try:
            async with self.group_sem:
                await self.check_group(row, gjid)
                await asyncio.sleep(1.5)
        except Exception:
            log.exception("проверка группы %s", gjid)
        finally:
            self.group_busy.discard((row["owner_id"], gjid))

    def _mark_group(self, owner: int, gjid: str, verdict: str, risk: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO wa_checks VALUES (?,?,?,?,?)", (owner, gjid, time.time(), verdict, risk))
        self.db.commit()

    async def _on_participants(self, row: dict, data: dict) -> None:
        gjid, action, parts = data.get("id") or data.get("groupJid"), data.get("action"), data.get("participants") or []
        me = self.my_jid.get(row["owner_id"])
        if gjid and action == "add" and me and any(jid_number(p if isinstance(p, str) else (p or {}).get("id", "")) == jid_number(me) for p in parts):
            self.schedule_group_check(row, gjid)  # вас добавили в группу

    async def check_group(self, row: dict, gjid: str) -> dict | None:
        owner, inst = row["owner_id"], row["instance"]
        try:
            info = await self.evo.group_info(inst, gjid)
            recs = await self.evo.find_messages(inst, gjid, limit=25)
        except EvolutionError as ex:
            log.info("группа %s: нет данных (%s)", gjid, ex)
            return None  # не пометим проверенной: попробуем при следующем сообщении
        me = self.my_jid.get(owner) or ""
        # свои группы и те, где вы писали, не трогаем
        mine = me and (info.get("owner") == me or any(
            jid_number((p or {}).get("id", "")) == jid_number(me) and (p or {}).get("admin") for p in (info.get("participants") or [])))
        if mine or any((r.get("key") or {}).get("fromMe") for r in recs):
            self.trust(owner, gjid)
            self._mark_group(owner, gjid, "own", 0.0)
            return None
        texts = [extract_text(r.get("message") or {}) for r in recs if not (r.get("key") or {}).get("fromMe")]
        title = info.get("subject") or ""
        res = score_chat(title, None, info.get("desc") or "", texts, self.guard)
        risk, label = res["risk"], res["label"]
        self._mark_group(owner, gjid, label, risk)
        if risk < self.group_warn_at:
            self.record(owner, gjid, "group", label, risk, 0.0, risk, "group_ok", res["reasons"])
            return {"action": "group_ok"}

        name = defang(f"«{title}»")
        why = "; ".join(res["reasons"]) or "признаки в сообщениях"
        reason = GROUP_REASONS.get(label, label)
        ref = self.ref(owner, gjid)
        trust_btn = InlineKeyboardButton(text="✅ Это не то, доверять", callback_data=f"wgtrust:{ref}")
        leave_btn = InlineKeyboardButton(text="🚪 Выйти", callback_data=f"wleave:{ref}")
        can_leave = label in self.leave_labels and risk >= self.group_leave_at
        if not can_leave:
            action = "group_warned"
            text = f"⚠️ WhatsApp: группа {name} похожа на тему «{reason}» (риск {risk:.0%}).\nПочему: {why}.\nАвтоматически не выхожу."
            markup = InlineKeyboardMarkup(inline_keyboard=[[leave_btn, trust_btn]])
        elif not row["wa_live"] or self._limited(self.leave_times, owner, self.max_leaves):
            action = "group_would_leave"
            mode = "режим «только уведомления»: /walive" if not row["wa_live"] else "лимит выходов в час"
            text = f"🚪 WhatsApp: я бы вышел из группы {name} ({mode}): {reason}, риск {risk:.0%}.\nПочему: {why}."
            markup = InlineKeyboardMarkup(inline_keyboard=[[leave_btn, trust_btn]])
        else:
            try:
                await self.evo.leave_group(inst, gjid)
            except EvolutionError as ex:
                self.record(owner, gjid, "group", label, risk, 0.0, risk, "group_leave_failed", res["reasons"])
                await self.notify(owner, f"❌ WhatsApp: не удалось выйти из группы {name}: {ex}")
                return None
            self.leave_times.setdefault(owner, deque()).append(time.time())
            action = "group_left"
            text = f"🚪 WhatsApp: бот вышел из группы {name}: она про «{reason}» (риск {risk:.0%}).\nПочему: {why}."
            markup = InlineKeyboardMarkup(inline_keyboard=[[trust_btn]])
        self.record(owner, gjid, "group", label, risk, 0.0, risk, action, res["reasons"])
        await self.notify(owner, text, markup)
        return {"action": action}

    # ------------------------------------------------------------------ кнопки
    async def cb_trust(self, owner_id: int, ref: int, group: bool = False) -> bool:
        jid = self.ref_jid(owner_id, ref)
        if not jid:
            return False
        self.trust(owner_id, jid)
        if group:
            self._mark_group(owner_id, jid, "trusted", 0.0)
        self.db.execute(
            "UPDATE wa_events SET action='false_positive' WHERE id=(SELECT MAX(id) FROM wa_events WHERE owner_id=? AND jid=? AND action IN ('warned','would_block','blocked','group_warned','group_would_leave','group_left'))",
            (owner_id, jid),
        )
        self.db.commit()
        return True

    async def cb_leave(self, owner_id: int, ref: int) -> str:
        jid, row = self.ref_jid(owner_id, ref), self.row(owner_id)
        if not jid or not row:
            return "Не найдено"
        try:
            await self.evo.leave_group(row["instance"], jid)
            return "Вышел"
        except EvolutionError as ex:
            return f"Не удалось: {str(ex)[:80]}"

    # ------------------------------------------------------------------ уборщик
    async def janitor_once(self) -> dict:
        """Сверяет Evolution с нашей базой. Возвращает счётчики (для логов и тестов)."""
        out = {"orphans": 0, "expired": 0, "stale": 0, "marked_open": 0}
        try:
            live = {i["name"]: i for i in await self.evo.instances() if str(i.get("name", "")).startswith("sg_")}
        except EvolutionError as ex:
            log.warning("janitor: Evolution недоступен (%s)", ex)
            return out
        now = time.time()
        rows = {r[0]: r for r in self.db.execute("SELECT instance, owner_id, status, created_at, closed_at FROM wa_instances").fetchall()}
        for name in live:
            if name not in rows:  # сирота: нет у нас в базе
                try:
                    await self.evo.delete(name)
                    out["orphans"] += 1
                except EvolutionError:
                    pass
        for name, (_, owner, status, created, closed) in rows.items():
            inst = live.get(name)
            state = (inst or {}).get("connectionStatus")
            if status == "pending":
                if state == "open":  # пропустили вебхук: считаем подключённым
                    row = self.row(owner)
                    await self._on_connection(row, {"state": "open"})
                    out["marked_open"] += 1
                elif now - created > self.pair_ttl or inst is None:
                    await self._drop(owner, "QR не отсканирован вовремя")
                    await self.notify(owner, "⌛ QR WhatsApp не отсканирован вовремя, подключение удалено. Нужно подключить снова: /whatsapp")
                    out["expired"] += 1
            elif status == "connected":
                if inst is None:
                    await self._drop(owner, "экземпляр пропал в Evolution")
                    await self.notify(owner, "⚠️ Подключение WhatsApp потеряно. Подключить снова: /whatsapp")
                    out["stale"] += 1
                elif state == "open":
                    if closed:
                        self.db.execute("UPDATE wa_instances SET closed_at=NULL WHERE owner_id=?", (owner,))
                        self.db.commit()
                else:
                    if not closed:
                        self.db.execute("UPDATE wa_instances SET closed_at=? WHERE owner_id=?", (now, owner))
                        self.db.commit()
                    elif now - closed > self.closed_ttl:
                        await self._drop(owner, "нет связи слишком долго")
                        await self.notify(owner, "⚠️ WhatsApp долго без связи, подключение удалено. Подключить снова: /whatsapp")
                        out["stale"] += 1
        return out

    async def janitor_loop(self, interval: float = 60) -> None:
        while True:
            try:
                r = await self.janitor_once()
                if any(r.values()):
                    log.info("janitor: %s", r)
            except Exception:
                log.exception("janitor")
            await asyncio.sleep(interval)

    async def start_server(self, port: int = 8081) -> web.AppRunner:
        app = web.Application()
        app.router.add_post("/wa/{secret}", self.handle_webhook)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()  # порт внутри docker-сети, наружу не публикуется
        return runner
