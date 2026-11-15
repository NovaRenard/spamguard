"""Интеграционная проверка на настоящем Evolution (запуск внутри контейнера бота):
создание экземпляра, QR, вебхук, повторное подключение, уборка просроченных/осиротевших, чужие экземпляры.
Использует временную базу в памяти и тестовые id (999001...), ваши данные не трогает."""
import asyncio
import os
import sqlite3
import sys

sys.path.insert(0, "/app")
from classifier import SpamGuard  # noqa: E402
from whatsapp import Evolution, WhatsAppService  # noqa: E402


class Bot:
    def __init__(self):
        self.photos, self.sent = [], []

    async def send_photo(self, chat_id, photo, caption=None, **k):
        self.photos.append(len(photo.data) if hasattr(photo, "data") else 0)
        from types import SimpleNamespace
        return SimpleNamespace(message_id=len(self.photos))

    async def send_message(self, chat_id, text, reply_markup=None, **k):
        self.sent.append(text)

    async def edit_message_media(self, *a, **k):
        pass


async def main():
    bot = Bot()
    evo = Evolution(os.environ["EVOLUTION_URL"], os.environ["EVOLUTION_API_KEY"])
    wa = WhatsAppService(bot=bot, db=sqlite3.connect(":memory:", check_same_thread=False), guard=SpamGuard(), evo=evo,
                         webhook_base="http://spamguard:8081", secret="itest", pair_ttl=12)

    async def names():
        return sorted(i["name"] for i in await evo.instances())

    for n in await names():  # чистим остатки прошлых запусков
        if n.startswith(("sg_99900", "zz_")):
            await evo.delete(n)
    await asyncio.sleep(6)
    print("до теста:", await names())
    await wa.connect(999001)
    print("1) экземпляр создан:", "sg_999001" in await names(), "| QR отправлен, байт в картинке:", bot.photos)
    wh = await evo._req("GET", "/webhook/find/sg_999001")
    print("2) вебхук:", wh["url"].replace("itest", "***"), wh["events"])
    st = await evo.state("sg_999001")
    print("3) состояние в Evolution:", st, "| статус у нас:", wa.row(999001)["status"])
    await wa.connect(999001)
    print("4) повторный /whatsapp: QR отправлено", len(bot.photos), "раза, экземпляров sg_999001:", sum(n == "sg_999001" for n in await names()))

    for n in ("sg_999002", "zz_keep"):
        await evo._req("POST", "/instance/create", {"instanceName": n, "integration": "WHATSAPP-BAILEYS", "qrcode": False}, timeout=40)
    print("5) добавили сироту sg_999002 и чужой zz_keep:", await names())
    r = await wa.janitor_once()
    print("6) уборка сразу:", r, "->", await names())
    await asyncio.sleep(14)
    r = await wa.janitor_once()
    print("7) уборка после TTL (12с):", r, "->", await names())
    print("   пользователю отправлено:", [s[:60] for s in bot.sent])
    await evo.delete("zz_keep")
    await asyncio.sleep(8)  # удаление в Evolution асинхронное: список обновляется с задержкой
    left = await names()
    print("после теста (через 8с):", left)
    ok = left == [] and r["expired"] == 1
    await evo._session.close()
    print("\nРЕЗУЛЬТАТ:", "OK" if ok else "ЕСТЬ ПРОБЛЕМЫ")


asyncio.run(main())
