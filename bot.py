"""Telegram Business-бот: проверяет сообщения от незнакомых чатов, предупреждает владельца и удаляет вредное.

Требуется: Telegram Premium у владельца. Настройки -> Telegram для бизнеса -> Чат-боты -> добавить бота,
дать права "читать сообщения" и "удалять сообщения". Бот сам заблокировать контакт НЕ может (ограничение Bot API),
для этого см. userbot.py.

Запуск: BOT_TOKEN=... python bot.py
"""
import asyncio
import logging
import os
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.types import BusinessConnection, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from classifier import SpamGuard

WARN_AT = 0.5    # показать предупреждение
DELETE_AT = 0.85  # + удалить сообщение
LABELS = {"gambling": "🎰 азартные игры / ставки", "scam": "⚠️ мошенничество", "spam": "📢 спам"}

bot = Bot(os.environ["BOT_TOKEN"])
dp = Dispatcher()
guard = SpamGuard()
db = sqlite3.connect("state.db")
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS conn(id TEXT PRIMARY KEY, owner_id INT, owner_chat INT);
    CREATE TABLE IF NOT EXISTS trusted(chat_id INT PRIMARY KEY);
    """
)


def is_trusted(chat_id: int) -> bool:
    return db.execute("SELECT 1 FROM trusted WHERE chat_id=?", (chat_id,)).fetchone() is not None


def trust(chat_id: int) -> None:
    db.execute("INSERT OR IGNORE INTO trusted VALUES (?)", (chat_id,))
    db.commit()


@dp.business_connection()
async def on_connection(c: BusinessConnection):
    db.execute("INSERT OR REPLACE INTO conn VALUES (?,?,?)", (c.id, c.user.id, c.user_chat_id))
    db.commit()


@dp.business_message()
async def on_business_message(m: Message):
    row = db.execute("SELECT owner_id, owner_chat FROM conn WHERE id=?", (m.business_connection_id,)).fetchone()
    if not row:
        return
    owner_id, owner_chat = row

    # Владелец сам написал в этот чат -> собеседник доверенный навсегда.
    if m.from_user and m.from_user.id == owner_id:
        trust(m.chat.id)
        return
    if is_trusted(m.chat.id):
        return

    text = m.text or m.caption
    if not text:
        return
    res = guard.predict(text)
    if res["risk"] < WARN_AT:
        return

    deleted = False
    if res["risk"] >= DELETE_AT:
        try:
            await bot.delete_business_messages(m.business_connection_id, [m.message_id])
            deleted = True
        except Exception as e:  # нет прав can_delete_all_messages и т.п.
            logging.warning("delete failed: %s", e)

    who = m.from_user.full_name if m.from_user else "?"
    uname = f" @{m.from_user.username}" if m.from_user and m.from_user.username else ""
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Это не спам, доверять", callback_data=f"trust:{m.chat.id}")]]
    )
    await bot.send_message(
        owner_chat,
        f"🛡 Подозрительное сообщение от {who}{uname}\n"
        f"Тип: {LABELS.get(res['label'], res['label'])} · риск {res['risk']:.0%}\n"
        f"{'Сообщение удалено. ' if deleted else ''}"
        f"Рекомендую заблокировать контакт.\n\n«{text[:200]}»",
        reply_markup=kb,
    )


@dp.callback_query(F.data.startswith("trust:"))
async def on_trust(q: CallbackQuery):
    trust(int(q.data.split(":")[1]))
    await q.answer("Добавлено в доверенные")
    await q.message.edit_reply_markup(reply_markup=None)


async def main():
    logging.basicConfig(level=logging.INFO)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
