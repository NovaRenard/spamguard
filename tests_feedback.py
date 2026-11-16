"""Тесты дообучения на отметках «Это не спам»:  python tests_feedback.py
Работает в памяти, ваши model.joblib и state.db не затрагиваются."""
import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

from classifier import SpamGuard, load_base, normalize
from feedback import Feedback

FAILS = []


def check(name, cond, extra=""):
    print(f"  {'✔' if cond else '✘ ПРОВАЛ'} {name} {extra}")
    if not cond:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp()) / "model.joblib"
g = SpamGuard(path=tmp)
g.retrain(save=True)  # чистая базовая модель во временной папке
db = sqlite3.connect(":memory:")
ADMIN, U1, U2, U3 = 1, 11, 12, 13
fb = Feedback(db, g, admin_ids={ADMIN}, min_users=2)

print("\n[Датасет]")
texts, labels = load_base()
from collections import Counter  # noqa: E402
cnt = Counter(labels)
check("данных стало больше (≥ 6000) и есть все 4 класса", len(texts) >= 6000 and set(cnt) == {"ok", "spam", "gambling", "scam"}, str(dict(cnt)))
import re  # noqa: E402
KZ = re.compile(r"[әғқңөұүһі]")
LAT = re.compile(r"\b(salem|brat|kazino|tirkel|casino)\b", re.I)
check("есть казахские примеры", sum(1 for t in texts if KZ.search(t.lower())) >= 1500)
check("есть смесь русского и казахского", sum(1 for t in texts if KZ.search(t.lower()) and re.search(r"[а-я]{4,}", t.lower())) >= 800)
check("есть транслит (латиницей)", sum(1 for t in texts if LAT.search(t)) >= 100)
check("нет «сырых» плейсхолдеров {…} в датасете", not any("{" in t or "}" in t for t in texts))

print("\n[Отметка «не спам» действует сразу для владельца]")
T = "Давай вечером посмотрим Казино Рояль"
before = g.predict(T, owner=U1)
check("до отметки сообщение подозрительное (правило «казино»)", before["label"] != "ok" and before["risk"] >= 0.5, str(before["risk"]))
n = fb.add(U1, [T], "chat")
check("отметка сохранена", n == 1)
check("повторная отметка того же текста не дублируется", fb.add(U1, [T]) == 0)
check("у этого владельца теперь не помечается", g.predict(T, owner=U1)["label"] == "ok" and g.predict(T, owner=U1)["risk"] == 0)
check("у другого пользователя пока по-прежнему помечается (один человек общую модель не меняет)", g.predict(T, owner=U2)["risk"] >= 0.5)
check("в общую модель пока не попало", T not in " ".join(fb.promoted()) and not fb.promoted())

print("\n[Защита от «отравления»]")
casino = "Регистрируйся в казино 1win и забирай фриспины https://1win-bonus.top"
fb.add(U1, [casino])
check("один обычный пользователь не может «научить» модель считать казино нормальным", g.predict(casino, owner=U3)["risk"] >= 0.85 and not fb.promoted())
fb.add(U2, [casino])
check("два разных пользователя — пример становится общим", len(fb.promoted()) == 1)
fb.forget(U2)
check("после /stop одного из них (удалены его тексты) пример перестаёт быть общим", not fb.promoted() and g.predict(casino, owner=U3)["risk"] >= 0.85)

print("\n[Админ подтверждает сразу, модель дообучается]")
S = "Мы сегодня смотрим фильм про казино, присоединяйся"
b_before = g.predict(S, owner=U3)
fb.add(ADMIN, [S])
check("текст админа сразу общий", normalize(S) in fb.promoted())
check("и сразу не помечается у всех", g.predict(S, owner=U3)["risk"] == 0.0)
v0 = g.model_version
ok = asyncio.run(fb.retrain_if_needed())
check("модель дообучена в фоне и подменена", ok and g.model_version == v0 + 1)
p_after = g.pipe.predict_proba([normalize(S)])[0][list(g.pipe.classes_).index("ok")]
check("модель теперь считает такое сообщение нормальным", p_after >= 0.5, f"P(ok)={p_after:.2f}")
check("без новых отметок повторно не переобучается", asyncio.run(fb.retrain_if_needed()) is False)

print("\n[Перезапуск]")
g2 = SpamGuard(path=tmp)
fb2 = Feedback(db, g2, admin_ids={ADMIN}, min_users=2)
fb2.load()
check("память об отметках восстанавливается из базы", g2.predict(T, owner=U1)["risk"] == 0 and g2.predict(S, owner=U3)["risk"] == 0)

print("\n[Мусор]")
check("слишком короткие/пустые тексты не сохраняются", fb.add(U3, ["", "  ", "ок", None]) == 0)
check("статистика считается", fb.stats()["marks"] >= 2)

print("\n" + ("ВСЕ ТЕСТЫ ПРОШЛИ" if not FAILS else f"ПРОВАЛЕНО: {len(FAILS)} -> {FAILS}"))
sys.exit(1 if FAILS else 0)
