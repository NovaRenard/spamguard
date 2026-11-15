"""Оценка группы или канала: «это про азартные игры / мошенничество / спам?»

Смотрим на три источника (как и просил владелец):
  1. название и @username        — короткий текст, верим только сильным словам (казино, 1win, фриспин…)
  2. описание («О группе/канале») — тоже короткий текст, верим только явному не-ok классу
  3. последние сообщения          — главный признак: не одно, а ЗАМЕТНАЯ ДОЛЯ сообщений должна быть подозрительной,
                                     иначе обычная группа с одним рекламным ботом улетит в «гемблинг»

Итог — noisy-or трёх оценок. Функция score_chat чистая (без сети), поэтому её легко тестировать.
"""
from __future__ import annotations

from collections import Counter

from classifier import SpamGuard, normalize, rule_score

MIN_MSG_LEN = 15  # короткие «ок», «+1», стикеры не считаем


def _noisy_or(ps: list[float]) -> float:
    out = 1.0
    for p in ps:
        out *= 1 - min(max(p, 0.0), 1.0)
    return 1 - out


def _short_text_signal(guard: SpamGuard, text: str, min_risk: float) -> tuple[float, str | None]:
    """Для коротких текстов (название, описание): модель на них теряется, поэтому верим только явному не-ok классу."""
    if not text or not text.strip():
        return 0.0, None
    r_rule, lab_rule, _ = rule_score(normalize(text))
    res = guard.predict(text)
    model_risk = res["risk"] if (res["label"] != "ok" and res["risk"] >= min_risk) else 0.0
    if r_rule >= model_risk and r_rule > 0:
        return r_rule, lab_rule
    if model_risk > 0:
        return model_risk, res["label"]
    return 0.0, None


def score_chat(title: str, username: str | None, about: str, texts: list[str], guard: SpamGuard) -> dict:
    """Возвращает {risk, label, reasons, flagged, checked}."""
    reasons: list[str] = []
    labels: Counter = Counter()
    parts: list[float] = []

    t_risk, t_lab = _short_text_signal(guard, f"{title} {username or ''}", min_risk=0.8)
    if t_risk > 0:
        parts.append(0.8 * t_risk)
        labels[t_lab] += 1
        reasons.append(f"название/@username: {t_lab}")

    a_risk, a_lab = _short_text_signal(guard, about, min_risk=0.7)
    if a_risk > 0:
        parts.append(0.85 * a_risk)
        labels[a_lab] += 1
        reasons.append(f"описание: {a_lab}")

    considered = [t for t in texts if t and len(t.strip()) >= MIN_MSG_LEN]
    flagged = 0
    for t in considered:
        res = guard.predict(t)
        if res["label"] != "ok" and res["risk"] >= 0.6:
            flagged += 1
            labels[res["label"]] += 1
    if considered and flagged >= 3:
        share = flagged / len(considered)
        if share >= 0.25:
            parts.append(min(0.95, 0.5 + share))
            reasons.append(f"{flagged} из {len(considered)} последних сообщений подозрительные")
    elif flagged == 2:
        parts.append(0.3)
        reasons.append(f"2 из {len(considered)} последних сообщений подозрительные")

    risk = _noisy_or(parts)
    label = labels.most_common(1)[0][0] if labels else "ok"
    return {"risk": risk, "label": label, "reasons": reasons, "flagged": flagged, "checked": len(considered)}


async def analyze_chat(client, ent, guard: SpamGuard, limit: int = 25) -> dict:
    """Достаёт данные из Telegram и оценивает. Побочных действий (выход) здесь нет."""
    from telethon import functions
    from telethon.tl.types import Channel

    title = getattr(ent, "title", "") or ""
    username = getattr(ent, "username", None)
    about = ""
    try:
        if isinstance(ent, Channel):
            full = await client(functions.channels.GetFullChannelRequest(ent))
        else:
            full = await client(functions.messages.GetFullChatRequest(ent.id))
        about = full.full_chat.about or ""
    except Exception:
        pass  # описания нет/не прочиталось — продолжаем по названию и сообщениям
    texts: list[str] = []
    try:
        msgs = await client.get_messages(ent, limit=limit)
        texts = [m.message for m in msgs if getattr(m, "message", None)]
    except Exception:
        pass
    res = score_chat(title, username, about, texts, guard)
    res.update(title=title, username=username, kind="канал" if getattr(ent, "broadcast", False) else "группа")
    return res
