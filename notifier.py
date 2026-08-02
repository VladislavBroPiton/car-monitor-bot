import asyncio
import logging
from typing import Optional
from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import datetime
from config import OWNER_ID, USD_RUB_RATE
from parsers.base import Listing
from parsers.copart import damage_ru, title_ru, keys_ru
from db.repository import (
    mark_seen,
    record_price,
    get_notification_settings,
    get_lots_to_remind,
    set_notify_stage,
    count_relists,
)

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


def _build_message(listing: Listing, relists: int = 0) -> str:
    badge     = SOURCE_BADGE.get(listing.source, listing.source)
    is_copart = listing.source == "copart"
    price     = (_fmt_amount(listing.price, listing.currency) if is_copart
                 else _fmt_price(listing.price))

    # ── Строка характеристик ──────────────────────────────────────────────────
    specs: list[str] = []
    if listing.year:
        specs.append(f"{listing.year} г.")
    if listing.mileage:
        if is_copart:
            miles = _fmt_miles(listing.mileage)
            # NOT ACTUAL — одометр скручен либо показания неизвестны
            if (listing.odometer_brand or "").upper() == "NOT ACTUAL":
                miles += " ⚠️ не подтверждён"
            specs.append(miles)
        else:
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

    # Номер лота — по нему ищут на самом аукционе
    if is_copart:
        lines.append(f"<code>Лот {listing.external_id}</code>")

    # Цена — главный акцент. У лотов «купить сразу» она и есть главная,
    # оценочной стоимости там часто нет вовсе.
    if is_copart and listing.buy_now_price:
        buy_now = _fmt_amount(listing.buy_now_price, listing.currency)
        lines.append(f"\n<b>⚡️ Купить сразу: {buy_now}</b>")
        if listing.price:
            lines.append(f"<i>оценка: {price}</i>")
    else:
        label = "💰 Оценка:" if is_copart else "💰"
        lines.append(f"\n<b>{label} {price}</b>")

    if is_copart and listing.repair_cost:
        lines.append(f"🔧 Ремонт: ~{_fmt_amount(listing.repair_cost, listing.currency)}")

    # Характеристики
    if specs:
        lines.append("📋 " + "  ·  ".join(specs))

    if is_copart and listing.specs:
        lines.append(f"⚙️ {listing.specs}")

    # Состояние лота: документ, ход, ключи, повреждение, торги
    if is_copart:
        state = [s for s in (title_ru(listing.title_group),
                             "🚀 На ходу" if listing.run_and_drive else "",
                             keys_ru(listing.has_keys)) if s]
        if state:
            lines.append("  ·  ".join(state))

        if listing.damage_description:
            lines.append(f"💥 Повреждение: {damage_ru(listing.damage_description)}")

        auction = _fmt_auction_date(listing.auction_date)
        if auction:
            lines.append(f"🗓 Аукцион: {auction}")

    # Город (для Copart — площадка хранения)
    if listing.city:
        icon = "🏁" if is_copart else "📍"
        lines.append(f"{icon} {listing.city}")

    if is_copart and listing.vin:
        lines.append(f"<code>VIN {listing.vin}</code>")

    # Машина уже была на торгах под другим номером — значит, не ушла.
    # Либо цена завышена, либо есть проблема, которой не видно в описании.
    if relists:
        times = "раз" if relists == 1 else "раза" if relists < 5 else "раз"
        lines.append(f"🔁 <b>Выставляется повторно</b> — уже был на торгах "
                     f"{relists} {times}")

    # Фильтр
    if listing.filter_name:
        lines.append(f"\n<i>🔍 Фильтр: {listing.filter_name}</i>")

    return "\n".join(lines)


def _build_keyboard(listing: Listing) -> InlineKeyboardMarkup:
    """Кнопки прямо под объявлением."""
    # callback_data ограничен 64 байтами. По id ищем объявление в seen_listings,
    # поэтому режем по максимуму, а не «на глазок»: "hide:" + источник + ":" ≈ 12
    limit = 64 - len("hide:") - len(listing.source) - 1
    short_id = listing.external_id[:limit]
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
    ] + ([
        [InlineKeyboardButton(
            text="🧮 Сколько выйдет «под ключ»",
            callback_data=f"cost:{short_id}",
        )],
    ] if listing.source == "copart" else []))


CAPTION_LIMIT = 1024   # ограничение Telegram на подпись к фото


async def send_listing(bot: Bot, listing: Listing, chat_id: int = OWNER_ID,
                       relists: int = 0):
    text = _build_message(listing, relists=relists)
    kb   = _build_keyboard(listing)

    # Лот с фотографией отправляем картинкой — по битой машине фото решает всё.
    # Подпись длиннее лимита Telegram не примет, тогда шлём обычным сообщением.
    if listing.image_url and len(text) <= CAPTION_LIMIT:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=listing.image_url,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
            return
        except Exception as e:
            # Картинка может быть недоступна — не теряем из-за этого объявление
            logger.warning(f"notifier: фото не отправилось ({e}), шлю текстом")

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

        is_new = await mark_seen(listing)

        # Записываем цену в историю
        if listing.price:
            await record_price(listing.source, listing.external_id, listing.price)

        if not is_new:
            continue

        new_count += 1

        # В тихие часы не отправляем (но считаем)
        if quiet:
            continue

        # Считаем после mark_seen — текущая запись исключается по external_id
        relists = 0
        if listing.source == "copart" and listing.vin:
            try:
                relists = await count_relists(listing.vin, listing.external_id)
            except Exception as e:
                logger.warning(f"не удалось посчитать повторы по VIN: {e}")

        await send_listing(bot, listing, chat_id=chat_id, relists=relists)
        await asyncio.sleep(SEND_DELAY)

    return new_count


# ── Напоминания о торгах ──────────────────────────────────────────────────────

# стадия → (за сколько часов предупредить, заголовок)
AUCTION_REMINDERS = (
    (1, 24, "🔔 <b>Торги завтра</b>"),
    (2, 1,  "⏰ <b>Торги через час</b>"),
)


async def notify_upcoming_auctions(bot: Bot, chat_id: int = OWNER_ID) -> int:
    """
    Напоминает о лотах Copart, торги по которым скоро начнутся.
    Каждому лоту — не больше одного напоминания на стадию.
    """
    settings = await get_notification_settings(chat_id)
    if _is_quiet_hours(settings.get("quiet_from", 23), settings.get("quiet_to", 8)):
        return 0

    sent = 0
    for stage, hours, header in AUCTION_REMINDERS:
        try:
            rows = await get_lots_to_remind(stage=stage, within_hours=hours)
        except Exception as e:
            logger.error(f"напоминания: ошибка выборки (стадия {stage}): {e}")
            continue

        for row in rows:
            when = _fmt_auction_date(row["auction_date"])
            price = _fmt_amount(row["price"], row["currency"])
            text = (
                f"{header}\n"
                f"<code>Лот {row['external_id']}</code>\n\n"
                f"<b>{row['title'] or 'Лот Copart'}</b>\n"
                f"💰 {price}\n"
                f"🗓 {when}\n"
                f"🏁 {row['city'] or '—'}\n\n"
                f'<a href="{row["url"]}">Открыть лот →</a>'
            )
            try:
                await bot.send_message(
                    chat_id=chat_id, text=text,
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                )
                await set_notify_stage(row["external_id"], stage)
                sent += 1
                await asyncio.sleep(SEND_DELAY)
            except Exception as e:
                logger.error(f"напоминания: не отправлено по лоту "
                             f"{row['external_id']}: {e}")

    if sent:
        logger.info(f"напоминания: отправлено {sent}")
    return sent


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
