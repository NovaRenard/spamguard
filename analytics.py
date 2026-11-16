"""Аналитика для сайта (Vercel / Telegram Mini App).

Что покидает сервер: агрегаты (счётчики по дням, топы категорий и доменов) и «справочник» пользователей для АДМИНА
(snapshot["admin"]: имя, @username, id, статус, режимы, цифры): API отдаёт его только владельцу роли admin.
Что НЕ покидает: тексты сообщений, телефоны, ID и имена собеседников (тех, кто писал пользователям).
Обычный пользователь видит только свои цифры; он обозначен псевдонимом p = HMAC(PSEUDO_KEY, user_id) (первые 16 hex-символов).

Доступ к сайту: бот по /report выдаёт подписанную ссылку (HMAC-токен с псевдонимом, ролью и сроком жизни).
Сайт проверяет подпись тем же секретом; без валидного токена данных не показывает.

Определения:
  проверено = события ok + помеченные;  помечено = warned | would_block | blocked | false_positive | block_failed
  false_positive = помеченное, которое владелец отметил кнопкой «Это не спам»
  источники: tg (личные чаты Telegram), wa (личные чаты WhatsApp); группы считаются отдельно (group_*)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import aiohttp

log = logging.getLogger("analytics")

FLAGGED = ("warned", "would_block", "blocked", "false_positive", "block_failed")
LABELS = ("gambling", "scam", "spam")
WINDOWS = (7, 30, 90)
_HOST_PREFIX = re.compile(r"^[\w.-]+\.[a-z]{2,}: ")


# ------------------------------------------------------------------ псевдонимы и токены
def pseudo(key: str, uid: int) -> str:
    return hmac.new(key.encode(), str(uid).encode(), hashlib.sha256).hexdigest()[:16]


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_token(secret: str, pseudo_id: str, role: str, ttl: int = 86400, now: float | None = None) -> str:
    payload = json.dumps({"p": pseudo_id, "r": role, "e": int((now or time.time()) + ttl)}, separators=(",", ":")).encode()
    body = _b64(payload)
    return f"{body}.{_b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())}"


def verify_token(secret: str, token: str, now: float | None = None) -> dict | None:
    try:
        body, sig = token.split(".")
        good = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return data if data["e"] >= (now or time.time()) else None
    except Exception:
        return None


# ------------------------------------------------------------------ сбор строк
def _rows(db: sqlite3.Connection, since: float) -> list[dict]:
    out: list[dict] = []
    for r in db.execute(
        "SELECT owner_id, ts, sender_id, label, text_risk, profile_risk, action, reasons, domains FROM events WHERE ts>=?", (since,)
    ):
        owner, ts, sender, label, tr, pr, action, reasons, domains = r
        if action == "test":
            continue
        kind = "group" if action.startswith("group_") else "known" if action == "known_link" else "chat"
        out.append(dict(owner=owner, ts=ts, src="tg", kind=kind, who=f"tg:{sender}", label=label, tr=tr or 0, pr=pr or 0,
                        action=action, reasons=reasons or "", domains=domains or ""))
    try:
        for r in db.execute("SELECT owner_id, ts, jid, kind, label, text_risk, profile_risk, action, reasons, domains FROM wa_events WHERE ts>=?", (since,)):
            owner, ts, jid, kind, label, tr, pr, action, reasons, domains = r
            k = "group" if kind == "group" else "known" if action == "known_link" else "chat"
            out.append(dict(owner=owner, ts=ts, src="wa", kind=k, who=f"wa:{jid}", label=label, tr=tr or 0, pr=pr or 0,
                            action=action, reasons=reasons or "", domains=domains or ""))
    except sqlite3.OperationalError:
        pass  # WhatsApp не подключали
    return out


def _clean_reason(s: str) -> str:
    s = _HOST_PREFIX.sub("", s.strip())
    return re.sub(r"\d+", "N", s)


def _scope(rows: list[dict], days: list[str], tz: timezone) -> dict:
    """Агрегаты для набора строк (один пользователь или все)."""
    daily = {d: dict(date=d, checked=0, flagged=0, blocked=0, would_block=0, warned=0, fp=0, known_link=0,
                     gambling=0, scam=0, spam=0, tg=0, wa=0, group_left=0) for d in days}
    cutoff = {w: time.time() - w * 86400 for w in WINDOWS}
    top: dict[int, dict] = {w: dict(reasons=Counter(), domains=Counter(), dom_label={}, signals=Counter(), senders=set()) for w in WINDOWS}
    groups: dict[int, Counter] = {w: Counter() for w in WINDOWS}
    for r in rows:
        day = datetime.fromtimestamp(r["ts"], tz).strftime("%Y-%m-%d")
        d = daily.get(day)
        a = r["action"]
        if r["kind"] == "group":
            for w in WINDOWS:
                if r["ts"] >= cutoff[w]:
                    groups[w][a] += 1
            if a == "group_left" and d:
                d["group_left"] += 1
            continue
        if r["kind"] == "known":
            if d:
                d["known_link"] += 1
            continue
        if d is not None:
            d["checked"] += 1
            d[r["src"]] += 1
            if a in FLAGGED:
                d["flagged"] += 1
                if r["label"] in LABELS:
                    d[r["label"]] += 1
                if a == "blocked":
                    d["blocked"] += 1
                elif a == "would_block":
                    d["would_block"] += 1
                elif a == "warned":
                    d["warned"] += 1
                elif a == "false_positive":
                    d["fp"] += 1
        if a in FLAGGED:
            for w in WINDOWS:
                if r["ts"] < cutoff[w]:
                    continue
                t = top[w]
                t["senders"].add(r["who"])
                for reason in filter(None, (x.strip() for x in r["reasons"].split(";"))):
                    t["reasons"][_clean_reason(reason)] += 1
                for dom in filter(None, r["domains"].split(",")):
                    t["domains"][dom] += 1
                    t["dom_label"][dom] = r["label"]
                if r["tr"] >= 0.5:
                    t["signals"]["текст"] += 1
                if r["pr"] >= 0.5:
                    t["signals"]["профиль"] += 1
                if r["domains"]:
                    t["signals"]["ссылка"] += 1
    return {
        "daily": [daily[d] for d in days],
        "top": {str(w): dict(
            reasons=[[k, v] for k, v in top[w]["reasons"].most_common(8)],
            domains=[[k, v, top[w]["dom_label"].get(k, "")] for k, v in top[w]["domains"].most_common(8)],
            signals=dict(top[w]["signals"]),
            unique_senders=len(top[w]["senders"]),
        ) for w in WINDOWS},
        "groups": {str(w): dict(groups[w]) for w in WINDOWS},
    }


# ------------------------------------------------------------------ снимок
def build_snapshot(db: sqlite3.Connection, *, pseudo_key: str, tz_hours: float = 5, days: int = 90, now: float | None = None,
                   extra: dict | None = None) -> dict:
    now = now or time.time()
    tz = timezone(timedelta(hours=tz_hours))
    today = datetime.fromtimestamp(now, tz).date()
    day_list = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
    rows = _rows(db, now - days * 86400)
    by_owner: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_owner[r["owner"]].append(r)

    # у кого что подключено и какой режим (только флаги, без идентификаторов)
    tg = {r[0]: r for r in db.execute("SELECT owner_id, dry_run, COALESCE(group_leave,0) FROM users")}
    try:
        wa = {r[0]: r for r in db.execute("SELECT owner_id, status, wa_live FROM wa_instances")}
    except sqlite3.OperationalError:
        wa = {}
    owners = set(by_owner) | set(tg) | set(wa)

    users: dict[str, dict] = {}
    table = []
    for i, owner in enumerate(sorted(owners)):
        sc = _scope(by_owner.get(owner, []), day_list, tz)
        pid = pseudo(pseudo_key, owner)
        last30 = sc["daily"][-30:]
        users[pid] = {
            **sc,
            "connected": {"tg": owner in tg, "wa": owner in wa and wa[owner][1] == "connected"},
            "modes": {"blocking_tg": bool(owner in tg and not tg[owner][1]), "autoleave_tg": bool(owner in tg and tg[owner][2]),
                      "blocking_wa": bool(owner in wa and wa[owner][2])},
        }
        table.append({"p": pid, "n": i + 1, "tg": owner in tg, "wa": owner in wa and wa[owner][1] == "connected",
                      "checked30": sum(d["checked"] for d in last30), "flagged30": sum(d["flagged"] for d in last30),
                      "blocked30": sum(d["blocked"] for d in last30), "fp30": sum(d["fp"] for d in last30)})

    glob = _scope(rows, day_list, tz)

    # справочник для админа: кто пользуется сервисом и что с ним происходит (без текстов сообщений)
    try:
        acc = {r[0]: r for r in db.execute("SELECT user_id, status, name, username, ts, last_seen FROM access")}
    except sqlite3.OperationalError:
        acc = {}
    directory = []
    for owner in sorted(set(acc) | owners):
        pid = pseudo(pseudo_key, owner)
        a = acc.get(owner)
        u = users.get(pid)
        last30 = u["daily"][-30:] if u else []
        top30 = (u["top"]["30"] if u else {"reasons": [], "domains": [], "signals": {}})
        owner_rows = by_owner.get(owner, [])
        last_ev = max((r["ts"] for r in owner_rows), default=None)
        directory.append({
            "p": pid, "id": owner, "name": (a[2] if a else "") or "", "username": (a[3] if a else "") or "",
            "status": (a[1] if a else "env") or "env", "joined": int(a[4]) if a and a[4] else None,
            "last_seen": int(max(filter(None, [a[5] if a else None, last_ev]), default=0)) or None,
            "tg": bool(u and u["connected"]["tg"]), "wa": bool(u and u["connected"]["wa"]),
            "blocking_tg": bool(u and u["modes"]["blocking_tg"]), "autoleave_tg": bool(u and u["modes"]["autoleave_tg"]),
            "blocking_wa": bool(u and u["modes"]["blocking_wa"]),
            "checked30": sum(d["checked"] for d in last30), "flagged30": sum(d["flagged"] for d in last30),
            "blocked30": sum(d["blocked"] for d in last30), "fp30": sum(d["fp"] for d in last30),
            "gambling30": sum(d["gambling"] for d in last30), "scam30": sum(d["scam"] for d in last30),
            "spam30": sum(d["spam"] for d in last30), "group_left30": sum(d["group_left"] for d in last30),
            "senders30": top30.get("unique_senders", 0),
        })
    directory.sort(key=lambda x: -(x["last_seen"] or 0))

    # «волны»: домены, замеченные у нескольких разных пользователей за 7 дней
    seen: dict[str, set] = defaultdict(set)
    cnt: Counter = Counter()
    week = now - 7 * 86400
    for r in rows:
        if r["ts"] >= week and r["action"] in FLAGGED:
            for dom in filter(None, r["domains"].split(",")):
                seen[dom].add(r["owner"])
                cnt[dom] += 1
    waves = [{"domain": d, "users": len(o), "count": cnt[d]} for d, o in seen.items() if len(o) >= 2]
    waves.sort(key=lambda x: (-x["users"], -x["count"]))

    # качество: доля ложных срабатываний по неделям
    weekly: dict[str, list[int]] = {}
    for r in rows:
        if r["kind"] != "chat" or r["action"] not in FLAGGED:
            continue
        dt = datetime.fromtimestamp(r["ts"], tz).date()
        ws = (dt - timedelta(days=dt.weekday())).strftime("%Y-%m-%d")
        w = weekly.setdefault(ws, [0, 0])
        w[0] += 1
        w[1] += r["action"] == "false_positive"
    quality = [{"week": k, "flagged": v[0], "fp": v[1]} for k, v in sorted(weekly.items())][-12:]

    try:
        access = dict(db.execute("SELECT status, COUNT(*) FROM access GROUP BY status").fetchall())
    except sqlite3.OperationalError:
        access = {}
    glob.update({
        "accounts": {"users": len(owners), "tg": len(tg), "wa": sum(1 for v in wa.values() if v[1] == "connected"),
                     "wa_pending": sum(1 for v in wa.values() if v[1] == "pending")},
        "access": {"approved": access.get("approved", 0), "pending": access.get("pending", 0),
                   "rejected": access.get("rejected", 0) + access.get("revoked", 0)},
        "waves": waves[:10],
        "quality": quality,
        "table": table,
        "infra": extra or {},
    })
    admin = {"directory": directory}
    return {"v": 1, "generated_at": int(now), "tz": tz_hours, "days": day_list, "global": glob, "users": users, "admin": admin}


def prune(db: sqlite3.Connection, keep_days: int = 180) -> dict:
    """Срок хранения сырых событий. Тексты сообщений не хранятся вовсе, только метаданные."""
    cut = time.time() - keep_days * 86400
    n1 = db.execute("DELETE FROM events WHERE ts<?", (cut,)).rowcount
    try:
        n2 = db.execute("DELETE FROM wa_events WHERE ts<?", (cut,)).rowcount
        db.execute("DELETE FROM wa_refs WHERE ts<?", (time.time() - 30 * 86400,))
    except sqlite3.OperationalError:
        n2 = 0
    db.commit()
    return {"events": n1, "wa_events": n2}


async def push(url: str, secret: str, snapshot: dict, timeout: float = 20) -> bool:
    """Отправляет снимок на сайт (POST /api/ingest). Секрет в заголовке, канал только HTTPS."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json=snapshot, headers={"Authorization": f"Bearer {secret}"},
                              timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status >= 300:
                    log.warning("сайт ответил %s: %s", r.status, (await r.text())[:120])
                    return False
                return True
    except Exception as ex:
        log.warning("не удалось отправить аналитику: %s", ex)
        return False
