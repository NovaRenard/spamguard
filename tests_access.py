"""Тесты открытого доступа, скрытия азартной рекламы и дообучения по кнопке:  python tests_access.py
Запускает service во временной папке, ваша state.db не затрагивается."""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace as NS

os.chdir(tempfile.mkdtemp())
from cryptography.fernet import Fernet  # noqa: E402

os.environ.update(BOT_TOKEN="123456:TESTTOKENTESTTOKENTESTTOKENTESTTOKE", API_ID="1", API_HASH="x", SESSION_KEY=Fernet.generate_key().decode(),
                  ADMIN_IDS="1", LINKS_RDAP="0", LINKS_ENABLED="0")
sys.path.insert(0, os.environ.get("SRC", "."))

import service as s  # noqa: E402
import senderprofile  # noqa: E402

senderprofile.functions = NS(users=NS(GetFullUserRequest=lambda x: x))
s.guard.path = __import__("pathlib").Path(os.getcwd()) / "model.joblib"  # дообучение не должно трогать настоящую модель
FAILS = []


def check(name, cond, extra=""):
    print(f"  {'✔' if cond else '✘ ПРОВАЛ'} {name} {extra}")
    if not cond:
        FAILS.append(name)


def user(uid, name, username=None):
    return NS(id=uid, full_name=name, username=username)


print("\n[Открытый доступ: одобрение не нужно]")
check("незнакомец допущен без заявки", s.allowed(5551) and s.allowed(5552))
s.touch_user(user(5551, "Алия К.", "aliya"))
row = s.db.execute("SELECT status, name, username FROM access WHERE user_id=5551").fetchone()
check("пользователь записан в справочник: имя, @username, статус", row == ("approved", "Алия К.", "aliya"), str(row))
s.touch_user(user(5551, "Алия Кенжебаева", "aliya"))
check("повторный вход обновляет имя, а не дублирует", s.db.execute("SELECT COUNT(*), MAX(name) FROM access WHERE user_id=5551").fetchone() == (1, "Алия Кенжебаева"))
s.db.execute("UPDATE access SET status='revoked' WHERE user_id=5551"); s.db.commit()
check("закрытый администратором доступ остаётся закрытым (/revoke)", not s.allowed(5551))
s.touch_user(user(5551, "Алия", "aliya"))
check("и повторный /start его не возвращает", not s.allowed(5551) and s.db.execute("SELECT status FROM access WHERE user_id=5551").fetchone()[0] == "revoked")
check("администратор из .env всегда допущен", s.allowed(1))
s.OPEN_ACCESS = False
check("OPEN_ACCESS=0 возвращает режим заявок", not s.allowed(7777) and s.allowed(1))
s.OPEN_ACCESS = True

print("\n[Азартная реклама скрыта, спам показан, кнопка «не спам» дообучает]")
sent = []
async def fake_send(chat_id, text, reply_markup=None, **k): sent.append((text, reply_markup))
s.bot.send_message = fake_send


class Cl:
    def add_event_handler(self, *a, **k): pass
    async def __call__(self, req): return NS(full_user=NS(about="", common_chats_count=0))
    async def get_messages(self, *a, **k): return []


class Msg:
    def get_entities_text(self): return []


class Ev:
    out, is_group, is_channel, is_private = False, False, False, True
    def __init__(self, text, uid):
        self.raw_text, self.sender_id, self.chat_id, self.message = text, uid, uid, Msg()
    async def get_sender(self):
        return NS(first_name="Некто", username=None, contact=False, bot=False, verified=False, support=False, mutual_contact=False,
                  scam=False, fake=False, photo=None, id=self.sender_id)
    async def get_input_sender(self): return None
    async def get_input_chat(self): return None
    async def respond(self, *a, **k): pass


async def main():
    mon = s.Monitor(42, Cl(), 999, True)
    s.monitors[42] = mon
    casino = "🔥 КАЗИНО ВАВАДА! Бонус 100 фриспинов за регистрацию t.me/vavada_bonus"
    await mon.on_incoming(Ev(casino, 777))
    txt, kb = sent[-1]
    check("уведомление о казино отправлено", "Подозрительное" in txt or "заблокирован" in txt)
    check("текст рекламы в уведомлении СКРЫТ", "ВАВАДА" not in txt and "фриспин" not in txt.lower() and "скрыт" in txt, txt[:90])
    btns = [b.callback_data for row in kb.inline_keyboard for b in row]
    check("есть кнопки «не спам» и «показать текст»", any(b.startswith("trust:") for b in btns) and any(b.startswith("show:") for b in btns), str(btns))
    check("исходный текст держится только в памяти для кнопки", mon.flagged.get(777) == [casino])

    spam = "Накрутка подписчиков и просмотров дёшево, пиши t.me/boost_cheap_77"
    await mon.on_incoming(Ev(spam, 778))
    txt2, kb2 = sent[-1]
    check("обычный спам цитируется как раньше (не азартная реклама)", "Накрутка" in txt2 or "t[.]me" in txt2 or "boost" in txt2, txt2[:90])
    check("у обычного спама кнопки «показать» нет", not any(b.callback_data.startswith("show:") for row in kb2.inline_keyboard for b in row))

    # «Это не спам» на сообщении, которое бот ошибочно счёл подозрительным
    fp = "Вчера смотрели Казино Рояль, классный фильм! Заходи в гости, t.me/my_movie_club"
    r0 = s.guard.predict(fp, owner=42)
    await mon.on_incoming(Ev(fp, 779))
    flagged = r0["risk"] >= s.WARN_AT
    check("(подготовка) ложное срабатывание воспроизведено", flagged, str(r0["risk"]))
    answers = []
    class Q:
        data = "trust:779"
        from_user = NS(id=42)
        message = NS(edit_reply_markup=lambda **k: asyncio.sleep(0))
        async def answer(self, text="", **k): answers.append(text)
    mon.client = NS(**{"__call__": None})
    class C2(Cl):
        async def get_input_entity(self, cid): return None
        async def __call__(self, req): return None
    mon.client = C2()
    await s.on_trust(Q())
    check("кнопка «не спам»: бот отвечает, что учится", any("учусь" in a for a in answers), str(answers))
    check("текст сохранён в «памяти» дообучения", s.db.execute("SELECT COUNT(*) FROM feedback WHERE owner_id=42").fetchone()[0] == 1)
    check("такое сообщение у этого владельца больше не помечается", s.guard.predict(fp, owner=42)["risk"] == 0)
    check("чат стал доверенным", mon.is_trusted(779))
    await asyncio.sleep(0.05)

    # /strict
    msgs = []
    class M:
        from_user = NS(id=42)
        async def answer(self, t, **k): msgs.append(t)
    await s.cmd_strict(M())
    check("/strict: блокировка и автовыход включены", not mon.dry_run and mon.group_leave and s.db.execute("SELECT dry_run, group_leave FROM users").fetchone() in ((0, 1), None))
    check("/strict: ответ поддерживающий", any("не один" in t for t in msgs))

    # /stop стирает тексты
    s.feedback.forget(42)
    check("/stop: отмеченные тексты удаляются", s.db.execute("SELECT COUNT(*) FROM feedback WHERE owner_id=42").fetchone()[0] == 0)

asyncio.run(main())
print("\n" + ("ВСЕ ТЕСТЫ ПРОШЛИ" if not FAILS else f"ПРОВАЛЕНО: {len(FAILS)} -> {FAILS}"))
sys.exit(1 if FAILS else 0)
