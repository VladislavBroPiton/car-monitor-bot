import asyncio
import logging
from typing import Optional
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import datetime
from config import OWNER_ID, USD_RUB_RATE
from parsers.base import Listing
from parsers.copart import damage_ru
from db.repository import mark_seen, record_price, get_notification_settings

logger = logging.getLogger(__name__)

SEND_DELAY = 0.5

SOURCE_BADGE = {
    "autoru": "🔵 Auto.ru",
    "drom":   "🟠 Дром.ру",
    "avito":  "🟢 Авито",
    "copart": "🟡 Copart",
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


THIN_SPACE = " "
CURRENCY_SIGN = {"USD": "$", "CAD": "CA$"}


def _fmt_amount(value: Optional[int], currency: Optional[str]) -> str:
    """Оценочная стоимость лота Copart в валюте торгов."""
    if not value:
        return "оценка не указана"
    sign = CURRENCY_SIGN.get(currency or "USD", f"{currency} ")
    return sign + f"{value:,}".replace(",", THIN_SPACE)


def _fmt_miles(value: Optional[int]) -> str:
    """Одометр Copart — в милях."""
    if not value:
        return ""
    return f"{value:,}".replace(",", THIN_SPACE) + " миль"


def _fmt_auction_date(value) -> str:
    """Дата торгов в московском времени."""
    if not value:
        return ""
    return (value + datetime.timedelta(hours=3)).strftime("%d.%m.%Y в %H:%M МСК")


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
    badge     = SOURCE_BADGE.get(listing.source, listing.source)
    is_copart = listing.source == "copart"
    price     = (_fmt_amount(listing.price, listing.currency) if is_copart
                 else _fmt_price(listing.price))

    # ── Строка характеристик ──────────────────────────────────────────────────
    specs: list[str] = []
    if listing.year:
        specs.append(f"{listing.year} г.")
    if listing.mileage:
        specs.append(_fmt_miles(listing.mileage) if is_copart
                     else _fmt_mileage(listing.mileage))
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

    # Номер лота — по нему ищут на самом аукционе
    if is_copart:
        lines.append(f"<code>Лот {listing.external_id}</code>")

    # Цена — главный акцент
    label = "💰 Оценка:" if is_copart else "💰"
    lines.append(f"\n<b>{label} {price}</b>")

    # Характеристики
    if specs:
        lines.append("📋 " + "  ·  ".join(specs))

    # Повреждения и дата торгов
    if is_copart:
        if listing.damage_description:
            lines.append(f"💥 Повреждение: {damage_ru(listing.damage_description)}")
        auction = _fmt_auction_date(listing.auction_date)
        if auction:
            lines.append(f"🗓 Аукцион: {auction}")

    # Город (для Copart — площадка хранения)
    if listing.city:
        icon = "🏁" if is_copart else "📍"
        lines.append(f"{icon} {listing.city}")

    # Фильтр
    if listing.filter_name:
        lines.append(f"\n<i>🔍 Фильтр: {listing.filter_name}</i>")

    return "\n".join(lines)


def _build_keyboard(listing: Listing) -> InlineKeyboardMarkup:
    """Кнопки прямо под объявлением."""
    # callback_data ограничен 64 байтами — берём только первые 20 символов external_id
    short_id = listing.external_id[:20]
    open_text = "🔗 Открыть лот" if listing.source == "copart" else "🔗 Открыть объявление"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=open_text,
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
        # Проверяем порог цены. Порог задаётся в рублях, а лоты Copart —
        # в долларах, поэтому приводим порог к валюте лота.
        if threshold and listing.price:
            limit = threshold / USD_RUB_RATE if listing.source == "copart" else threshold
            if listing.price > limit:
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
            damage_description=listing.damage_description,
            auction_date=listing.auction_date,
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
            if listing.source == "copart":
                was  = _fmt_amount(old_price, listing.currency)
                now  = _fmt_amount(listing.price, listing.currency)
                diff = _fmt_amount(drop, listing.currency)
                text = (
                    f"📉 <b>Оценка снижена!</b>\n"
                    f"{listing.title}\n\n"
                    f"Было: <s>{was}</s>\n"
                    f"Стало: <b>{now}</b> (-{diff} / -{pct}%)\n\n"
                    f'<a href="{listing.url}">Открыть лот →</a>'
                )
            else:
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
