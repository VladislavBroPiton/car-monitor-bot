import asyncio
import logging
from typing import Optional
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import OWNER_ID
from parsers.base import Listing
from db.repository import mark_seen

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


async def process_listings(
    bot: Bot,
    listings: list[Listing],
    chat_id: int = OWNER_ID,
) -> int:
    new_count = 0
    for listing in listings:
        is_new = await mark_seen(
            source=listing.source,
            external_id=listing.external_id,
            url=listing.url,
            title=listing.title,
            price=listing.price,
            year=listing.year,
            mileage=listing.mileage,
            city=listing.city,
        )
        if not is_new:
            continue
        new_count += 1
        await send_listing(bot, listing, chat_id=chat_id)
        await asyncio.sleep(SEND_DELAY)
    return new_count
