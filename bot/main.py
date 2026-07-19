import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config import (
    BOT_TOKEN,
    OWNER_ID,
    WEBHOOK_HOST,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    APP_HOST,
    APP_PORT,
)
from bot.handlers import router as handlers_router
from scheduler import create_scheduler_router
from db.repository import get_pool, close_pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(handlers_router)

    app = FastAPI(title="Car Monitor Bot")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    scheduler_router = create_scheduler_router(bot)
    app.include_router(scheduler_router)

    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)

    @app.on_event("startup")
    async def on_startup():
        await get_pool()
        logger.info("БД: пул создан")

        webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook: {webhook_url}")

        try:
            await bot.send_message(
                chat_id=OWNER_ID,
                text="🟢 <b>Car Monitor Bot запущен</b>\n\n/filters — настройка поиска",
            )
        except Exception as e:
            logger.warning(f"Стартовое сообщение не отправлено: {e}")

    @app.on_event("shutdown")
    async def on_shutdown():
        await bot.delete_webhook()
        await close_pool()
        await bot.session.close()
        logger.info("Бот остановлен")

    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        "bot.main:app",
        host=APP_HOST,
        port=APP_PORT,
        log_level="info",
    )
