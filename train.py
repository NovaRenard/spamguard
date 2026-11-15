"""python train.py — оценка на data/handwritten.csv (модель его НЕ видит), затем финальное обучение на всех данных."""
import sys
from pathlib import Path

from sklearn.metrics import classification_report, confusion_matrix

from classifier import SpamGuard, build_pipeline, normalize, train

D = Path(__file__).parent / "data"


def load(p: Path):
    pairs = [ln.rsplit(",", 1) for ln in p.read_text(encoding="utf-8").splitlines()[1:] if ln.strip()]
    return [t for t, _ in pairs], [l.strip() for _, l in pairs]


Xs, ys = load(D / "synthetic.csv")
Xh, yh = load(D / "handwritten.csv")

# 1) честная оценка: обучаем только на синтетике, проверяем на ручных примерах
tmp = build_pipeline().fit([normalize(t) for t in Xs], ys)
import classifier  # noqa: E402

g = SpamGuard(path=Path("/nonexistent"))
g.pipe = tmp
pred = [g.predict(t)["label"] for t in Xh]  # модель + правила, как в бою
print(f"hold-out на ручных примерах ({len(Xh)} шт.), модель+правила:")
print(classification_report(yh, pred, zero_division=0))
labels = ["ok", "gambling", "scam", "spam"]
print("confusion (строки=факт, столбцы=предсказание)", labels)
print(confusion_matrix(yh, pred, labels=labels))
import re  # noqa: E402

KZ = re.compile(r"[әғқңөұүһі]")
kz_idx = [i for i, t in enumerate(Xh) if KZ.search(t.lower())]
if kz_idx:
    ok = sum(1 for i in kz_idx if yh[i] == pred[i])
    print(f"казахские ручные примеры: {ok}/{len(kz_idx)} верно ({ok / len(kz_idx):.0%})")
for t, a, p in zip(Xh, yh, pred):
    if a != p:
        print(f"  ошибка: факт={a} pred={p} | {t[:80]}")

# 2) финальная модель на всех данных
train(Xs + Xh, ys + yh)
print(f"saved model: {len(Xs)} синтетических + {len(Xh)} ручных")
