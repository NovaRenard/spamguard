"""Живая проверка ссылок на сервере (внутри контейнера): настоящие списки и RDAP. Ничего не меняет."""
import asyncio
import sys

sys.path.insert(0, "/app")
from links import LinkChecker  # noqa: E402


async def main():
    c = LinkChecker("/data/lists", rdap=True)
    c.load_from_disk()
    g = next(iter(sorted(c.sets["gambling"])[100:101]))
    p = next(iter(sorted(c.sets["scam"])[100:101]))
    tests = [
        ("обычный сайт", "https://example.com/page"),
        ("Google Docs (безопасный)", "https://docs.google.com/document/d/1"),
        ("настоящий kaspi.kz", "https://kaspi.kz/shop"),
        ("поддельный Kaspi", "http://kaspi-secure-pay.top/login"),
        ("бренд в поддомене", "http://kaspi.kz.verify-login.xyz/"),
        ("домен казино из списка", f"http://{g}/promo"),
        ("домен фишинга из списка", f"http://{p}/login"),
        ("t.me с именем казино", "https://t.me/vavada_bonus_kz"),
        ("обычный t.me", "https://t.me/durov"),
        ("IP-адрес", "http://185.12.3.4/login"),
        ("обфускация", "1win[.]casino/reg"),
    ]
    for name, u in tests:
        tl = await c.check_text(u)
        r = tl.results[0] if tl.results else None
        print(f"{name:28} -> риск {tl.risk:4.0%} {tl.label:8} {'; '.join(r.reasons)[:70] if r else 'ссылка не найдена'}")
    await c.close()


asyncio.run(main())
