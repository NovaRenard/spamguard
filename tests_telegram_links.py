"""Тест ссылок в Telegram-части (Monitor.on_incoming) с имитацией Telethon:  python tests_telegram_links.py
Запускает service во временной папке с тестовыми переменными, ваша state.db не затрагивается."""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace as NS

os.chdir(tempfile.mkdtemp())
from cryptography.fernet import Fernet  # noqa: E402

os.environ.update(BOT_TOKEN="123456:TESTTOKENTESTTOKENTESTTOKENTESTTOKE", API_ID="1", API_HASH="x", SESSION_KEY=Fernet.generate_key().decode(),
                  ADMIN_IDS="1", LINKS_RDAP="0")
sys.path.insert(0, os.environ.get("SRC", "."))

import service as s  # noqa: E402
import senderprofile  # noqa: E402

senderprofile.functions = NS(users=NS(GetFullUserRequest=lambda x: x))
FAILS = []


def check(name, cond, extra=""):
    print(f"  {'✔' if cond else '✘ ПРОВАЛ'} {name} {extra}")
    if not cond:
        FAILS.append(name)


s.linkchk.sets = {"gambling": {"1win-bonus.com"}, "scam": {"evil-phish.net"}}
s.linkchk.expand = lambda url, hops=3: asyncio.sleep(0, result=url)


class Cl:
    def add_event_handler(self, *a, **k): pass
    async def __call__(self, req): return NS(full_user=NS(about="", common_chats_count=1))
    async def get_messages(self, *a, **k): return []


class Msg:
    def __init__(self, hidden): self.hidden = hidden
    def get_entities_text(self): return [(NS(url=u), "тут") for u in self.hidden]


class Ev:
    out, is_group, is_channel, is_private = False, False, False, True
    def __init__(self, text, uid, hidden=()):
        self.raw_text, self.sender_id, self.chat_id, self.message = text, uid, uid, Msg(hidden)
    async def get_sender(self):
        return NS(first_name="Анна", username=None, contact=False, bot=False, verified=False, support=False, mutual_contact=False,
                  scam=False, fake=False, photo=object(), id=self.sender_id)
    async def get_input_sender(self): return None
    async def get_input_chat(self): return None
    async def respond(self, *a, **k): pass


out = []
async def fake_send(chat_id, text, reply_markup=None, **k): out.append(text)
s.bot.send_message = fake_send


async def main():
    mon = s.Monitor(1, Cl(), 111, dry_run=True)
    print("\n[Telegram: ссылки]")
    await mon.on_incoming(Ev("Привет! Гляди, интересно: https://example.com/page", 501))
    check("чужой + безобидная ссылка: тишина", out == [])

    await mon.on_incoming(Ev("Привет, посмотри сюда", 502, hidden=["https://1win-bonus.com/reg"]))
    check("скрытая в разметке ссылка на казино ловится у чужого", len(out) == 1 and "Ссылки:" in out[0] and "казино" in out[0], out[-1][:100] if out else "")

    out.clear(); mon.trust(503)
    await mon.on_incoming(Ev("Глянь фото, лови", 503, hidden=["https://evil-phish.net/x"]))
    check("ЗНАКОМЫЙ прислал фишинг в разметке: предупреждение о взломе",
          len(out) == 1 and "Знакомый" in out[0] and "взломали" in out[0], out[-1][:90] if out else "")

    out.clear()
    await mon.on_incoming(Ev("ещё раз https://evil-phish.net/y", 503))
    check("повтор от знакомого в течение 10 минут не дублируется", out == [])

    mon.trust(504)
    await mon.on_incoming(Ev("вот документ https://example.com/doc", 504))
    check("знакомый + безобидная ссылка: тишина", out == [])

    await mon.on_incoming(Ev("просто привет, как дела?", 505))
    check("обычное сообщение без ссылок: тишина и без лишней нагрузки", out == [])

    n = s.db.execute("SELECT COUNT(*) FROM events WHERE action='known_link'").fetchone()[0]
    check("событие known_link записано в статистику", n == 1, f"({n})")
    print("\n" + ("ВСЕ ТЕСТЫ ПРОШЛИ" if not FAILS else f"ПРОВАЛЕНО: {len(FAILS)} -> {FAILS}"))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
