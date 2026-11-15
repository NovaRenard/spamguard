"""Проверка ссылок: гемблинг, фишинг, мошенничество.

Принципы (безопасность и нагрузка):
  • НА САЙТЫ ПО ССЫЛКАМ МЫ НЕ ХОДИМ. Это исключает SSRF (бот не полезет во внутреннюю сеть) и не выдаёт серверу «живого» адресата.
    Единственное исключение — раскрытие коротких ссылок: запрос идёт только на сам сокращатель (bit.ly и т. п.),
    редиректы читаются вручную (allow_redirects=False), итоговую цель мы только анализируем, но не открываем.
  • Основа — локальные списки доменов (blocklistproject: gambling, phishing, scam, fraud) и фид OpenPhish. Поиск за микросекунды.
  • Эвристики: похожесть на бренды (kaspi, halyk…), punycode, IP вместо домена, «@» в адресе, подозрительные зоны, ключевые слова.
  • Возраст домена через RDAP (по желанию, LINKS_RDAP=1): новый домен (<30 дней) — сильный сигнал. Кэшируется.
  • Большинство сообщений без ссылок вообще не создают нагрузки; результаты по доменам кэшируются.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import tldextract

from classifier import normalize, rule_score

log = logging.getLogger("links")

_EXT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)  # встроенный список зон, без сети

LIST_SOURCES = {  # имя -> (URL, категория)
    "gambling": ("https://raw.githubusercontent.com/blocklistproject/Lists/master/gambling.txt", "gambling"),
    "phishing": ("https://raw.githubusercontent.com/blocklistproject/Lists/master/phishing.txt", "scam"),
    "scam": ("https://raw.githubusercontent.com/blocklistproject/Lists/master/scam.txt", "scam"),
    "fraud": ("https://raw.githubusercontent.com/blocklistproject/Lists/master/fraud.txt", "scam"),
}
OPENPHISH_URL = "https://openphish.com/feed.txt"

SHORTENERS = {
    "bit.ly", "tinyurl.com", "cutt.ly", "t.ly", "rb.gy", "is.gd", "v.gd", "shorturl.at", "clck.ru", "goo.gl", "ow.ly",
    "buff.ly", "tiny.cc", "vk.cc", "s.id", "u.to", "lnkd.in", "t.co", "fb.me", "bitly.com", "qr.ae", "short.io",
    "rebrand.ly", "cli.re", "l.wl.co", "trib.al", "soo.gd", "shrtco.de", "clk.sh", "adf.ly", "ouo.io", "bl.ink",
}
FREE_HOSTING = {
    "github.io", "pages.dev", "vercel.app", "netlify.app", "blogspot.com", "weebly.com", "wixsite.com", "webflow.io",
    "workers.dev", "glitch.me", "replit.app", "herokuapp.com", "firebaseapp.com", "web.app", "000webhostapp.com",
    "framer.website", "carrd.co", "tilda.ws", "wordpress.com", "sites.google.com", "notion.site",
}
SUSPICIOUS_TLDS = {
    "top", "xyz", "click", "site", "online", "icu", "cfd", "sbs", "live", "buzz", "rest", "monster", "vip", "bet", "casino",
    "win", "fun", "work", "loan", "gq", "tk", "ml", "cf", "ga", "link", "cam", "bid", "stream", "download", "racing",
    "party", "review", "trade", "webcam", "date", "men", "country", "kim", "science", "zip", "mov",
}
ALLOWLIST = {
    "google.com", "youtube.com", "youtu.be", "wikipedia.org", "github.com", "gitlab.com", "microsoft.com", "office.com",
    "live.com", "apple.com", "icloud.com", "amazon.com", "zoom.us", "whatsapp.com", "instagram.com", "facebook.com",
    "linkedin.com", "twitter.com", "x.com", "yandex.ru", "ya.ru", "mail.ru", "vk.com", "ok.ru", "2gis.kz", "2gis.ru",
    "kaspi.kz", "halykbank.kz", "jusan.kz", "egov.kz", "gov.kz", "olx.kz", "kolesa.kz", "krisha.kz", "telegram.org",
    "kazpost.kz", "beeline.kz", "tele2.kz", "altel.kz", "sber.ru", "sberbank.ru", "tinkoff.ru", "binance.com", "paypal.com",
    "stackoverflow.com", "reddit.com", "medium.com", "dropbox.com", "drive.google.com", "docs.google.com", "mozilla.org",
    "python.org", "npmjs.com", "docker.com", "cloudflare.com", "openai.com", "anthropic.com", "claude.ai",
}
# бренды, под которые маскируются мошенники: токен -> официальные домены
BRANDS = {
    "kaspi": {"kaspi.kz"}, "halyk": {"halykbank.kz", "halyk.kz"}, "halykbank": {"halykbank.kz"}, "jusan": {"jusan.kz"},
    "sberbank": {"sberbank.ru", "sber.ru"}, "tinkoff": {"tinkoff.ru"}, "binance": {"binance.com"}, "paypal": {"paypal.com"},
    "telegram": {"telegram.org", "t.me", "telegram.me"}, "whatsapp": {"whatsapp.com"}, "instagram": {"instagram.com"},
    "egov": {"egov.kz", "gov.kz"}, "kolesa": {"kolesa.kz"}, "olx": {"olx.kz"}, "krisha": {"krisha.kz"},
    "kazpost": {"kazpost.kz"}, "beeline": {"beeline.kz"}, "google": {"google.com"}, "apple": {"apple.com"},
}
PATH_WORDS = re.compile(r"login|signin|verify|secure|account|update|bonus|promo|claim|giveaway|prize|wallet|airdrop|casino|slots|1win|vavada")
TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}

_URL_RE = re.compile(r"(?i)((?:https?://|www\.)[^\s<>\"'«»]+|(?<![\w@.-])(?:[a-z0-9-]+\.)+[a-z]{2,24}(?:/[^\s<>\"'«»]*)?)")
_TRIM = ".,;:!?)]}»\"'>"


def _noisy_or(ps) -> float:
    out = 1.0
    for p in ps:
        out *= 1 - min(max(p, 0.0), 1.0)
    return 1 - out


def deobfuscate(text: str) -> str:
    """Спамеры пишут «1win[.]com» и «hxxp» — возвращаем обычный вид."""
    t = text.replace("[.]", ".").replace("(.)", ".").replace("{.}", ".").replace("[dot]", ".")
    t = re.sub(r"(?i)hxxp", "http", t)
    return re.sub(r"[​‌‍⁠﻿]", "", t)


def extract_urls(text: str, extra: list[str] | None = None, limit: int = 5) -> list[str]:
    """Достаёт ссылки (включая «голые» домены вроде 1win.casino и скрытые ссылки из разметки)."""
    found: list[str] = list(extra or [])
    for m in _URL_RE.finditer(deobfuscate(text or "")):
        u = m.group(1).rstrip(_TRIM)
        if not u.lower().startswith(("http://", "https://")):
            if u.lower().startswith("www."):
                u = "http://" + u
            else:  # «голый» домен: нужна настоящая зона (отсекает file.txt, т.д. и т. п.)
                host = u.split("/")[0]
                ext = _EXT(host)
                if not ext.suffix or not ext.domain:
                    continue
                u = "http://" + u
        found.append(u)
    seen, out = set(), []
    for u in found:
        key = u.lower()
        if key not in seen:
            seen.add(key)
            out.append(u)
    return out[:limit]


def _lev(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


@dataclass
class LinkResult:
    url: str
    host: str
    risk: float = 0.0
    label: str = "ok"
    reasons: list[str] = field(default_factory=list)


@dataclass
class TextLinks:
    risk: float = 0.0
    label: str = "ok"
    results: list[LinkResult] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [f"{r.host}: {', '.join(r.reasons)}" for r in self.results if r.risk >= 0.3 and r.reasons]


class LinkChecker:
    def __init__(self, data_dir: str | Path = "lists", rdap: bool = True):
        self.dir = Path(data_dir)
        self.rdap = rdap
        self.sets: dict[str, set[str]] = {"gambling": set(), "scam": set()}
        self.phish_urls: set[str] = set()
        self._session: aiohttp.ClientSession | None = None
        self._age: dict[str, tuple[float, int | None]] = {}
        self._expanded: dict[str, tuple[float, str]] = {}
        self._rdap_sem = asyncio.Semaphore(3)
        self.loaded_at: float = 0

    # ------------------------------------------------------------------ списки
    @staticmethod
    def _parse_hosts(path: Path) -> set[str]:
        out = set()
        if not path.exists():
            return out
        with open(path, encoding="utf-8", errors="ignore") as f:
            for ln in f:
                ln = ln.strip()
                if not ln or ln.startswith("#"):
                    continue
                d = ln.split()[-1].lower().strip(".")
                if d and d != "localhost" and "." in d:
                    out.add(d)
        return out

    def load_from_disk(self) -> None:
        sets: dict[str, set[str]] = {"gambling": set(), "scam": set()}
        for name, (_, cat) in LIST_SOURCES.items():
            sets[cat] |= self._parse_hosts(self.dir / f"{name}.txt")
        self.sets = sets
        p = self.dir / "openphish.txt"
        self.phish_urls = {ln.strip().lower().rstrip("/") for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()} if p.exists() else set()
        self.loaded_at = time.time()
        log.info("списки загружены: gambling=%d, scam/phishing=%d, openphish=%d", len(sets["gambling"]), len(sets["scam"]), len(self.phish_urls))

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 (SpamGuard link checker)"})
        return self._session

    async def refresh_lists(self) -> None:
        """Скачивает списки (≈25 МБ) во временные файлы и атомарно подменяет. Ошибка одного источника не ломает остальные."""
        self.dir.mkdir(parents=True, exist_ok=True)
        s = await self._session_get()
        for name, (url, _) in {**LIST_SOURCES, "openphish": (OPENPHISH_URL, "scam")}.items():
            tmp = self.dir / f"{name}.txt.tmp"
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=180), allow_redirects=True) as r:
                    if r.status != 200:
                        log.warning("список %s: HTTP %s", name, r.status)
                        continue
                    with open(tmp, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            f.write(chunk)
                tmp.replace(self.dir / f"{name}.txt")
            except Exception as ex:
                log.warning("список %s не обновился: %s", name, ex)
                tmp.unlink(missing_ok=True)
        self.load_from_disk()

    async def refresh_loop(self, every: float = 86400) -> None:
        self.load_from_disk()
        while True:
            try:
                age = time.time() - min((p.stat().st_mtime for p in self.dir.glob("*.txt")), default=0)
                if age > every * 0.9 or not self.sets["gambling"]:
                    await self.refresh_lists()
            except Exception:
                log.exception("обновление списков")
            await asyncio.sleep(3600)

    def lookup(self, host: str) -> str | None:
        """Проверяет домен и его родителей: sub.bad.com найдётся по bad.com. Возвращает категорию."""
        labels = host.split(".")
        for i in range(len(labels) - 1):
            d = ".".join(labels[i:])
            for cat in ("scam", "gambling"):
                if d in self.sets[cat]:
                    return cat
        return None

    # ------------------------------------------------------------------ сеть (только сокращатели и RDAP)
    async def expand(self, url: str, hops: int = 3) -> str:
        cur = url
        for _ in range(hops):
            host = (urllib.parse.urlparse(cur).hostname or "").lower()
            if host not in SHORTENERS:
                return cur
            cached = self._expanded.get(cur)
            if cached and time.time() - cached[0] < 86400:
                cur = cached[1]
                continue
            try:
                s = await self._session_get()
                async with s.get(cur, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)) as r:
                    loc = r.headers.get("Location")
                    if r.status in (301, 302, 303, 307, 308) and loc:
                        nxt = urllib.parse.urljoin(cur, loc)
                        self._expanded[cur] = (time.time(), nxt)
                        cur = nxt
                        continue
            except Exception:
                pass
            return cur
        return cur

    async def domain_age_days(self, reg_domain: str) -> int | None:
        if not self.rdap:
            return None
        hit = self._age.get(reg_domain)
        if hit and time.time() - hit[0] < 7 * 86400:
            return hit[1]
        age = None
        try:
            async with self._rdap_sem:
                s = await self._session_get()
                async with s.get(f"https://rdap.org/domain/{reg_domain}", timeout=aiohttp.ClientTimeout(total=5)) as r:
                    if r.status == 200:
                        data = await r.json(content_type=None)
                        for ev in data.get("events", []):
                            if ev.get("eventAction") == "registration":
                                dt = datetime.fromisoformat(ev["eventDate"].replace("Z", "+00:00"))
                                age = max(0, (datetime.now(timezone.utc) - dt).days)
                                break
        except Exception:
            pass
        if len(self._age) > 5000:
            self._age.clear()
        self._age[reg_domain] = (time.time(), age)
        return age

    # ------------------------------------------------------------------ анализ
    def _heuristics(self, url: str, host: str, ext) -> tuple[list[float], float, list[str], str | None]:
        strong: list[float] = []
        weak = 0.0
        reasons: list[str] = []
        label: str | None = None
        parsed = urllib.parse.urlparse(url)
        reg = ext.top_domain_under_public_suffix if hasattr(ext, "top_domain_under_public_suffix") else ext.registered_domain

        # Telegram-ссылки: смотрим на имя канала/бота
        if host in TG_HOSTS:
            seg = parsed.path.strip("/").split("/")[0]
            if seg.startswith("+") or seg == "joinchat":
                weak += 0.05
                reasons.append("приглашение в чат")
            elif seg:
                r, lab, _ = rule_score(normalize(seg.replace("_", " ")))
                if r > 0:
                    strong.append(0.8 * r)
                    reasons.append(f"имя в t.me похоже на «{lab}»")
                    label = lab
            return strong, weak, reasons, label

        for brand, official in BRANDS.items():
            if reg in official or any(reg.endswith("." + o) or reg == o for o in official):
                break
            lab = ext.domain.lower()
            near = len(lab) >= 5 and any(_lev(lab, b) <= 1 for b in ([brand] + [o.split(".")[0] for o in official]))
            sub_hit = brand in ext.subdomain.lower() and reg not in official
            if brand in lab or near or sub_hit:
                strong.append(0.85)
                reasons.append(f"маскируется под «{brand}»")
                label = "scam"
                break

        if "xn--" in host:
            weak += 0.25
            reasons.append("домен с необычными символами")
        try:
            ipaddress.ip_address(host)
            weak += 0.35
            reasons.append("ссылка на IP-адрес")
        except ValueError:
            pass
        if "@" in parsed.netloc:
            weak += 0.4
            reasons.append("«@» в адресе")
        if ext.subdomain.count(".") >= 3:
            weak += 0.10
            reasons.append("слишком много поддоменов")
        if ext.domain.count("-") >= 3:
            weak += 0.10
            reasons.append("много дефисов в домене")
        if ext.suffix.split(".")[-1] in SUSPICIOUS_TLDS:
            weak += 0.12
            reasons.append(f"зона .{ext.suffix}")
        if reg in FREE_HOSTING or any(host.endswith("." + h) for h in FREE_HOSTING):
            weak += 0.10
            reasons.append("бесплатный хостинг")
        if host in SHORTENERS:
            weak += 0.05
            reasons.append("сокращённая ссылка")
        pq = (parsed.path + " " + parsed.query).lower()
        if PATH_WORDS.search(pq):
            weak += 0.10
            reasons.append("подозрительные слова в адресе")
        r, lab, _ = rule_score(normalize(re.sub(r"[/_\-.=&?]", " ", pq)))
        if r > 0:
            strong.append(0.5 * r)
            reasons.append(f"в адресе слова про «{lab}»")
            label = label or lab
        return strong, weak, reasons, label

    async def check(self, url: str, _depth: int = 0) -> LinkResult:
        deob = deobfuscate(url)
        parsed = urllib.parse.urlparse(deob if "://" in deob else "http://" + deob)
        host = (parsed.hostname or "").lower().strip(".")
        res = LinkResult(url=url, host=host)
        if not host:
            return res
        ext = _EXT(host)
        reg = ".".join(p for p in (ext.domain, ext.suffix) if p)

        # 1) точное совпадение с фидом фишинга
        if deob.lower().rstrip("/") in self.phish_urls:
            return LinkResult(url, host, 0.97, "scam", ["адрес есть в базе фишинга (OpenPhish)"])
        # 2) известно хорошие домены (но не сокращатели)
        if host not in SHORTENERS and (reg in ALLOWLIST or host in ALLOWLIST):
            return res
        # 3) сокращённые ссылки: раскрываем и проверяем конечный адрес
        if host in SHORTENERS and _depth < 2:
            final = await self.expand(deob)
            if final != deob:
                inner = await self.check(final, _depth + 1)
                inner.url = url
                inner.reasons.insert(0, f"сокращённая ссылка ведёт на {inner.host}")
                inner.risk = max(inner.risk, 0.1)
                return inner
        # 4) списки доменов
        cat = self.lookup(host)
        if cat:
            return LinkResult(url, host, 0.92 if cat == "gambling" else 0.95, cat,
                              ["домен в базе казино/ставок" if cat == "gambling" else "домен в базе фишинга/мошенников"])
        # 5) эвристики и возраст домена
        strong, weak, reasons, label = self._heuristics(deob, host, ext)
        if reg and not strong and weak < 0.9 and not host.replace(".", "").isdigit():
            age = await self.domain_age_days(reg)
            if age is not None:
                if age < 30:
                    weak += 0.30
                    reasons.append(f"домен зарегистрирован {age} дн. назад")
                elif age < 90:
                    weak += 0.12
                    reasons.append(f"домен новый ({age} дн.)")
        res.risk = _noisy_or(strong + [min(weak, 0.65)])
        res.reasons = reasons
        res.label = (label or "scam") if res.risk >= 0.3 else "ok"
        return res

    async def check_text(self, text: str, extra: list[str] | None = None) -> TextLinks:
        urls = extract_urls(text, extra)
        if not urls:
            return TextLinks()
        try:
            results = await asyncio.wait_for(asyncio.gather(*(self.check(u) for u in urls)), timeout=12)
        except asyncio.TimeoutError:
            return TextLinks()
        top = max(results, key=lambda r: r.risk)
        return TextLinks(risk=_noisy_or(r.risk for r in results), label=top.label if top.risk >= 0.3 else "ok", results=list(results))

    async def close(self) -> None:
        if self._session:
            await self._session.close()
