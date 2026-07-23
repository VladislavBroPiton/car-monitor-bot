import asyncio
import logging
from typing import Optional
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import datetime
from config import OWNER_ID
from parsers.base import Listing
from db.repository import mark_seen, record_price, get_notification_settings

logger = logging.getLogger(__name__)

SEND_DELAY = 0.5

SOURCE_BADGE = {
    "autoru": "🔵 Auto.ru",
    "drom":   "🟠 Дром.ру",
}

TRANSMISSION_RU = {
    "AUTOMATIC": "Автомат",
    "MECHANICAL": "Механика",
    "ROBOT":      "Робот",
    "VARIATOR":   "Вариатор",
    "AUTO":       "Автомат",
}

BODY_RU = {
    "SEDAN":    "Седан",
    "SUV":      "Внедорожник",
    "HATCHBACK":"Хэтчбек",
    "WAGON":    "Универсал",
    "COUPE":    "Купе",
    "MINIVAN":  "Минивэн",
    "PICKUP":   "Пикап",
    "VAN":      "Фургон",
}


def _fmt_price(price: Optional[int]) -> str:
    if not price:
        return "цена не указана"
    return f"{price:,}".replace(",", "\u2009") + " ₽"   # тонкий пробел как разделитель


def _fmt_mileage(mileage: Optional[int]) -> str:
    if not mileage:
        return ""
    return f"{mileage:,}".replace(",", "\u2009") + " км"


def _fmt_transmission(value: Optional[str]) -> str:
    if not value:
        return ""
    return TRANSMISSION_RU.get(value.upper(), value)


def _fmt_body(value: Optional[str]) -> str:
    if not value:
        return ""
    return BODY_RU.get(value.upper(), value)


def _build_message(listing: Listing) -> str:
    badge   = SOURCE_BADGE.get(listing.source, listing.source)
    price   = _fmt_price(listing.price)

    # ── Строка характеристик ──────────────────────────────────────────────────
    specs: list[str] = []
    if listing.year:
        specs.append(f"{listing.year} г.")
    if listing.mileage:
        specs.append(_fmt_mileage(listing.mileage))
    tr = _fmt_transmission(listing.transmission)
    if tr:
        specs.append(tr)
    bt = _fmt_body(listing.body_type)
    if bt:
        specs.append(bt)

    # ── Сборка сообщения ──────────────────────────────────────────────────────
    lines: list[str] = []

    # Шапка: источник + разделитель
    lines.append(f"{badge}")
    lines.append("┄" * 18)

    # Название
    lines.append(f"<b>{listing.title}</b>")

    # Цена — главный акцент
    lines.append(f"\n<b>💰 {price}</b>")

    # Характеристики
    if specs:
        lines.append("📋 " + "  ·  ".join(specs))

    # Город
    if listing.city:
        lines.append(f"📍 {listing.city}")

    # Фильтр
    if listing.filter_name:
        lines.append(f"\n<i>🔍 Фильтр: {listing.filter_name}</i>")

    return "\n".join(lines)


def _build_keyboard(listing: Listing) -> InlineKeyboardMarkup:
    """Кнопки прямо под объявлением."""
    # callback_data ограничен 64 байтами — берём только первые 20 символов external_id
    short_id = listing.external_id[:20]
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🔗 Открыть объявление",
                url=listing.url,
            ),
        ],
        [
            InlineKeyboardButton(
                text="⭐️ В избранное",
                callback_data=f"fav:{listing.source}:{short_id}",
            ),
            InlineKeyboardButton(
                text="🚫 Скрыть",
                callback_data=f"hide:{listing.source}:{short_id}",
            ),
        ],
    ])


async def send_listing(bot: Bot, listing: Listing, chat_id: int = OWNER_ID):
    text = _build_message(listing)
    kb   = _build_keyboard(listing)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"notifier: ошибка отправки: {e}")


def _is_quiet_hours(quiet_from: int, quiet_to: int) -> bool:
    """Проверяем тихие часы (московское время UTC+3)."""
    hour = (datetime.datetime.utcnow().hour + 3) % 24
    if quiet_from > quiet_to:  # например 23-8
        return hour >= quiet_from or hour < quiet_to
    return quiet_from <= hour < quiet_to


async def process_listings(
    bot: Bot,
    listings: list[Listing],
    chat_id: int = OWNER_ID,
) -> int:
    new_count = 0
    settings = await get_notification_settings(chat_id)
    quiet = _is_quiet_hours(settings.get("quiet_from", 23), settings.get("quiet_to", 8))
    threshold = settings.get("price_threshold")

    for listing in listings:
        # Проверяем порог цены
        if threshold and listing.price and listing.price > threshold:
            continue

        is_new = await mark_seen(
            source=listing.source,
            external_id=listing.external_id,
            url=listing.url,
            title=listing.title,
            price=listing.price,
            year=listing.year,
            mileage=listing.mileage,
            city=listing.city,
            transmission=listing.transmission,
        )

        # Записываем цену в историю
        if listing.price:
            await record_price(listing.source, listing.external_id, listing.price)

        if not is_new:
            continue

        new_count += 1

        # В тихие часы не отправляем (но считаем)
        if quiet:
            continue

        await send_listing(bot, listing, chat_id=chat_id)
        await asyncio.sleep(SEND_DELAY)

    return new_count


async def process_price_drops(
    bot: Bot,
    listings: list[Listing],
    chat_id: int = OWNER_ID,
):
    """Проверяем снижение цены на уже виденные объявления."""
    settings = await get_notification_settings(chat_id)
    if not settings.get("notify_price_drop", True):
        return
    quiet = _is_quiet_hours(settings.get("quiet_from", 23), settings.get("quiet_to", 8))
    if quiet:
        return

    for listing in listings:
        if not listing.price:
            continue
        old_price = await record_price(listing.source, listing.external_id, listing.price)
        if old_price and old_price > listing.price:
            drop = old_price - listing.price
            pct  = round(drop / old_price * 100)
            text = (
                f"📉 <b>Цена снижена!</b>\n"
                f"{listing.title}\n\n"
                f"Было: <s>{old_price:,} ₽</s>\n"
                f"Стало: <b>{listing.price:,} ₽</b> (-{drop:,} ₽ / -{pct}%)\n\n"
                f'<a href="{listing.url}">Открыть →</a>'
            ).replace(",", "\u2009")
            try:
                await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"price drop notify error: {e}")
