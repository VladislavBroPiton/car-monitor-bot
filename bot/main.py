import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, Request, Response
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

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(handlers_router)


def create_app() -> FastAPI:
    app = FastAPI(title="Car Monitor Bot")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    scheduler_router = create_scheduler_router(bot)
    app.include_router(scheduler_router)

    @app.post(WEBHOOK_PATH)
    async def telegram_webhook(request: Request) -> Response:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret != WEBHOOK_SECRET:
            return Response(status_code=403)
        body = await request.json()
        update = Update.model_validate(body)
        await dp.feed_update(bot=bot, update=update)
        return Response(status_code=200)

    @app.get("/set_webhook")
    async def set_webhook_manually():
        """Ручная установка webhook — открыть в браузере если бот не отвечает."""
        webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False,
        )
        info = await bot.get_webhook_info()
        return {"webhook_url": webhook_url, "telegram_confirms": info.url}

    @app.on_event("startup")
    async def on_startup():
        await get_pool()
        logger.info("БД: пул создан")

        webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False,
        )
        logger.info(f"Webhook установлен: {webhook_url}")

        try:
            await bot.send_message(
                chat_id=OWNER_ID,
                text="🟢 <b>Car Monitor Bot запущен</b>\n\n/filters — настройка поиска",
            )
        except Exception as e:
            logger.warning(f"Стартовое сообщение не отправлено: {e}")

    @app.on_event("shutdown")
    async def on_shutdown():
        # НЕ удаляем webhook при shutdown — иначе при рестарте Render
        # webhook сбрасывается и бот перестаёт получать апдейты
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
