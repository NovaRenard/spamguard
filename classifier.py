"""Лёгкий классификатор: правила + TF-IDF(char+word) + LogisticRegression.

Классы: ok | spam | gambling | scam
Итоговый риск = max(1 - P(ok), оценка правил).
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import FeatureUnion, Pipeline

MODEL_PATH = Path(__file__).parent / "model.joblib"

# ---------- нормализация (против обфускации: "к а з и н о", "casino", смесь латиницы и кириллицы)
_LAT2CYR = str.maketrans("acekmohptxyb", "асекмонртхув")
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_SPACED = re.compile(r"(?<!\w)(?:\w[\s.\-_*]){3,}\w(?!\w)")
_HAS_CYR = re.compile(r"[а-яёәғқңөұүһі]")
_HAS_LAT = re.compile(r"[a-z]")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH).lower()
    t = _SPACED.sub(lambda m: re.sub(r"[\s.\-_*]", "", m.group()), t)
    words = []
    for w in t.split():
        if _HAS_CYR.search(w) and _HAS_LAT.search(w):  # смешанное слово -> всё в кириллицу
            w = w.translate(_LAT2CYR)
        words.append(w)
    return " ".join(words)


# ---------- правила
_G_STRONG = re.compile(
    r"казино|casino|букмекер|1win|1xbet|мелбет|melbet|вавада|vavada|pin-?up|пинап|фри-?спин|free ?spin|"
    r"джекпот|jackpot|авиатор|aviator|lucky ?jet|заносы|фрибет|азино|betwinner|parimatch|mostbet|joycasino|фонбет|тегін айналым|букмекер\w*|ұтып алдым|ұтыс\w*"
)
_G_WEAK = re.compile(r"ставк[аиуе]|\bслот|рулетк|бонус|выигрыш|выиграл|промокод|депозит|капперы?|прогноз|бәс тік\w*|сигнал\w*|жабық арна|кэшбэк|занос")
_SCAM = re.compile(
    r"заработ\w+ (?:от )?\d+|пассивн\w+ доход|удал[её]нн\w+ работ|без вложени|usdt|"
    r"аккаунт\w* (?:будет )?(?:заблок|взлом)|карта (?:заблок|сбербанк\w* заблок)|код из смс|гарантирован\w+ (?:доход|прибыл)|"
    r"вам (?:полагается|начислен)|вы (?:выиграли|стали победител)|переведи \d+ ?(?:руб|₽)?|без отказа|легк\w+ подработк|л[её]гк\w+ заработок|оңай (?:жол|ақша)|есе көбей\w*|көбейтуге|пайда кепіл\w*|кепілді (?:пайда|табыс|жеңіс)|бұғатталды|кодты жібер\w*|растау коды|инвестициясыз|ұтып алдыңыз|алдын ала [\w ]{0,12}төле\w*|шұғыл \d* ?(?:тг|теңге)? ?аудар\w*|айына \d[\d ]* ?(?:тг|теңге) (?:таб\w*|жұмыс)|ақша салсаң|кепілдік беремін"
)
_LINK = re.compile(r"https?://|t\.me/|@\w{5,}|\bbit\.ly|tinyurl|\.top\b|\.xyz\b")


def rule_score(norm: str) -> tuple[float, str | None, list[str]]:
    """Консервативные правила: одно слабое слово («ставка», «слот», «бонус») риска не даёт."""
    reasons: list[str] = []
    gs = set(_G_STRONG.findall(norm))
    gw = set(_G_WEAK.findall(norm))
    s = set(_SCAM.findall(norm))
    link = bool(_LINK.search(norm))
    if gs:
        reasons.append(f"gambling-strong: {sorted(gs)[:3]}")
    if len(gw) >= 2 or (gw and link):
        reasons.append(f"gambling-weak: {sorted(gw)[:3]}")
    if s:
        reasons.append(f"scam: {sorted(s)[:3]}")
    if link and reasons:
        reasons.append("ссылка/юзернейм рядом с триггерами")
    if gs:
        return min(0.97, 0.6 + 0.15 * len(gs) + (0.15 if link else 0)), "gambling", reasons
    if len(gw) >= 2 or (gw and link):
        return min(0.8, 0.5 + 0.1 * len(gw) + (0.15 if link else 0)), "gambling", reasons
    if s:
        return min(0.95, 0.55 + 0.15 * len(s) + (0.15 if link else 0)), "scam", reasons
    return 0.0, None, reasons


# ---------- модель
def build_pipeline() -> Pipeline:
    feats = FeatureUnion(
        [
            ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True, min_df=1)),
            ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2), sublinear_tf=True, min_df=1)),
        ]
    )
    clf = LogisticRegression(C=10, max_iter=3000, class_weight="balanced")
    return Pipeline([("f", feats), ("clf", clf)])


def train(texts: list[str], labels: list[str], path: Path = MODEL_PATH) -> Pipeline:
    pipe = build_pipeline().fit([normalize(t) for t in texts], labels)
    joblib.dump(pipe, path)
    return pipe


class SpamGuard:
    def __init__(self, path: Path = MODEL_PATH):
        self.pipe: Pipeline | None = joblib.load(path) if Path(path).exists() else None

    def predict(self, text: str) -> dict:
        norm = normalize(text)
        r_score, r_label, reasons = rule_score(norm)
        label, risk, probs = "ok", 0.0, {}
        if self.pipe is not None:
            p = self.pipe.predict_proba([norm])[0]
            probs = dict(zip(self.pipe.classes_, map(float, p)))
            risk = 1.0 - probs.get("ok", 0.0)
            label = max(probs, key=probs.get)
        if r_score > risk:  # правила перевесили
            risk, label = r_score, r_label or label
        return {"label": label if risk >= 0.5 else "ok", "risk": round(risk, 3), "probs": probs, "reasons": reasons}


def has_link(text: str) -> bool:
    return bool(_LINK.search(normalize(text)))
