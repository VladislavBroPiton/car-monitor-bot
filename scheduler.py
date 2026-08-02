import asyncio
import logging
from aiogram import Bot
from fastapi import APIRouter, Header, HTTPException

from config import WEBHOOK_SECRET
from db.repository import get_all_active_filters, cleanup_old_listings
from parsers.base import SearchFilter
from parsers.autoru import AutoRuParser
from parsers.drom import DromParser
from parsers.avito import AvitoParser
from parsers.copart import CopartParser
from notifier import process_listings, notify_upcoming_auctions

logger = logging.getLogger(__name__)

autoru_parser = AutoRuParser()
drom_parser = DromParser()
avito_parser = AvitoParser()
copart_parser = CopartParser()


async def run_parsers(bot: Bot) -> dict:
    records = await get_all_active_filters()

    if not records:
        logger.info("scheduler: нет активных фильтров")
        return {"status": "ok", "filters": 0, "new_listings": 0}

    filters = [SearchFilter.from_record(r) for r in records]
    logger.info(f"scheduler: фильтров: {len(filters)}")

    total_new = 0

    for f in filters:
        try:
            logger.info(f"scheduler: фильтр «{f.name}» sources={f.sources}")
            parsers = [
                ("autoru", autoru_parser),
                ("drom",   drom_parser),
                ("avito",  avito_parser),
                ("copart", copart_parser),
            ]
            results = await asyncio.gather(
                *(p.search(f) for _, p in parsers),
                return_exceptions=True,
            )

            all_listings = []

            for (source, _), result in zip(parsers, results):
                if isinstance(result, Exception):
                    logger.error(f"{source} ошибка «{f.name}»: {result}")
                else:
                    all_listings.extend(result)

            if all_listings:
                new_count = await process_listings(
                    bot=bot,
                    listings=all_listings,
                    chat_id=f.user_id,
                )
                total_new += new_count
                logger.info(
                    f"scheduler: «{f.name}» — "
                    f"всего {len(all_listings)}, новых: {new_count}"
                )

        except Exception as e:
            logger.error(f"scheduler: ошибка фильтра «{f.name}»: {e}")
            continue

    # Напоминаем о торгах, которые вот-вот начнутся
    reminders = 0
    try:
        reminders = await notify_upcoming_auctions(bot)
    except Exception as e:
        logger.warning(f"scheduler: ошибка напоминаний: {e}")

    try:
        await cleanup_old_listings(days=30)
    except Exception as e:
        logger.warning(f"scheduler: ошибка очистки: {e}")

    logger.info(f"scheduler: завершён, новых: {total_new}, напоминаний: {reminders}")
    return {
        "status": "ok",
        "filters": len(filters),
        "new_listings": total_new,
        "reminders": reminders,
    }


def create_scheduler_router(bot: Bot) -> APIRouter:
    router = APIRouter()

    @router.post("/run")
    async def run_endpoint(x_secret: str = Header(default="")):
        """
        Endpoint для cron-job.org.
        Заголовок: X-Secret: <WEBHOOK_SECRET>
        """
        if x_secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")
        return await run_parsers(bot)

    @router.get("/")
    @router.head("/")
    async def root():
        return {"status": "ok"}

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    @router.get("/run_now")
    async def run_now():
        """Запустить парсер вручную через браузер (без авторизации — только для тестов)."""
        result = await run_parsers(bot)
        return result

    @router.get("/debug/drom")
    async def debug_drom():
        import asyncio, random, aiohttp
        from bs4 import BeautifulSoup
        url = "https://auto.drom.ru/region34/chevrolet/cruze/?minyear=2015&maxyear=2024&minprice=500000&maxprice=1500000&order=date_add"
        await asyncio.sleep(random.uniform(2, 4))
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.drom.ru/",
            "Upgrade-Insecure-Requests": "1",
        }
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=30), allow_redirects=True) as resp:
                status = resp.status
                html = await resp.text() if resp.status == 200 else ""
        if not html:
            return {"status": status, "html_length": 0, "note": "429=rate limit, попробуй через 5 мин"}
        soup = BeautifulSoup(html, "html.parser")
        selectors = {
            "data-ftid=bulls-list_bull": len(soup.select("[data-ftid='bulls-list_bull']")),
            "div.bull-list-item-v2": len(soup.select("div.bull-list-item-v2")),
            "div[data-bull-id]": len(soup.select("div[data-bull-id]")),
            "article": len(soup.select("article")),
            "data-ftid_any": len(soup.select("[data-ftid]")),
        }
        ftid_vals = list({el.get("data-ftid") for el in soup.select("[data-ftid]") if el.get("data-ftid")})[:30]
        return {"status": status, "html_length": len(html), "selectors": selectors, "ftid_values": ftid_vals}

    return router
