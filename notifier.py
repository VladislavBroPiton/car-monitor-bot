import asyncio
import logging
from typing import Optional
from aiogram import Bot
from aiogram.enums import ParseMode

from config import OWNER_ID
from parsers.base import Listing
from db.repository import mark_seen

logger = logging.getLogger(__name__)

SEND_DELAY = 0.5


def _format_transmission(value: Optional[str]) -> str:
    MAP = {
        "AUTOMATIC": "Автомат",
        "MECHANICAL": "Механика",
        "ROBOT": "Робот",
        "VARIATOR": "Вариатор",
        "AUTO": "Автомат",
    }
    if not value:
        return ""
    return MAP.get(value.upper(), value)


def _format_source(source: str) -> str:
    return {"autoru": "Auto.ru", "drom": "Дром.ру"}.get(source, source)


def _format_price(price: Optional[int]) -> str:
    if not price:
        return "цена не указана"
    return f"{price:,}".replace(",", " ") + " ₽"


def _format_mileage(mileage: Optional[int]) -> str:
    if not mileage:
        return ""
    return f"{mileage:,}".replace(",", " ") + " км"


def _build_message(listing: Listing) -> str:
    source_label = _format_source(listing.source)
    price_str = _format_price(listing.price)

    specs_parts = []
    if listing.year:
        specs_parts.append(f"{listing.year} г.")
    if listing.mileage:
        specs_parts.append(_format_mileage(listing.mileage))
    if listing.transmission:
        t = _format_transmission(listing.transmission)
        if t:
            specs_parts.append(t)
    specs_str = " • ".join(specs_parts)

    lines = [
        f"🚗 <b>Новое объявление — {source_label}</b>",
        listing.title,
        "",
        f"💰 {price_str}",
    ]
    if listing.city:
        lines.append(f"📍 {listing.city}")
    if specs_str:
        lines.append(f"📅 {specs_str}")
    if listing.filter_name:
        lines.append(f"🔍 Фильтр: «{listing.filter_name}»")
    lines.append("")
    lines.append(f'<a href="{listing.url}">👁 Открыть объявление →</a>')

    return "\n".join(lines)


async def send_listing(bot: Bot, listing: Listing, chat_id: int = OWNER_ID):
    text = _build_message(listing)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
    except Exception as e:
        logger.error(f"notifier: не удалось отправить сообщение: {e}")


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
