"""Защита аккаунта Telegram от незнакомых спамеров/казино/мошенников (Telethon, работает от вашего аккаунта).

Логика:
  1. Входящее сообщение от НОВОГО собеседника (не контакт, вы ему никогда не писали) -> классификатор.
  2. риск >= BLOCK_AT  -> жалоба на спам + блокировка + удаление чата + уведомление «заблокирован, потому что ...»
     риск >= WARN_AT   -> только уведомление, без блокировки
  3. Уведомление приходит в «Избранное»; если задан NOTIFY_BOT_TOKEN — ещё и от вашего бота (со звуком/пушем).
  4. Ошибка? В «Избранное» отправьте  /trust <id>  — собеседник разблокируется и больше не проверяется.

Настройки (переменные окружения):
  API_ID, API_HASH     обязательно (my.telegram.org)
  DRY_RUN=1            по умолчанию ТОЛЬКО уведомляет, ничего не блокирует. Для боевого режима: DRY_RUN=0
  BLOCK_AT=0.85  WARN_AT=0.5  MAX_BLOCKS_PER_HOUR=5
  CHECK_GROUPS=0       1 = проверять и группы, где вы ни разу не писали (при высоком риске — выйти из группы)
  NOTIFY_BOT_TOKEN     токен бота для пуш-уведомлений (вы должны один раз нажать /start у этого бота)
  SESSION=userbot      имя файла сессии (userbot.session = полный доступ к аккаунту, не храните в git!)
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import deque

from telethon import TelegramClient, events, functions

from classifier import SpamGuard

log = logging.getLogger("userbot")

DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"
WARN_AT = float(os.environ.get("WARN_AT", 0.5))
BLOCK_AT = float(os.environ.get("BLOCK_AT", 0.85))
MAX_BLOCKS_PER_HOUR = int(os.environ.get("MAX_BLOCKS_PER_HOUR", 5))
CHECK_GROUPS = os.environ.get("CHECK_GROUPS", "0") == "1"
NOTIFY_BOT_TOKEN = os.environ.get("NOTIFY_BOT_TOKEN")

REASONS = {
    "gambling": "продвигает азартные игры / казино / ставки",
    "scam": "признаки мошенничества (быстрый заработок, фишинг, «вы выиграли»)",
    "spam": "рассылает спам / рекламу",
}

guard = SpamGuard()
db = sqlite3.connect("userbot_state.db")
db.execute("CREATE TABLE IF NOT EXISTS trusted(chat_id INT PRIMARY KEY)")
block_times: deque[float] = deque()


def is_trusted(chat_id: int) -> bool:
    return db.execute("SELECT 1 FROM trusted WHERE chat_id=?", (chat_id,)).fetchone() is not None


def trust(chat_id: int) -> None:
    db.execute("INSERT OR IGNORE INTO trusted VALUES (?)", (chat_id,))
    db.commit()


def defang(s: str) -> str:
    """Чтобы цитата спама в «Избранном» не превращалась в кликабельные ссылки."""
    return s.replace("http", "hxxp").replace("t.me/", "t[.]me/").replace("@", "＠")


def rate_limited() -> bool:
    now = time.time()
    while block_times and now - block_times[0] > 3600:
        block_times.popleft()
    return len(block_times) >= MAX_BLOCKS_PER_HOUR


def _bot_send(chat_id: int, text: str) -> None:
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage", data, timeout=10).read()


async def notify(client: TelegramClient, owner_id: int, text: str) -> None:
    await client.send_message("me", text, link_preview=False)
    if NOTIFY_BOT_TOKEN:
        try:
            await asyncio.to_thread(_bot_send, owner_id, text)
        except Exception as e:
            log.warning("bot notify failed: %s", e)


async def is_new_sender(client: TelegramClient, event, sender) -> bool:
    if is_trusted(event.chat_id):
        return False
    if event.is_private:
        if (
            getattr(sender, "bot", False)
            or getattr(sender, "verified", False)
            or getattr(sender, "support", False)
            or getattr(sender, "contact", False)
            or getattr(sender, "mutual_contact", False)
            or event.chat_id == 777000  # служебные уведомления Telegram
        ):
            trust(event.chat_id)
            return False
    # Если вы когда-либо писали в этот чат — он доверенный (проверяем один раз, дальше кэш).
    if await client.get_messages(event.chat_id, limit=1, from_user="me"):
        trust(event.chat_id)
        return False
    return True


async def punish(client: TelegramClient, event, sender, res: dict) -> list[str]:
    """Возвращает список реально выполненных действий."""
    done = []
    peer = await event.get_input_chat()
    if event.is_private:
        try:
            await client(functions.messages.ReportSpamRequest(peer=peer))
            done.append("жалоба на спам отправлена")
        except Exception as e:
            log.warning("report failed: %s", e)
        await client(functions.contacts.BlockRequest(id=await event.get_input_sender()))
        done.append("контакт заблокирован")
        try:
            await client.delete_dialog(event.chat_id)
            done.append("чат удалён")
        except Exception as e:
            log.warning("delete_dialog failed: %s", e)
    else:
        await client.delete_dialog(event.chat_id)  # для группы это «выйти из группы»
        done.append("вышел из группы")
    return done


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    client = TelegramClient(os.environ.get("SESSION", "userbot"), int(os.environ["API_ID"]), os.environ["API_HASH"])
    await client.start()
    me = await client.get_me()

    @client.on(events.NewMessage(outgoing=True))
    async def on_outgoing(e):
        if e.chat_id != me.id:
            trust(e.chat_id)  # вы написали — собеседник доверенный

    @client.on(events.NewMessage(chats="me", pattern=r"^/trust (-?\d+)$"))
    async def on_trust(e):
        chat_id = int(e.pattern_match.group(1))
        trust(chat_id)
        try:
            await client(functions.contacts.UnblockRequest(id=await client.get_input_entity(chat_id)))
            await e.reply("✅ Добавлен в доверенные и разблокирован")
        except Exception as ex:
            await e.reply(f"✅ Добавлен в доверенные. Разблокировать не удалось ({ex}) — сделайте вручную")

    @client.on(events.NewMessage(incoming=True))
    async def on_incoming(e):
        text = e.raw_text
        if not text or e.out:
            return
        if not (e.is_private or (e.is_group and CHECK_GROUPS)):
            return
        sender = await e.get_sender()
        if not await is_new_sender(client, e, sender):
            return

        res = guard.predict(text)
        if res["risk"] < WARN_AT:
            return

        name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "?"
        uname = f" @{sender.username}" if getattr(sender, "username", None) else ""
        reason = REASONS.get(res["label"], res["label"])
        head = f"{name}{defang(uname)} (id {e.sender_id})"
        quote = f"«{defang(text[:200])}»"

        if res["risk"] < BLOCK_AT:
            await notify(client, me.id, f"⚠️ Подозрительное сообщение от {head}: {reason}, риск {res['risk']:.0%}.\n{quote}\nНе заблокирован.")
            return
        if DRY_RUN or rate_limited():
            why = "режим DRY_RUN" if DRY_RUN else "лимит блокировок в час"
            await notify(client, me.id, f"🛡 Был бы заблокирован ({why}): {head} — {reason}, риск {res['risk']:.0%}.\n{quote}")
            return

        block_times.append(time.time())
        try:
            done = await punish(client, e, sender, res)
        except Exception as ex:
            await notify(client, me.id, f"❌ Не удалось заблокировать {head}: {ex}\n{quote}")
            return
        await notify(
            client,
            me.id,
            f"🛡 Telegram-защита: {head} заблокирован — {reason} (риск {res['risk']:.0%}).\n"
            f"Сделано: {', '.join(done)}.\n{quote}\n"
            f"Ошибка? Отправьте сюда: /trust {e.chat_id}",
        )

    log.info("started, DRY_RUN=%s BLOCK_AT=%s", DRY_RUN, BLOCK_AT)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
