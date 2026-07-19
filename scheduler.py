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

    return router
