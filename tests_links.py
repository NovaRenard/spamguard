"""Тесты проверки ссылок:  python tests_links.py          (быстро, без сети)
                          python tests_links.py --net    (+ реальные списки и RDAP, нужна сеть, ~25 МБ)"""
import asyncio
import sys
import tempfile
import time

from links import LinkChecker, extract_urls

FAILS: list[str] = []


def check(name, cond, extra=""):
    print(f"  {'✔' if cond else '✘ ПРОВАЛ'} {name} {extra}")
    if not cond:
        FAILS.append(name)


class Fake(LinkChecker):
    """Без сети: раскрытие и возраст домена подменены."""
    def __init__(self):
        super().__init__(rdap=False)
        self.sets = {"gambling": {"1win-bonus.com", "vavada.casino"}, "scam": {"evil-phish.net"}}
        self.phish_urls = {"http://phish.example/login"}
        self.short = {"https://bit.ly/abc": "http://1win-bonus.com/reg", "https://bit.ly/ok": "https://example.com/doc"}
        self.ages: dict[str, int] = {}
        self.expand_calls = 0

    async def expand(self, url, hops=3):
        self.expand_calls += 1
        return self.short.get(url, url)

    async def domain_age_days(self, reg):
        return self.ages.get(reg)


async def main():
    print("\n[Извлечение ссылок]")
    u = extract_urls("Заходи на 1win.casino и kaspi-secure.top/login, файл report.txt, т.д., t.me/vavada_kz, 1win[.]com, hxxps://evil.xyz/a?b=1.")
    check("находит обычные, голые домены и t.me", {"http://1win.casino", "http://kaspi-secure.top/login", "http://t.me/vavada_kz"} <= set(u), str(u))
    check("снимает обфускацию [.] и hxxps", "http://1win.com" in u and "https://evil.xyz/a?b=1" in u)
    check("не принимает report.txt и «т.д.» за ссылки", not any("report" in x or "т.д" in x for x in u))
    check("скрытая ссылка из разметки добавляется", "http://hidden.top" in extract_urls("кликни сюда", ["http://hidden.top"]))
    check("не больше 5 ссылок на сообщение", len(extract_urls(" ".join(f"a{i}.com" for i in range(20)))) == 5)
    check("пустой текст", extract_urls("") == [] and extract_urls("просто привет, как дела?") == [])

    print("\n[Списки и репутация]")
    c = Fake()
    r = await c.check("https://sub.1win-bonus.com/reg"); check("домен в базе казино (через поддомен)", r.label == "gambling" and r.risk >= 0.9, str(r.reasons))
    r = await c.check("http://evil-phish.net/x"); check("домен в базе фишинга", r.label == "scam" and r.risk >= 0.9)
    r = await c.check("http://phish.example/login"); check("точный адрес из фида OpenPhish", r.risk >= 0.97)
    r = await c.check("https://docs.google.com/document/d/1"); check("известный безопасный домен: тишина", r.risk == 0)
    r = await c.check("https://example.com/page"); check("обычный неизвестный сайт: риск 0", r.risk == 0, str(r.reasons))

    print("\n[Маскировка и эвристики]")
    r = await c.check("http://kaspi-secure.top/login"); check("kaspi-secure.top: маскировка под Kaspi", r.risk >= 0.85 and r.label == "scam", str(r.reasons))
    r = await c.check("http://kasp1.kz/pay"); check("kasp1.kz: опечатка в бренде", r.risk >= 0.85, str(r.reasons))
    r = await c.check("http://kaspi.kz.verify-login.top/"); check("бренд в поддомене чужого домена", r.risk >= 0.85, str(r.reasons))
    r = await c.check("https://kaspi.kz/shop"); check("настоящий kaspi.kz не трогаем", r.risk == 0)
    r = await c.check("http://185.12.3.4/login"); check("IP-адрес + login: подозрительно", 0.3 <= r.risk < 0.85, f"{r.risk:.2f} {r.reasons}")
    r = await c.check("http://xn--80ak6aa92e.com/"); check("только punycode: ниже порога предупреждения", r.risk < 0.5, f"{r.risk:.2f}")
    r = await c.check("http://paypal.com@evil.top/"); check("«@» в адресе: опасно", r.risk >= 0.5, f"{r.risk:.2f} {r.reasons}")
    r = await c.check("http://t.me/vavada_kz"); check("t.me с именем казино", r.label == "gambling" and r.risk >= 0.6, str(r.reasons))
    r = await c.check("http://t.me/durov"); check("обычный t.me канал: тишина", r.risk == 0)
    r = await c.check("http://t.me/+AbCdEf123"); check("приглашение в чат: слабый сигнал", 0 < r.risk < 0.3)

    print("\n[Сокращённые ссылки и возраст домена]")
    r = await c.check("https://bit.ly/abc"); check("bit.ly раскрыт: цель из базы казино", r.label == "gambling" and r.risk >= 0.9 and "ведёт на" in r.reasons[0], str(r.reasons))
    r = await c.check("https://bit.ly/ok"); check("bit.ly на нормальный сайт: низкий риск", r.risk < 0.3)
    c.ages["fresh-site.top"] = 5
    r = await c.check("http://fresh-site.top/"); check("домен 5 дней + зона .top: заметный риск", 0.3 <= r.risk < 0.85, f"{r.risk:.2f} {r.reasons}")
    c.ages["old-site.top"] = 4000
    r = await c.check("http://old-site.top/"); check("старый домен: нет добавки за возраст", r.risk < 0.3)

    print("\n[Текст целиком]")
    tl = await c.check_text("Привет! Вот ссылка: https://example.com/page и всё")
    check("безобидные ссылки: риск 0", tl.risk == 0)
    tl = await c.check_text("Лови бонус 1win-bonus[.]com/reg")
    check("обфусцированная ссылка найдена и опознана", tl.label == "gambling" and tl.risk >= 0.9, str(tl.reasons))
    tl = await c.check_text("кликни сюда", ["http://evil-phish.net/x"])
    check("скрытая ссылка из разметки проверяется", tl.label == "scam" and tl.risk >= 0.9)
    tl = await c.check_text("просто текст без ссылок")
    check("без ссылок: нулевая нагрузка", tl.risk == 0 and tl.results == [])
    n = c.expand_calls
    await c.check_text("тут нет коротких ссылок: example.com")
    check("раскрытие вызывается только для сокращателей", c.expand_calls == n)

    if "--net" in sys.argv:
        print("\n[Сеть: реальные списки и RDAP]")
        with tempfile.TemporaryDirectory() as d:
            real = LinkChecker(data_dir=d, rdap=True)
            t = time.time(); await real.refresh_lists()
            g, s = real.sets["gambling"], real.sets["scam"]
            print(f"     загружено за {time.time() - t:.0f} с: казино={len(g)}, фишинг/мошенничество={len(s)}, OpenPhish={len(real.phish_urls)}")
            check("списки загружены", len(g) > 100000 and len(s) > 50000 and len(real.phish_urls) > 50)
            sample = next(iter(g))
            check("домен из списка казино опознан", real.lookup(sample) == "gambling" and real.lookup("sub." + sample) == "gambling")
            check("популярные сайты не в списках", all(real.lookup(x) is None for x in ("google.com", "wikipedia.org", "github.com", "kaspi.kz", "t.me")))
            t = time.time()
            for _ in range(20000):
                real.lookup("some.unknown-domain-example.org")
            per = (time.time() - t) / 20000 * 1e6
            check("поиск быстрый (микросекунды)", per < 100, f"{per:.1f} мкс")
            r = await real.check(f"http://{sample}/promo"); check("реальный домен казино -> gambling", r.label == "gambling")
            age = await real.domain_age_days("example.com"); check("RDAP: возраст example.com > 1 года", age is not None and age > 365, str(age))
            check("RDAP: несуществующий домен -> None без ошибки", await real.domain_age_days("zz-no-such-domain-12345.com") is None)
            await real.close()

    print("\n" + ("ВСЕ ТЕСТЫ ПРОШЛИ" if not FAILS else f"ПРОВАЛЕНО: {len(FAILS)} -> {FAILS}"))
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
