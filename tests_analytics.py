"""Тесты аналитики:  python tests_analytics.py
Проверяют счётчики, ПРИВАТНОСТЬ снимка (нет реальных ID и текстов), срок хранения и подписанные токены.
Запускается во временной папке, ваша state.db не затрагивается."""
import json
import os
import subprocess
import sys
import tempfile
import time

os.chdir(tempfile.mkdtemp())
from cryptography.fernet import Fernet  # noqa: E402

os.environ.update(BOT_TOKEN="123456:TESTTOKENTESTTOKENTESTTOKENTESTTOKE", API_ID="1", API_HASH="x", SESSION_KEY=Fernet.generate_key().decode(),
                  ADMIN_IDS="7771110001", EVOLUTION_API_KEY="k", WA_WEBHOOK_SECRET="w", LINKS_RDAP="0")
SRC = os.environ.get("SRC", ".")
sys.path.insert(0, SRC)

import analytics  # noqa: E402
import service as s  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print(f"  {'✔' if cond else '✘ ПРОВАЛ'} {name} {extra}")
    if not cond:
        FAILS.append(name)


NOW = time.time()
DAY = 86400
A, B, C = 7771110001, 7771110002, 7771110003      # «реальные» владельцы (не должны попасть в снимок)
SEND1, SEND2 = 9990001111, 9990002222             # «реальные» ID собеседников
db = s.db


def ev(owner, days_ago, action, label="gambling", tr=0.0, pr=0.0, reasons="", domains="", sender=SEND1):
    db.execute("INSERT INTO events(owner_id, ts, chat_id, sender_id, label, text_risk, profile_risk, risk, action, reasons, domains) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (owner, NOW - days_ago * DAY - 60, sender, sender, label, tr, pr, max(tr, pr), action, reasons, domains))


def waev(owner, days_ago, kind, action, label="scam", jid="7701@s.whatsapp.net", domains="", tr=0.0, pr=0.0):
    db.execute("INSERT INTO wa_events(owner_id, ts, jid, kind, label, text_risk, profile_risk, risk, action, reasons, domains) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (owner, NOW - days_ago * DAY - 60, jid, kind, label, tr, pr, max(tr, pr), action, "", domains))


# --- владелец A (админ): Telegram
ev(A, 0, "ok", "ok"); ev(A, 0, "ok", "ok"); ev(A, 0, "warned", "gambling", tr=0.7, pr=0.1, reasons="нет фото; 3 сообщений за минуту", domains="1win-bonus.com")
ev(A, 1, "would_block", "scam", tr=0.9, pr=0.8, reasons="в «О себе»: gambling", domains="kaspi-secure.top", sender=SEND2)
ev(A, 2, "false_positive", "gambling", tr=0.6, reasons="", domains="")
ev(A, 3, "blocked", "gambling", tr=0.95, domains="1win-bonus.com")
ev(A, 3, "test", "gambling")                      # тестовые события не считаются
ev(A, 4, "known_link", "scam", tr=0.9, domains="evil-phish.net")
ev(A, 5, "group_left", "gambling", tr=0.9)
ev(A, 6, "group_warned", "scam", tr=0.6)
ev(A, 200, "blocked", "scam")                     # старше 90 дней: вне окна снимка
# --- владелец B: Telegram, тот же домен (волна)
ev(B, 0, "warned", "gambling", tr=0.8, domains="1win-bonus.com", sender=SEND1)
ev(B, 2, "ok", "ok")
# --- владелец C: только WhatsApp
waev(C, 0, "chat", "warned", "scam", domains="evil-phish.net", tr=0.8)
waev(C, 1, "chat", "ok", "ok")
waev(C, 2, "group", "group_would_leave", "gambling", jid="g1@g.us")
db.execute("INSERT INTO users(owner_id, session, dry_run) VALUES (?,?,?)", (A, b"x", 0))
db.execute("INSERT INTO users(owner_id, session, dry_run) VALUES (?,?,?)", (B, b"x", 1))
db.execute("INSERT INTO wa_instances(owner_id, instance, status, created_at, connected_at, wa_live) VALUES (?,?,?,?,?,?)", (C, "sg_c", "connected", NOW, NOW, 1))
db.execute("INSERT INTO access(user_id, status, name, username, ts) VALUES (?,?,?,?,?)", (B, "approved", "B", "", NOW))
db.execute("INSERT INTO access(user_id, status, name, username, ts) VALUES (?,?,?,?,?)", (999, "pending", "X", "", NOW))
db.commit()

KEY = "pseudo-test-key"
snap = analytics.build_snapshot(db, pseudo_key=KEY, tz_hours=5, now=NOW, extra={"lists_age_h": 3.5})
g = snap["global"]
pa, pb, pc = (analytics.pseudo(KEY, x) for x in (A, B, C))


def tot(scope, field, days=90):
    return sum(d[field] for d in scope["daily"][-days:])


print("\n[Счётчики]")
check("в снимке 90 дней по порядку, последний — сегодня", len(snap["days"]) == 90 and snap["days"] == sorted(snap["days"]))
check("проверено у всех: A=6, B=2, C=2 -> 10", tot(g, "checked") == 10, str(tot(g, "checked")))
ua, ub, uc = snap["users"][pa], snap["users"][pb], snap["users"][pc]
check("A: проверено 6 (ok×2, warned, would_block, fp, blocked), тесты/группы/знакомые не в счёте", tot(ua, "checked") == 6, str(tot(ua, "checked")))
check("A: помечено 4, из них заблокировано 1, «был бы заблокирован» 1, ложных 1", (tot(ua, "flagged"), tot(ua, "blocked"), tot(ua, "would_block"), tot(ua, "fp")) == (4, 1, 1, 1))
check("A: по типам gambling=3, scam=1", (tot(ua, "gambling"), tot(ua, "scam")) == (3, 1))
check("A: опасная ссылка от знакомого посчитана отдельно", tot(ua, "known_link") == 1)
check("A: событие старше 90 дней не попало", tot(ua, "blocked") == 1)
check("A: группа покинута 1", tot(ua, "group_left") == 1 and ua["groups"]["30"].get("group_warned") == 1)
check("B: проверено 2, помечено 1", (tot(ub, "checked"), tot(ub, "flagged")) == (2, 1))
check("C (WhatsApp): проверено 2, помечено 1, источник wa=2", (tot(uc, "checked"), tot(uc, "flagged"), tot(uc, "wa")) == (2, 1, 2))
check("C: группа WhatsApp учтена отдельно", uc["groups"]["30"].get("group_would_leave") == 1)
check("глобально = сумма пользователей", tot(g, "checked") == tot(ua, "checked") + tot(ub, "checked") + tot(uc, "checked"))
check("источники tg/wa разделены", tot(g, "tg") == 8 and tot(g, "wa") == 2, f"tg={tot(g, 'tg')} wa={tot(g, 'wa')}")

print("\n[Топы, волны, качество]")
doms = {d[0]: d[1] for d in g["top"]["30"]["domains"]}
check("топ доменов: 1win-bonus.com встречается 3 раза", doms.get("1win-bonus.com") == 3, str(doms))
check("«волна»: домен замечен у 2 пользователей", any(w["domain"] == "1win-bonus.com" and w["users"] == 2 for w in g["waves"]) and any(w["domain"] == "evil-phish.net" and w["users"] == 1 for w in []) is False)
reasons = dict(g["top"]["30"]["reasons"] and [(k, v) for k, v in g["top"]["30"]["reasons"]])
check("причины очищены от чисел: «N сообщений за минуту»", "N сообщений за минуту" in reasons, str(list(reasons)[:4]))
check("сигналы: текст/профиль/ссылка посчитаны", g["top"]["30"]["signals"].get("текст", 0) >= 4 and g["top"]["30"]["signals"].get("ссылка", 0) >= 3, str(g["top"]["30"]["signals"]))
check("уникальных помеченных отправителей считаем", g["top"]["30"]["unique_senders"] >= 2, str(g["top"]["30"]["unique_senders"]))
check("качество по неделям: есть ложные срабатывания", sum(w["fp"] for w in g["quality"]) == 1 and sum(w["flagged"] for w in g["quality"]) == 6, str(g["quality"]))
check("аккаунты: 3 пользователя, Telegram 2, WhatsApp 1", (g["accounts"]["users"], g["accounts"]["tg"], g["accounts"]["wa"]) == (3, 2, 1))
check("заявки: одобрено 1, ожидает 1", (g["access"]["approved"], g["access"]["pending"]) == (1, 1))
check("режимы: блокировка у A (dry_run=0) и C в WhatsApp; у B только уведомления", ua["modes"]["blocking_tg"] and uc["modes"]["blocking_wa"] and not ub["modes"]["blocking_tg"])
check("служебные данные (возраст списков) переданы", g["infra"].get("lists_age_h") == 3.5)

print("\n[Приватность]")
public = json.dumps({k: v for k, v in snap.items() if k != "admin"}, ensure_ascii=False)  # то, что видит обычный пользователь/график
dump = json.dumps(snap, ensure_ascii=False)
check("реальные ID владельцев только в разделе admin (для админа), не в общих данных", not any(str(x) in public for x in (A, B, C)) and all(str(x) in json.dumps(snap["admin"]) for x in (A, B, C)))
dr = {d["id"]: d for d in snap["admin"]["directory"]}
check("справочник админа: все пользователи, у каждого имя/статус/режимы/цифры", set(dr) >= {A, B, C} and all({"name", "username", "status", "tg", "wa", "checked30", "flagged30", "gambling30", "last_seen"} <= set(d) for d in dr.values()))
check("справочник согласован с графиками (псевдоним ведёт на users[p]); без активности — только запись справочника", all(d["p"] in snap["users"] for d in dr.values() if d["id"] != 999) and dr[999]["status"] == "pending")
check("в справочнике нет текстов сообщений и ID собеседников", not any(str(x) in json.dumps(snap["admin"]) for x in (SEND1, SEND2)))
check("ID собеседников отсутствуют в снимке", not any(str(x) in dump for x in (SEND1, SEND2)))
check("JID WhatsApp и названия групп отсутствуют", "@s.whatsapp.net" not in dump and "@g.us" not in dump)
check("псевдонимы стабильны и зависят от ключа", analytics.pseudo(KEY, A) == pa and analytics.pseudo("other-key", A) != pa and len(pa) == 16)
check("в таблице пользователей только псевдонимы и числа", all(set(r) == {"p", "n", "tg", "wa", "checked30", "flagged30", "blocked30", "fp30"} for r in g["table"]))

print("\n[Срок хранения]")
n_before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
res = analytics.prune(db, keep_days=100)
n_after = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
check("старше 100 дней удалено (1 событие), свежие остались", res["events"] == 1 and n_after == n_before - 1, str(res))

print("\n[Подписанные ссылки]")
tok = analytics.make_token("secret", pa, "admin", ttl=60, now=NOW)
d = analytics.verify_token("secret", tok, now=NOW)
check("токен проверяется: псевдоним и роль на месте", d and d["p"] == pa and d["r"] == "admin")
check("чужой секрет отвергается", analytics.verify_token("other", tok, now=NOW) is None)
check("подмена роли отвергается", analytics.verify_token("secret", tok.split(".")[0][:-2] + "AA." + tok.split(".")[1], now=NOW) is None)
check("истёкший токен отвергается", analytics.verify_token("secret", tok, now=NOW + 120) is None)
check("мусор не роняет проверку", analytics.verify_token("secret", "xx") is None and analytics.verify_token("secret", "a.b") is None)

print("\n[Скорость]")
rows = [(A, NOW - (i % 80) * DAY, SEND1, SEND1, "gambling", 0.9, 0.1, 0.9, "warned", "нет фото", "a.com,b.com") for i in range(40000)]
db.executemany("INSERT INTO events(owner_id, ts, chat_id, sender_id, label, text_risk, profile_risk, risk, action, reasons, domains) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
t = time.time(); analytics.build_snapshot(db, pseudo_key=KEY, now=NOW); dt = time.time() - t
check("снимок по 40 000 событий строится быстро", dt < 3, f"{dt:.2f} с")

# кросс-проверка с Node (проверка токена на стороне сайта), если сайт уже есть
web = os.path.join(SRC, "web", "tests", "verify_token.js")
if os.path.exists(web):
    print("\n[Python → Node: токен принимает сайт]")
    tok2 = analytics.make_token("shared-secret", pa, "user", ttl=300)
    out = subprocess.run(["node", web], env={**os.environ, "SITE_SECRET": "shared-secret", "TOKEN": tok2}, capture_output=True, text=True)
    r = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
    check("Node принял токен, выданный Python", r.get("p") == pa and r.get("r") == "user", out.stderr[:100])
    bad = subprocess.run(["node", web], env={**os.environ, "SITE_SECRET": "shared-secret", "TOKEN": tok2[:-3] + "AAA"}, capture_output=True, text=True)
    check("Node отверг подделанный токен", bad.stdout.strip() in ("null", ""))

if os.environ.get("DUMP_SNAPSHOT"):  # для сквозной проверки с сайтом (см. web/tests/e2e.md)
    json.dump({"snapshot": snap, "pa": pa, "pb": pb, "pc": pc}, open(os.environ["DUMP_SNAPSHOT"], "w"), ensure_ascii=False)

print("\n" + ("ВСЕ ТЕСТЫ ПРОШЛИ" if not FAILS else f"ПРОВАЛЕНО: {len(FAILS)} -> {FAILS}"))
sys.exit(1 if FAILS else 0)
