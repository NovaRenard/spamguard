"""Тесты WhatsApp-модуля с имитацией Evolution и Telegram:  python tests_whatsapp.py"""
import asyncio
import base64
import sqlite3
import sys
import time
from types import SimpleNamespace as NS

from classifier import SpamGuard
from whatsapp import Evolution, EvolutionError, WhatsAppService

PNG = "data:image/png;base64," + base64.b64encode(b"png").decode()
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    print(f"  {'✔' if cond else '✘ ПРОВАЛ'} {name} {extra}")
    if not cond:
        FAILS.append(name)


class FakeBot:
    def __init__(self):
        self.sent, self.photos, self.edits = [], [], []

    async def send_message(self, chat_id, text, reply_markup=None, **k):
        self.sent.append((chat_id, text, [b.callback_data for r in (reply_markup.inline_keyboard if reply_markup else []) for b in r]))

    async def send_photo(self, chat_id, photo, caption=None, **k):
        self.photos.append((chat_id, caption))
        return NS(message_id=len(self.photos) + 100)

    async def edit_message_media(self, chat_id, message_id, media, **k):
        self.edits.append((chat_id, message_id))


class FakeEvo(Evolution):
    def __init__(self):
        self.inst: dict[str, str] = {}  # name -> connectionStatus
        self.msgs: dict[str, list] = {}
        self.groups: dict[str, dict] = {}
        self.deleted, self.blocked, self.left, self.webhooks, self.logged_out = [], [], [], [], []
        self.fail_find = False

    async def create(self, name):
        self.inst[name] = "connecting"
        return {"instance": {"instanceName": name}, "qrcode": {"base64": PNG, "count": 1}}

    async def set_webhook(self, name, url):
        self.webhooks.append((name, url))

    async def connect(self, name, number=None):
        return {"base64": PNG, "count": 2}

    async def instances(self):
        return [{"name": n, "connectionStatus": s, "ownerJid": "77000000000@s.whatsapp.net"} for n, s in self.inst.items()]

    async def delete(self, name):
        if name in self.inst:
            del self.inst[name]
        self.deleted.append(name)

    async def logout(self, name):
        self.logged_out.append(name)

    async def find_messages(self, name, jid, limit=30):
        if self.fail_find:
            raise EvolutionError("timeout")
        return self.msgs.get(jid, [])

    async def group_info(self, name, gjid):
        if gjid not in self.groups:
            raise EvolutionError("404")
        return self.groups[gjid]

    async def leave_group(self, name, gjid):
        self.left.append(gjid)

    async def block(self, name, jid):
        self.blocked.append(jid)

    async def profile(self, name, number):
        return {"status": {"status": ""}, "picture": "https://pic"}


def make():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    bot, evo = FakeBot(), FakeEvo()
    wa = WhatsAppService(bot=bot, db=db, guard=SpamGuard(), evo=evo, webhook_base="http://spamguard:8081", secret="sekret", pair_ttl=300, closed_ttl=3600)
    return wa, bot, evo, db


def add_connected(db, owner=1, live=0, connected_ago=3600):
    now = time.time()
    db.execute("INSERT INTO wa_instances(owner_id, instance, status, created_at, connected_at, wa_live) VALUES (?,?,?,?,?,?)",
               (owner, f"sg_{owner}", "connected", now - connected_ago, now - connected_ago, live))
    db.commit()


def msg(jid, text, mid, from_me=False, push="Аноним", ts=None):
    return {"key": {"remoteJid": jid, "fromMe": from_me, "id": mid}, "pushName": push,
            "message": {"conversation": text}, "messageTimestamp": int(ts or time.time())}


SPAM = "Регистрируйся в 1win и получи 100 фриспинов t.me/win_bonus, бонус 500% на депозит"


async def test_messages():
    print("\n[Личные чаты]")
    wa, bot, evo, db = make(); add_connected(db)
    row = wa.row(1)
    await wa.on_message(row, msg("7701@s.whatsapp.net", SPAM, "A1", push="Vavada Casino"))
    check("чужой спам, режим уведомлений -> «был бы заблокирован» + кнопка", len(bot.sent) == 1 and "был бы заблокирован" in bot.sent[0][1] and bot.sent[0][2][0].startswith("wtrust:"), str(bot.sent[-1:])[:90])
    check("тексты в уведомлении обезврежены (нет кликабельных ссылок)", "t.me/" not in bot.sent[0][1] and "hxxp" not in bot.sent[0][1] or "t[.]me" in bot.sent[0][1])
    check("ничего не заблокировано в режиме уведомлений", evo.blocked == [])

    bot.sent.clear(); await wa.on_message(row, msg("7701@s.whatsapp.net", SPAM, "A1", push="Vavada Casino"))
    check("повтор вебхука (тот же id) не дублирует уведомление", bot.sent == [])

    wa.set_live(1, True); row = wa.row(1)
    await wa.on_message(row, msg("7702@s.whatsapp.net", SPAM, "A2", push="Vavada Casino"))
    check("боевой режим: блокировка вызвана", evo.blocked == ["7702@s.whatsapp.net"])
    check("боевой режим: уведомление «заблокирован»", any("заблокирован" in s[1] and "был бы" not in s[1] for s in bot.sent))

    bot.sent.clear()
    await wa.on_message(row, msg("7703@s.whatsapp.net", "Привет! Ты завтра придёшь на встречу в 6?", "A3", push="Иван"))
    check("обычное сообщение от чужого: тишина", bot.sent == [])

    evo.msgs["7704@s.whatsapp.net"] = [msg("7704@s.whatsapp.net", "Привет", "H1", from_me=True)]
    await wa.on_message(row, msg("7704@s.whatsapp.net", SPAM, "A4"))
    check("чат, куда вы писали: не трогаем даже спам", bot.sent == [])

    evo.msgs["7705@s.whatsapp.net"] = [msg("7705@s.whatsapp.net", "старая переписка", "H2", ts=time.time() - 7200)]
    await wa.on_message(row, msg("7705@s.whatsapp.net", SPAM, "A5"))
    check("переписка была до подключения: не трогаем", bot.sent == [])

    await wa.on_message(row, msg("7706@s.whatsapp.net", SPAM, "A6", ts=time.time() - 3600))
    check("сообщение старше 10 минут пропускаем", bot.sent == [])

    await wa.on_message(row, msg("7707@s.whatsapp.net", "ответил сам", "A7", from_me=True))
    await wa.on_message(row, msg("7707@s.whatsapp.net", SPAM, "A8"))
    check("после вашего ответа собеседник доверенный", bot.sent == [])

    evo.fail_find = True
    await wa.on_message(row, msg("7708@s.whatsapp.net", SPAM, "A9"))
    check("Evolution не ответил: не поднимаем ложную тревогу", bot.sent == [])
    evo.fail_find = False

    await wa.on_message(row, msg("status@broadcast", SPAM, "A10"))
    await wa.on_message(row, msg("1203@newsletter", SPAM, "A11"))
    check("статусы и каналы игнорируются", bot.sent == [])

    ref = wa.ref(1, "7702@s.whatsapp.net")
    check("кнопка «не спам» доверяет и помечает ложное", await wa.cb_trust(1, ref) and wa.is_trusted(1, "7702@s.whatsapp.net"))
    check("кнопка чужого владельца не работает", not await wa.cb_trust(2, ref))
    check("статистика считает события", "WhatsApp: чатов проверено" in (wa.stats_text(1) or ""), wa.stats_text(1)[:70])


async def test_groups():
    print("\n[Группы]")
    wa, bot, evo, db = make(); add_connected(db)
    row = wa.row(1); wa.my_jid[1] = "77000000000@s.whatsapp.net"
    gamb = [msg("g1@g.us", t, f"G{i}") for i, t in enumerate([
        "Регистрируйся в 1win и получи 100 фриспинов", "Занос 400к в слотах, смотри как играю",
        "Промокод WIN500 на депозит, бонус 500%", "Ставки на спорт, прогноз на матч, проход 90%"])]
    evo.groups["g1@g.us"] = {"subject": "Казино Вавада | Бонусы", "desc": "бонусы казино", "owner": "777@s.whatsapp.net", "participants": []}
    evo.msgs["g1@g.us"] = gamb
    r = await wa.check_group(row, "g1@g.us")
    check("гемблинг-группа, режим уведомлений -> «вышел бы»", r["action"] == "group_would_leave" and evo.left == [] and "бы вышел" in bot.sent[-1][1], bot.sent[-1][1][:60])
    check("кнопки: выйти и доверять", {c.split(":")[0] for c in bot.sent[-1][2]} == {"wleave", "wgtrust"})

    wa.set_live(1, True); row = wa.row(1)
    evo.groups["g2@g.us"] = dict(evo.groups["g1@g.us"]); evo.msgs["g2@g.us"] = gamb
    r = await wa.check_group(row, "g2@g.us")
    check("боевой режим: вышел из группы", r["action"] == "group_left" and evo.left == ["g2@g.us"])
    check("уведомление «бот вышел из группы»", "бот вышел из группы" in bot.sent[-1][1] and "азартные игры" in bot.sent[-1][1])

    bot.sent.clear()
    evo.groups["g3@g.us"] = {"subject": "Семья", "desc": "", "owner": "888@s.whatsapp.net", "participants": []}
    evo.msgs["g3@g.us"] = [msg("g3@g.us", t, f"F{i}") for i, t in enumerate(["Мам, я скоро буду, не волнуйся", "Купи хлеб и молоко по дороге домой", "Завтра едем к бабушке в 10 утра"])]
    r = await wa.check_group(row, "g3@g.us")
    check("семейная группа: тишина", r["action"] == "group_ok" and bot.sent == [])

    evo.groups["g4@g.us"] = {"subject": "Казино Вавада", "desc": "казино", "owner": "777@s.whatsapp.net",
                             "participants": [{"id": "77000000000@s.whatsapp.net", "admin": "admin"}]}
    evo.msgs["g4@g.us"] = gamb
    r = await wa.check_group(row, "g4@g.us")
    check("группа, где вы админ: не трогаем", r is None and wa.is_trusted(1, "g4@g.us") and "g4@g.us" not in evo.left)

    evo.groups["g5@g.us"] = dict(evo.groups["g1@g.us"]); evo.msgs["g5@g.us"] = gamb + [msg("g5@g.us", "я писал тут", "M", from_me=True)]
    check("группа, где вы писали: не трогаем", await wa.check_group(row, "g5@g.us") is None)

    check("нет данных о группе: пропускаем без отметки", await wa.check_group(row, "nope@g.us") is None
          and db.execute("SELECT 1 FROM wa_checks WHERE jid='nope@g.us'").fetchone() is None)

    evo.groups["g6@g.us"] = {"subject": "Казино: новости регулирования", "desc": "законы рынка", "owner": "1@s.whatsapp.net", "participants": []}
    evo.msgs["g6@g.us"] = [msg("g6@g.us", "Минфин опубликовал проект поправок о налогообложении игорного бизнеса", "N1")]
    r = await wa.check_group(row, "g6@g.us")
    check("новости про казино: предупреждение, без выхода", r["action"] == "group_warned" and "g6@g.us" not in evo.left)

    check("проверка группы запоминается (не чаще раза в 7 дней)", db.execute("SELECT 1 FROM wa_checks WHERE jid='g1@g.us'").fetchone() is not None)
    n = len(bot.sent); wa.schedule_group_check(row, "g1@g.us"); await asyncio.sleep(0.05)
    check("повторная проверка не запускается", len(bot.sent) == n and ("1", "g1@g.us") not in wa.group_busy)

    # вас добавили в группу
    evo.groups["g7@g.us"] = dict(evo.groups["g1@g.us"]); evo.msgs["g7@g.us"] = gamb
    await wa._on_participants(row, {"id": "g7@g.us", "action": "add", "participants": ["77000000000@s.whatsapp.net"]})
    await asyncio.sleep(1.8)
    check("добавление в группу запускает проверку", db.execute("SELECT 1 FROM wa_checks WHERE jid='g7@g.us'").fetchone() is not None)


async def test_connect_and_janitor():
    print("\n[Подключение, QR, уборка]")
    wa, bot, evo, db = make()
    await wa.connect(1)
    check("экземпляр создан, вебхук задан, QR отправлен", "sg_1" in evo.inst and evo.webhooks == [("sg_1", "http://spamguard:8081/wa/sekret")] and len(bot.photos) == 1)
    check("статус pending", wa.row(1)["status"] == "pending")
    await wa.connect(1)
    check("повторный /whatsapp: новый QR, экземпляр не дублируется", len(evo.inst) == 1 and len(bot.photos) == 2)

    await wa._dispatch({"event": "qrcode.updated", "instance": "sg_1", "data": {"qrcode": {"base64": PNG}}})
    check("qrcode.updated обновляет сообщение с QR", len(bot.edits) == 1)

    # уборка: просроченный pending
    db.execute("UPDATE wa_instances SET created_at=? WHERE owner_id=1", (time.time() - 400,)); db.commit()
    bot.sent.clear(); r = await wa.janitor_once()
    check("не подключён за TTL: экземпляр удалён", r["expired"] == 1 and "sg_1" not in evo.inst and wa.row(1) is None)
    check("пользователь предупреждён об удалении", any("не отсканирован" in s[1] for s in bot.sent))

    # сирота и чужие экземпляры
    evo.inst["sg_555"] = "connecting"; evo.inst["other_app"] = "open"
    r = await wa.janitor_once()
    check("сирота (нет в нашей базе) удалена", r["orphans"] == 1 and "sg_555" not in evo.inst)
    check("чужие экземпляры (не sg_) не тронуты", "other_app" in evo.inst)

    # свежий pending не трогаем
    await wa.connect(2); bot.sent.clear()
    r = await wa.janitor_once()
    check("свежий pending не удаляется", "sg_2" in evo.inst and r["expired"] == 0)

    # pending, но Evolution уже open (вебхук потерян)
    evo.inst["sg_2"] = "open"; r = await wa.janitor_once()
    check("потерян вебхук open: считаем подключённым", r["marked_open"] == 1 and wa.row(2)["status"] == "connected")
    check("пользователь получил «WhatsApp подключён»", any("подключён" in s[1] and "Анализ запущен" in s[1] for s in bot.sent))

    # connected, нет связи долго
    evo.inst["sg_2"] = "close"; await wa.janitor_once()
    check("нет связи: пока только отмечаем", wa.row(2) is not None and wa.row(2)["closed_at"])
    db.execute("UPDATE wa_instances SET closed_at=? WHERE owner_id=2", (time.time() - 4000,)); db.commit()
    r = await wa.janitor_once()
    check("нет связи дольше часа: удалён", wa.row(2) is None and "sg_2" not in evo.inst and r["stale"] == 1)

    # connected, экземпляр пропал в Evolution
    add_connected(db, owner=3); r = await wa.janitor_once()
    check("подключённый экземпляр пропал в Evolution: запись удалена", wa.row(3) is None and r["stale"] == 1)

    # connection.update
    await wa.connect(4); bot.sent.clear()
    await wa._dispatch({"event": "connection.update", "instance": "sg_4", "data": {"state": "open"}})
    check("connection.update open -> подключено", wa.row(4)["status"] == "connected" and wa.row(4)["phone"] == "77000000000")
    await wa._dispatch({"event": "connection.update", "instance": "sg_4", "data": {"state": "close", "statusReason": 401}})
    check("устройство удалено в телефоне (401): экземпляр удалён", wa.row(4) is None and "sg_4" not in evo.inst and any("отключён" in s[1] for s in bot.sent))

    # /wastop и лимиты
    await wa.connect(5); wa.trust(5, "x@s.whatsapp.net"); evo.deleted.clear()
    check("/wastop: выход, удаление экземпляра и данных", await wa.stop(5) and evo.logged_out == ["sg_5"] and "sg_5" in evo.deleted and not wa.is_trusted(5, "x@s.whatsapp.net"))
    wa.max_instances = 1; await wa.connect(6); bot.sent.clear(); await wa.connect(7)
    check("лимит экземпляров: сверх лимита не создаём", "sg_7" not in evo.inst and any("слишком много" in s[1] for s in bot.sent))

    # безопасность вебхука
    class Req:
        def __init__(self, secret, body): self.match_info = {"secret": secret}; self._b = body
        async def json(self): return self._b
    ok = await wa.handle_webhook(Req("sekret", {"event": "x"}))
    bad = await wa.handle_webhook(Req("wrong", {"event": "x"}))
    check("вебхук с неверным секретом отклонён (404)", bad.status == 404 and ok.status == 200)


async def test_links():
    print("\n[Ссылки]")
    from links import LinkChecker

    class FakeLinks(LinkChecker):
        def __init__(self):
            super().__init__(rdap=False)
            self.sets = {"gambling": {"1win-bonus.com"}, "scam": {"evil-phish.net"}}

        async def expand(self, url, hops=3):
            return url

    wa, bot, evo, db = make(); add_connected(db)
    wa.links = FakeLinks(); row = wa.row(1)

    await wa.on_message(row, msg("7801@s.whatsapp.net", "Привет, глянь тут https://example.com/page", "L1"))
    check("чужой + безобидная ссылка: тишина", bot.sent == [])

    await wa.on_message(row, msg("7802@s.whatsapp.net", "Привет! Посмотри, тут интересно: 1win-bonus.com/reg", "L2"))
    check("чужой + «чистый» текст, но ссылка на казино: предупреждение со ссылками",
          len(bot.sent) == 1 and "Ссылки:" in bot.sent[0][1] and "казино" in bot.sent[0][1], bot.sent[-1][1][:110] if bot.sent else "")

    bot.sent.clear(); wa.trust(1, "7803@s.whatsapp.net")
    await wa.on_message(row, msg("7803@s.whatsapp.net", "Глянь фотки https://evil-phish.net/photos", "L3", push="Друг"))
    check("ЗНАКОМЫЙ прислал фишинг: предупреждение «возможно, взломали»", len(bot.sent) == 1 and "знакомый" in bot.sent[0][1] and "взломали" in bot.sent[0][1], bot.sent[-1][1][:80] if bot.sent else "")
    check("знакомого не блокируем и кнопок «доверять» нет", evo.blocked == [] and bot.sent[0][2] == [])

    bot.sent.clear()
    await wa.on_message(row, msg("7803@s.whatsapp.net", "ещё раз https://evil-phish.net/photos2", "L4", push="Друг"))
    check("повтор от того же знакомого в течение 10 минут не дублируется", bot.sent == [])

    await wa.on_message(row, msg("7804@s.whatsapp.net", "ответ", "L5", from_me=True))
    await wa.on_message(row, msg("7804@s.whatsapp.net", "Смотри https://example.com/ok", "L6", push="Коллега"))
    check("знакомый + безобидная ссылка: тишина", bot.sent == [])

    wa.set_live(1, True); row = wa.row(1); evo.blocked.clear(); bot.sent.clear()
    wa.trust(1, "7805@s.whatsapp.net")
    await wa.on_message(row, msg("7805@s.whatsapp.net", "https://1win-bonus.com/x", "L7", push="Друг2"))
    check("даже в боевом режиме знакомого не блокируем", evo.blocked == [] and len(bot.sent) == 1)
    check("статистика: ссылки знакомых не считаются проверкой чужих (только 2 чужих чата)", "чатов проверено 2" in (wa.stats_text(1) or ""), (wa.stats_text(1) or "")[:60])


async def main():
    await test_messages()
    await test_links()
    await test_groups()
    await test_connect_and_janitor()
    print("\n" + ("ВСЕ ТЕСТЫ ПРОШЛИ" if not FAILS else f"ПРОВАЛЕНО: {len(FAILS)} -> {FAILS}"))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
