import logging
from typing import Optional
from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from config import OWNER_ID
from db.repository import (
    get_active_filters,
    create_filter,
    delete_filter,
    toggle_filter,
    get_pool,
)

logger = logging.getLogger(__name__)
router = Router()


# ── FSM ───────────────────────────────────────────────────────────────────────

class FilterForm(StatesGroup):
    name         = State()
    brand        = State()
    model        = State()
    year_from    = State()
    year_to      = State()
    price_from   = State()
    price_to     = State()
    mileage_from = State()
    mileage_to   = State()
    city         = State()
    transmission = State()
    body_type    = State()
    sources      = State()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_owner(message: Message) -> bool:
    return message.from_user.id == OWNER_ID


def _parse_int_or_none(text: str) -> Optional[int] | bool:
    text = text.strip().replace(" ", "")
    if text in ("-", "0", "нет", "skip", ""):
        return None
    try:
        return int(text)
    except ValueError:
        return False


def _filters_keyboard(filters: list) -> InlineKeyboardMarkup:
    buttons = []
    for f in filters:
        status = "✅" if f["is_active"] else "⏸"
        buttons.append([
            InlineKeyboardButton(
                text=f"{status} {f['name']}",
                callback_data=f"filter_info:{f['id']}",
            )
        ])
    buttons.append([
        InlineKeyboardButton(text="➕ Добавить фильтр", callback_data="filter_add")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _filter_detail_keyboard(filter_id: int, is_active: bool) -> InlineKeyboardMarkup:
    toggle_text = "⏸ Приостановить" if is_active else "▶️ Включить"
    toggle_cb = (
        f"filter_pause:{filter_id}" if is_active else f"filter_resume:{filter_id}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=toggle_text, callback_data=toggle_cb),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"filter_delete:{filter_id}"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="filters_list")],
    ])


def _format_filter(f) -> str:
    lines = [f"<b>📌 {f['name']}</b>"]
    if f["brand"]:
        line = f"Марка: {f['brand']}"
        if f["model"]:
            line += f" {f['model']}"
        lines.append(line)
    if f["year_from"] or f["year_to"]:
        lines.append(f"Год: {f['year_from'] or '—'} – {f['year_to'] or '—'}")
    if f["price_from"] or f["price_to"]:
        pf = f"{f['price_from']:,}".replace(",", " ") if f["price_from"] else "—"
        pt = f"{f['price_to']:,}".replace(",", " ") if f["price_to"] else "—"
        lines.append(f"Цена: {pf} – {pt} ₽")
    if f["mileage_from"] or f["mileage_to"]:
        lines.append(f"Пробег: {f['mileage_from'] or '—'} – {f['mileage_to'] or '—'} км")
    if f["city"]:
        lines.append(f"Город: {f['city']}")
    if f["transmission"]:
        lines.append(f"КПП: {f['transmission']}")
    if f["body_type"]:
        lines.append(f"Кузов: {f['body_type']}")
    sources = ", ".join(f["sources"] or [])
    lines.append(f"Источники: {sources}")
    status = "активен ✅" if f["is_active"] else "приостановлен ⏸"
    lines.append(f"Статус: {status}")
    return "\n".join(lines)


# ── Команды ───────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not _is_owner(message):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "👋 <b>Car Monitor Bot</b>\n\n"
        "Мониторю новые объявления о продаже авто на Auto.ru и Дром.ру.\n\n"
        "/filters — управление фильтрами\n"
        "/status  — статистика\n"
        "/help    — справка",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _is_owner(message):
        return
    await message.answer(
        "<b>Справка</b>\n\n"
        "/filters — список фильтров, добавление/удаление\n"
        "/status  — статистика seen_listings и фильтров\n\n"
        "<b>Как работает:</b>\n"
        "Каждые 30 минут cron-job.org стучится на /run,\n"
        "бот парсит Auto.ru и Дром по фильтрам\n"
        "и присылает только новые объявления.\n\n"
        "При создании фильтра отправляй «-» чтобы пропустить поле.",
        parse_mode="HTML",
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not _is_owner(message):
        return
    pool = await get_pool()
    filters_count = await pool.fetchval(
        "SELECT COUNT(*) FROM filters WHERE user_id=$1 AND is_active=TRUE",
        OWNER_ID,
    )
    seen_count = await pool.fetchval("SELECT COUNT(*) FROM seen_listings")
    seen_today = await pool.fetchval(
        "SELECT COUNT(*) FROM seen_listings "
        "WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    await message.answer(
        f"<b>📊 Статус</b>\n\n"
        f"Активных фильтров: <b>{filters_count}</b>\n"
        f"Всего в seen_listings: <b>{seen_count}</b>\n"
        f"За последние 24ч: <b>{seen_today}</b>",
        parse_mode="HTML",
    )


# ── /filters ──────────────────────────────────────────────────────────────────

@router.message(Command("filters"))
async def cmd_filters(message: Message):
    if not _is_owner(message):
        return
    filters = await get_active_filters(OWNER_ID)
    if not filters:
        await message.answer(
            "Фильтров пока нет. Создай первый:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ Добавить фильтр", callback_data="filter_add")
            ]]),
        )
        return
    await message.answer(
        f"<b>Твои фильтры ({len(filters)}):</b>",
        reply_markup=_filters_keyboard(filters),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "filters_list")
async def cb_filters_list(call: CallbackQuery):
    filters = await get_active_filters(OWNER_ID)
    if not filters:
        await call.message.edit_text(
            "Фильтров пока нет. Создай первый:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ Добавить фильтр", callback_data="filter_add")
            ]]),
        )
    else:
        await call.message.edit_text(
            f"<b>Твои фильтры ({len(filters)}):</b>",
            reply_markup=_filters_keyboard(filters),
            parse_mode="HTML",
        )
    await call.answer()


@router.callback_query(F.data.startswith("filter_info:"))
async def cb_filter_info(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    filters = await get_active_filters(OWNER_ID)
    f = next((x for x in filters if x["id"] == filter_id), None)
    if not f:
        await call.answer("Фильтр не найден", show_alert=True)
        return
    await call.message.edit_text(
        _format_filter(f),
        reply_markup=_filter_detail_keyboard(filter_id, f["is_active"]),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("filter_pause:"))
async def cb_filter_pause(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    await toggle_filter(filter_id, OWNER_ID, active=False)
    await call.answer("⏸ Приостановлен")
    filters = await get_active_filters(OWNER_ID)
    await call.message.edit_text(
        f"<b>Твои фильтры ({len(filters)}):</b>",
        reply_markup=_filters_keyboard(filters),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("filter_resume:"))
async def cb_filter_resume(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    await toggle_filter(filter_id, OWNER_ID, active=True)
    await call.answer("✅ Возобновлён")
    filters = await get_active_filters(OWNER_ID)
    await call.message.edit_text(
        f"<b>Твои фильтры ({len(filters)}):</b>",
        reply_markup=_filters_keyboard(filters),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("filter_delete:"))
async def cb_filter_delete(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    await call.message.edit_text(
        "Удалить этот фильтр? Отменить нельзя.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Да, удалить",
                    callback_data=f"filter_delete_confirm:{filter_id}",
                ),
                InlineKeyboardButton(text="◀️ Отмена", callback_data="filters_list"),
            ]
        ]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("filter_delete_confirm:"))
async def cb_filter_delete_confirm(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    deleted = await delete_filter(filter_id, OWNER_ID)
    await call.answer("🗑 Удалён" if deleted else "Не найден", show_alert=True)
    filters = await get_active_filters(OWNER_ID)
    if filters:
        await call.message.edit_text(
            f"<b>Твои фильтры ({len(filters)}):</b>",
            reply_markup=_filters_keyboard(filters),
            parse_mode="HTML",
        )
    else:
        await call.message.edit_text(
            "Фильтров нет. Создай первый:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="➕ Добавить фильтр", callback_data="filter_add")
            ]]),
        )


# ── FSM создание фильтра ──────────────────────────────────────────────────────

@router.callback_query(F.data == "filter_add")
async def cb_filter_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(FilterForm.name)
    await call.message.answer(
        "Создаём новый фильтр.\n\n"
        "<b>Шаг 1/13 — Название</b>\n"
        "Например: «Camry бюджетная»",
        parse_mode="HTML",
    )
    await call.answer()


@router.message(StateFilter(FilterForm.name))
async def fsm_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(FilterForm.brand)
    await message.answer(
        "<b>Шаг 2/13 — Марка</b>\n"
        "Например: toyota, bmw\n«-» — любая",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.brand))
async def fsm_brand(message: Message, state: FSMContext):
    val = message.text.strip()
    await state.update_data(brand=None if val == "-" else val.upper())
    await state.set_state(FilterForm.model)
    await message.answer(
        "<b>Шаг 3/13 — Модель</b>\n"
        "Например: camry, x5\n«-» — любая",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.model))
async def fsm_model(message: Message, state: FSMContext):
    val = message.text.strip()
    await state.update_data(model=None if val == "-" else val.upper())
    await state.set_state(FilterForm.year_from)
    await message.answer(
        "<b>Шаг 4/13 — Год от</b>\nНапример: 2018\n«-» — пропустить",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.year_from))
async def fsm_year_from(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("Введи год числом или «-»")
        return
    await state.update_data(year_from=val)
    await state.set_state(FilterForm.year_to)
    await message.answer(
        "<b>Шаг 5/13 — Год до</b>\nНапример: 2022\n«-» — пропустить",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.year_to))
async def fsm_year_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("Введи год числом или «-»")
        return
    await state.update_data(year_to=val)
    await state.set_state(FilterForm.price_from)
    await message.answer(
        "<b>Шаг 6/13 — Цена от (₽)</b>\nНапример: 1000000\n«-» — пропустить",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.price_from))
async def fsm_price_from(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("Введи сумму числом или «-»")
        return
    await state.update_data(price_from=val)
    await state.set_state(FilterForm.price_to)
    await message.answer(
        "<b>Шаг 7/13 — Цена до (₽)</b>\nНапример: 2000000\n«-» — пропустить",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.price_to))
async def fsm_price_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("Введи сумму числом или «-»")
        return
    await state.update_data(price_to=val)
    await state.set_state(FilterForm.mileage_from)
    await message.answer(
        "<b>Шаг 8/13 — Пробег от (км)</b>\n«-» — пропустить",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.mileage_from))
async def fsm_mileage_from(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("Введи число или «-»")
        return
    await state.update_data(mileage_from=val)
    await state.set_state(FilterForm.mileage_to)
    await message.answer(
        "<b>Шаг 9/13 — Пробег до (км)</b>\nНапример: 100000\n«-» — пропустить",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.mileage_to))
async def fsm_mileage_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("Введи число или «-»")
        return
    await state.update_data(mileage_to=val)
    await state.set_state(FilterForm.city)
    await message.answer(
        "<b>Шаг 10/13 — Город</b>\nНапример: Москва\n«-» — любой",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.city))
async def fsm_city(message: Message, state: FSMContext):
    val = message.text.strip()
    await state.update_data(city=None if val == "-" else val)
    await state.set_state(FilterForm.transmission)
    await message.answer(
        "<b>Шаг 11/13 — КПП</b>\n"
        "AUTO / MECHANICAL / ROBOT / VARIATOR\n«-» — любая",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.transmission))
async def fsm_transmission(message: Message, state: FSMContext):
    val = message.text.strip().upper()
    valid = {"AUTO", "MECHANICAL", "ROBOT", "VARIATOR", "-"}
    if val not in valid:
        await message.answer("Отправь: AUTO, MECHANICAL, ROBOT, VARIATOR или «-»")
        return
    await state.update_data(transmission=None if val == "-" else val)
    await state.set_state(FilterForm.body_type)
    await message.answer(
        "<b>Шаг 12/13 — Кузов</b>\n"
        "SEDAN / SUV / HATCHBACK / WAGON / COUPE / MINIVAN / PICKUP\n«-» — любой",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.body_type))
async def fsm_body_type(message: Message, state: FSMContext):
    val = message.text.strip().upper()
    valid = {"SEDAN", "SUV", "HATCHBACK", "WAGON", "COUPE", "MINIVAN", "PICKUP", "-"}
    if val not in valid:
        await message.answer("Отправь тип кузова из списка или «-»")
        return
    await state.update_data(body_type=None if val == "-" else val)
    await state.set_state(FilterForm.sources)
    await message.answer(
        "<b>Шаг 13/13 — Источники</b>\n"
        "autoru / drom / autoru,drom\n«-» — оба",
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.sources))
async def fsm_sources(message: Message, state: FSMContext):
    val = message.text.strip().lower()
    if val == "-":
        sources = ["autoru", "drom"]
    else:
        sources = [s.strip() for s in val.split(",") if s.strip() in {"autoru", "drom"}]
    if not sources:
        await message.answer("Укажи хотя бы один источник: autoru и/или drom")
        return

    data = await state.get_data()
    await state.clear()

    f = await create_filter(
        user_id=message.from_user.id,
        name=data["name"],
        brand=data.get("brand"),
        model=data.get("model"),
        year_from=data.get("year_from"),
        year_to=data.get("year_to"),
        price_from=data.get("price_from"),
        price_to=data.get("price_to"),
        mileage_from=data.get("mileage_from"),
        mileage_to=data.get("mileage_to"),
        city=data.get("city"),
        transmission=data.get("transmission"),
        body_type=data.get("body_type"),
        sources=sources,
    )

    await message.answer(
        f"✅ <b>Фильтр «{f['name']}» создан!</b>\n\n"
        "Объявления начнут приходить при следующем запуске парсера.\n"
        "/filters — управление фильтрами",
        parse_mode="HTML",
    )
