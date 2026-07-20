import asyncio
import logging
from aiogram import Bot
from fastapi import APIRouter, Header, HTTPException

from config import WEBHOOK_SECRET
from db.repository import get_all_active_filters, cleanup_old_listings
from parsers.base import SearchFilter
from parsers.autoru import AutoRuParser
from parsers.drom import DromParser
from notifier import process_listings

logger = logging.getLogger(__name__)

autoru_parser = AutoRuParser()
drom_parser = DromParser()


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
            autoru_results, drom_results = await asyncio.gather(
                autoru_parser.search(f),
                drom_parser.search(f),
                return_exceptions=True,
            )

            all_listings = []

            if isinstance(autoru_results, Exception):
                logger.error(f"autoru ошибка «{f.name}»: {autoru_results}")
            else:
                all_listings.extend(autoru_results)

            if isinstance(drom_results, Exception):
                logger.error(f"drom ошибка «{f.name}»: {drom_results}")
            else:
                all_listings.extend(drom_results)

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

    try:
        await cleanup_old_listings(days=30)
    except Exception as e:
        logger.warning(f"scheduler: ошибка очистки: {e}")

    logger.info(f"scheduler: завершён, новых: {total_new}")
    return {"status": "ok", "filters": len(filters), "new_listings": total_new}


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

    @router.get("/debug/drom")
    async def debug_drom():
        import aiohttp
        from bs4 import BeautifulSoup
        url = "https://auto.drom.ru/region34/chevrolet/cruze/?minyear=2015&maxyear=2024&minprice=500000&maxprice=1500000&order=date_add"
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://www.drom.ru/",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                status = resp.status
                html = await resp.text() if resp.status == 200 else ""
        if not html:
            return {"status": status, "html_length": 0}
        soup = BeautifulSoup(html, "html.parser")
        selectors = {
            "data-ftid=bulls-list_bull": len(soup.select("[data-ftid='bulls-list_bull']")),
            "div.bull-list-item-v2": len(soup.select("div.bull-list-item-v2")),
            "div[data-bull-id]": len(soup.select("div[data-bull-id]")),
            "article": len(soup.select("article")),
            "data-ftid_any": len(soup.select("[data-ftid]")),
        }
        ftid_vals = list({el.get("data-ftid") for el in soup.select("[data-ftid]") if el.get("data-ftid")})[:30]
        return {"status": status, "url": url, "html_length": len(html), "selectors": selectors, "ftid_values": ftid_vals}

    return router
