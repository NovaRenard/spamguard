"""Признаки профиля отправителя. Спамера часто видно по профилю, даже если текст сообщения «чистый».

Сильные признаки (сами по себе тянут на предупреждение/блокировку):
  • Telegram пометил аккаунт как scam / fake
  • имя или @username похожи на казино/мошенничество
  • «О себе» содержит спам/казино
Слабые (только усиливают другие признаки, суммарно не больше 0.35):
  • нет фото, нет @username, нет общих чатов с вами, ссылка в «О себе» или в сообщении,
    несколько сообщений подряд за минуту
Итог — «noisy-or»: риск растёт, если признаки накладываются друг на друга.
"""
from telethon import functions
from telethon.tl.types import UserProfilePhotoEmpty

from classifier import SpamGuard, has_link, normalize, rule_score


def _noisy_or(ps: list[float]) -> float:
    out = 1.0
    for p in ps:
        out *= 1 - min(max(p, 0.0), 1.0)
    return 1 - out


async def profile_risk(client, e, sender, guard: SpamGuard, burst: int, text: str) -> tuple[float, list[str], str | None]:
    """Возвращает (риск профиля 0..1, список причин по-русски, предполагаемый класс или None)."""
    strong: list[float] = []
    reasons: list[str] = []
    label: str | None = None
    weak = 0.0

    if getattr(sender, "scam", False) or getattr(sender, "fake", False):
        strong.append(0.95)
        reasons.append("Telegram пометил аккаунт как scam/fake")
        label = "scam"

    ident = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None), getattr(sender, "username", None)]))
    r, lab, _ = rule_score(normalize(ident))
    if r > 0:
        strong.append(0.8 * r)
        reasons.append(f"имя/@username похожи на «{lab}»")
        label = label or lab

    about, common = "", None
    try:
        full = await client(functions.users.GetFullUserRequest(await e.get_input_sender()))
        about = full.full_user.about or ""
        common = getattr(full.full_user, "common_chats_count", None)
    except Exception:
        pass  # профиль не получилось прочитать — просто без этих признаков

    if about:
        b = guard.predict(about)
        # «О себе» — короткий текст не из обучающего распределения: верим только явному не-ok классу
        if b["label"] != "ok" and b["risk"] >= 0.7:
            strong.append(0.85 * b["risk"])
            reasons.append(f"в «О себе»: {b['label']}")
            label = label or b["label"]
        elif has_link(about):
            weak += 0.10
            reasons.append("ссылка в «О себе»")

    photo = getattr(sender, "photo", None)
    if photo is None or isinstance(photo, UserProfilePhotoEmpty):
        weak += 0.08
        reasons.append("нет фото")
    if not getattr(sender, "username", None):
        weak += 0.05
    if common == 0:
        weak += 0.10
        reasons.append("нет общих чатов")
    if has_link(text):
        weak += 0.10
        reasons.append("ссылка в сообщении")
    if burst >= 3:
        weak += 0.15
        reasons.append(f"{burst} сообщений за минуту")

    return _noisy_or(strong + [min(weak, 0.35)]), reasons, label
