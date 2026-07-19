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

# Фильтров на страницу в списке
PAGE_SIZE = 5


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

def _is_owner(uid: int) -> bool:
    return uid == OWNER_ID


def _parse_int_or_none(text: str):
    text = text.strip().replace(" ", "").replace("\u2009", "")
    if text in ("-", "0", "нет", "skip", ""):
        return None
    try:
        return int(text)
    except ValueError:
        return False


def _fmt_price(v: Optional[int]) -> str:
    if not v:
        return "—"
    return f"{v:,}".replace(",", "\u2009") + " ₽"


def _fmt_mileage(v: Optional[int]) -> str:
    if not v:
        return "—"
    return f"{v:,}".replace(",", "\u2009") + " км"


TRANSMISSION_LABELS = {
    "AUTO":       "🔄 Автомат",
    "MECHANICAL": "⚙️ Механика",
    "ROBOT":      "🤖 Робот",
    "VARIATOR":   "〰️ Вариатор",
}
BODY_LABELS = {
    "SEDAN":     "🚗 Седан",
    "SUV":       "🚙 Внедорожник",
    "HATCHBACK": "🚗 Хэтчбек",
    "WAGON":     "🚐 Универсал",
    "COUPE":     "🏎 Купе",
    "MINIVAN":   "🚌 Минивэн",
    "PICKUP":    "🛻 Пикап",
}
SOURCE_LABELS = {
    "autoru": "🔵 Auto.ru",
    "drom":   "🟠 Дром",
}


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои фильтры",   callback_data="filters_list:0")],
        [InlineKeyboardButton(text="➕ Новый фильтр",  callback_data="filter_add")],
        [InlineKeyboardButton(text="📊 Статистика",    callback_data="show_status")],
    ])


def _filters_kb(filters: list, page: int = 0) -> InlineKeyboardMarkup:
    """Список фильтров с пагинацией."""
    start  = page * PAGE_SIZE
    chunk  = filters[start: start + PAGE_SIZE]
    total  = len(filters)
    pages  = (total + PAGE_SIZE - 1) // PAGE_SIZE

    rows = []
    for f in chunk:
        icon   = "✅" if f["is_active"] else "⏸"
        srcs   = " · ".join(SOURCE_LABELS.get(s, s) for s in (f["sources"] or []))
        label  = f"{icon} {f['name']}"
        if f["brand"]:
            label += f"  ({f['brand']}"
            label += f" {f['model']}" if f["model"] else ""
            label += ")"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"filter_info:{f['id']}")
        ])

    # Пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"filters_list:{page-1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(
            text=f"{page+1}/{pages}", callback_data="noop"
        ))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"filters_list:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([
        InlineKeyboardButton(text="➕ Новый фильтр", callback_data="filter_add"),
        InlineKeyboardButton(text="🏠 Меню",         callback_data="main_menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _filter_detail_kb(filter_id: int, is_active: bool) -> InlineKeyboardMarkup:
    if is_active:
        toggle = InlineKeyboardButton(text="⏸ Пауза",   callback_data=f"filter_pause:{filter_id}")
    else:
        toggle = InlineKeyboardButton(text="▶️ Включить", callback_data=f"filter_resume:{filter_id}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            toggle,
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"filter_delete:{filter_id}"),
        ],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="filters_list:0")],
    ])


def _confirm_delete_kb(filter_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"filter_delete_confirm:{filter_id}"),
            InlineKeyboardButton(text="❌ Отмена",      callback_data=f"filter_info:{filter_id}"),
        ]
    ])


# ── Форматирование карточки фильтра ───────────────────────────────────────────

def _render_filter(f) -> str:
    status = "✅ Активен" if f["is_active"] else "⏸ Приостановлен"
    srcs   = "  ".join(SOURCE_LABELS.get(s, s) for s in (f["sources"] or []))

    lines = [
        f"<b>📌 {f['name']}</b>",
        f"<code>{'─' * 24}</code>",
    ]

    # Авто
    if f["brand"] or f["model"]:
        car = " ".join(filter(None, [f["brand"], f["model"]]))
        lines.append(f"🚗 <b>Марка/Модель:</b>  {car}")

    # Год
    yf, yt = f["year_from"], f["year_to"]
    if yf or yt:
        lines.append(f"📅 <b>Год:</b>  {yf or '—'} – {yt or '—'}")

    # Цена
    pf, pt = f["price_from"], f["price_to"]
    if pf or pt:
        lines.append(f"💰 <b>Цена:</b>  {_fmt_price(pf)} – {_fmt_price(pt)}")

    # Пробег
    mf, mt = f["mileage_from"], f["mileage_to"]
    if mf or mt:
        lines.append(f"🛣 <b>Пробег:</b>  {_fmt_mileage(mf)} – {_fmt_mileage(mt)}")

    # Доп.
    if f["city"]:
        lines.append(f"📍 <b>Город:</b>  {f['city']}")
    if f["transmission"]:
        tr = TRANSMISSION_LABELS.get(f["transmission"], f["transmission"])
        lines.append(f"⚙️ <b>КПП:</b>  {tr}")
    if f["body_type"]:
        bt = BODY_LABELS.get(f["body_type"], f["body_type"])
        lines.append(f"🚘 <b>Кузов:</b>  {bt}")

    lines.append(f"<code>{'─' * 24}</code>")
    lines.append(f"📡 {srcs}")
    lines.append(f"🔘 {status}")

    return "\n".join(lines)


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not _is_owner(message.from_user.id):
        await message.answer("⛔ Доступ запрещён.")
        return
    await message.answer(
        "👋 <b>Car Monitor Bot</b>\n\n"
        "Слежу за новыми объявлениями на Auto.ru и Дром.ру\n"
        "и сразу сообщаю, когда появится что-то по твоим фильтрам.",
        parse_mode="HTML",
        reply_markup=_main_menu_kb(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    if not _is_owner(message.from_user.id):
        return
    await message.answer(
        "🏠 <b>Главное меню</b>",
        parse_mode="HTML",
        reply_markup=_main_menu_kb(),
    )


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    await call.message.edit_text(
        "🏠 <b>Главное меню</b>",
        parse_mode="HTML",
        reply_markup=_main_menu_kb(),
    )
    await call.answer()


# ── /help ────────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _is_owner(message.from_user.id):
        return
    await message.answer(
        "<b>📖 Справка</b>\n\n"
        "/start — главное меню\n"
        "/filters — список фильтров\n"
        "/status — статистика\n"
        "/help — эта справка\n\n"
        "<b>Как работает:</b>\n"
        "Каждые 30 мин cron-job.org стучится на /run,\n"
        "бот парсит площадки по твоим фильтрам\n"
        "и присылает только <i>новые</i> объявления.\n\n"
        "При создании фильтра отправляй «-» чтобы пропустить поле.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")
        ]]),
    )


# ── /status + callback ────────────────────────────────────────────────────────

async def _status_text() -> str:
    pool = await get_pool()
    total_filters = await pool.fetchval(
        "SELECT COUNT(*) FROM filters WHERE user_id=$1", OWNER_ID
    )
    active_filters = await pool.fetchval(
        "SELECT COUNT(*) FROM filters WHERE user_id=$1 AND is_active=TRUE", OWNER_ID
    )
    seen_total = await pool.fetchval("SELECT COUNT(*) FROM seen_listings")
    seen_24h   = await pool.fetchval(
        "SELECT COUNT(*) FROM seen_listings WHERE created_at > NOW() - INTERVAL '24 hours'"
    )
    seen_1h    = await pool.fetchval(
        "SELECT COUNT(*) FROM seen_listings WHERE created_at > NOW() - INTERVAL '1 hour'"
    )
    return (
        "<b>📊 Статистика</b>\n\n"
        f"<b>Фильтры</b>\n"
        f"  Всего: {total_filters}  ·  Активных: {active_filters}\n\n"
        f"<b>Просмотрено объявлений</b>\n"
        f"  За час:  {seen_1h}\n"
        f"  За сутки: {seen_24h}\n"
        f"  Всего:  {seen_total}"
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not _is_owner(message.from_user.id):
        return
    text = await _status_text()
    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="show_status"),
            InlineKeyboardButton(text="🏠 Меню",     callback_data="main_menu"),
        ]]),
    )


@router.callback_query(F.data == "show_status")
async def cb_show_status(call: CallbackQuery):
    text = await _status_text()
    await call.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="show_status"),
            InlineKeyboardButton(text="🏠 Меню",     callback_data="main_menu"),
        ]]),
    )
    await call.answer("Обновлено ✓")


# ── /filters ──────────────────────────────────────────────────────────────────

@router.message(Command("filters"))
async def cmd_filters(message: Message):
    if not _is_owner(message.from_user.id):
        return
    filters = await get_active_filters(OWNER_ID)
    if not filters:
        await message.answer(
            "📋 Фильтров пока нет.\nСоздай первый — бот начнёт присылать объявления.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать фильтр", callback_data="filter_add")],
                [InlineKeyboardButton(text="🏠 Меню",           callback_data="main_menu")],
            ]),
        )
        return
    await message.answer(
        f"<b>📋 Фильтры</b>  <i>({len(filters)} шт.)</i>",
        parse_mode="HTML",
        reply_markup=_filters_kb(filters, page=0),
    )


@router.callback_query(F.data.startswith("filters_list:"))
async def cb_filters_list(call: CallbackQuery):
    page    = int(call.data.split(":")[1])
    filters = await get_active_filters(OWNER_ID)
    if not filters:
        await call.message.edit_text(
            "📋 Фильтров пока нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать фильтр", callback_data="filter_add")],
                [InlineKeyboardButton(text="🏠 Меню",           callback_data="main_menu")],
            ]),
        )
    else:
        await call.message.edit_text(
            f"<b>📋 Фильтры</b>  <i>({len(filters)} шт.)</i>",
            parse_mode="HTML",
            reply_markup=_filters_kb(filters, page=page),
        )
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ── Детали фильтра ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_info:"))
async def cb_filter_info(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    filters   = await get_active_filters(OWNER_ID)
    f = next((x for x in filters if x["id"] == filter_id), None)
    if not f:
        await call.answer("Фильтр не найден", show_alert=True)
        return
    await call.message.edit_text(
        _render_filter(f),
        parse_mode="HTML",
        reply_markup=_filter_detail_kb(filter_id, f["is_active"]),
    )
    await call.answer()


# ── Пауза / возобновление ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_pause:"))
async def cb_filter_pause(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    await toggle_filter(filter_id, OWNER_ID, active=False)
    await call.answer("⏸ Фильтр приостановлен")
    # Обновляем карточку на месте
    filters = await get_active_filters(OWNER_ID)
    f = next((x for x in filters if x["id"] == filter_id), None)
    if f:
        await call.message.edit_text(
            _render_filter(f),
            parse_mode="HTML",
            reply_markup=_filter_detail_kb(filter_id, f["is_active"]),
        )


@router.callback_query(F.data.startswith("filter_resume:"))
async def cb_filter_resume(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    await toggle_filter(filter_id, OWNER_ID, active=True)
    await call.answer("✅ Фильтр активен")
    filters = await get_active_filters(OWNER_ID)
    f = next((x for x in filters if x["id"] == filter_id), None)
    if f:
        await call.message.edit_text(
            _render_filter(f),
            parse_mode="HTML",
            reply_markup=_filter_detail_kb(filter_id, f["is_active"]),
        )


# ── Удаление ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_delete:"))
async def cb_filter_delete(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    filters   = await get_active_filters(OWNER_ID)
    f = next((x for x in filters if x["id"] == filter_id), None)
    name = f["name"] if f else "фильтр"
    await call.message.edit_text(
        f"🗑 Удалить <b>«{name}»</b>?\n\n<i>Это действие нельзя отменить.</i>",
        parse_mode="HTML",
        reply_markup=_confirm_delete_kb(filter_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("filter_delete_confirm:"))
async def cb_filter_delete_confirm(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    deleted   = await delete_filter(filter_id, OWNER_ID)
    await call.answer("🗑 Удалён" if deleted else "Не найден", show_alert=True)
    filters = await get_active_filters(OWNER_ID)
    if filters:
        await call.message.edit_text(
            f"<b>📋 Фильтры</b>  <i>({len(filters)} шт.)</i>",
            parse_mode="HTML",
            reply_markup=_filters_kb(filters, page=0),
        )
    else:
        await call.message.edit_text(
            "📋 Фильтров нет.\nСоздай первый:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать фильтр", callback_data="filter_add")],
                [InlineKeyboardButton(text="🏠 Меню",           callback_data="main_menu")],
            ]),
        )


# ── Избранное / скрыть (заглушки, можно расширить) ───────────────────────────

@router.callback_query(F.data.startswith("fav_add:"))
async def cb_fav_add(call: CallbackQuery):
    await call.answer("⭐️ Добавлено в избранное (скоро)")


@router.callback_query(F.data.startswith("listing_hide:"))
async def cb_listing_hide(call: CallbackQuery):
    await call.message.delete()
    await call.answer("🚫 Объявление скрыто")


# ── FSM: создание фильтра ─────────────────────────────────────────────────────

def _step(n: int, total: int, title: str, hint: str, skip: bool = True) -> str:
    bar   = "▓" * n + "░" * (total - n)
    skip_note = "\n<i>«-» — пропустить</i>" if skip else ""
    return (
        f"<b>{title}</b>\n"
        f"<code>{bar}</code>  {n}/{total}"
        f"{skip_note}\n\n"
        f"{hint}"
    )


@router.callback_query(F.data == "filter_add")
async def cb_filter_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(FilterForm.name)
    await call.message.answer(
        _step(1, 13, "Шаг 1 — Название фильтра",
              "Как назовёшь этот поиск?\n"
              "Например: <i>Camry бюджетная</i> или <i>Любой SUV до 2М</i>",
              skip=False),
        parse_mode="HTML",
    )
    await call.answer()


# Каталог марок и моделей
CATALOG: dict[str, list[str]] = {
    "CHEVROLET": ["CRUZE", "CAPTIVA", "ORLANDO", "AVEO", "LACETTI", "NIVA"],
    "SKODA":     ["OCTAVIA", "SUPERB", "RAPID", "KODIAQ", "KAROQ", "FABIA"],
    "TOYOTA":    ["CAMRY", "COROLLA", "RAV4", "LAND CRUISER", "HIGHLANDER", "YARIS"],
    "BMW":       ["3 SERIES", "5 SERIES", "X3", "X5", "X6", "1 SERIES"],
    "KIA":       ["RIO", "SPORTAGE", "CERATO", "SORENTO", "OPTIMA", "CEED"],
    "HYUNDAI":   ["SOLARIS", "TUCSON", "SANTA FE", "ELANTRA", "CRETA", "I30"],
    "VOLKSWAGEN":["POLO", "PASSAT", "TIGUAN", "GOLF", "JETTA", "TOUAREG"],
    "MERCEDES":  ["E-CLASS", "C-CLASS", "GLC", "GLE", "A-CLASS", "S-CLASS"],
    "AUDI":      ["A4", "A6", "Q5", "Q7", "A3", "Q3"],
    "NISSAN":    ["QASHQAI", "X-TRAIL", "ALMERA", "TEANA", "JUKE", "PATROL"],
    "RENAULT":   ["DUSTER", "LOGAN", "SANDERO", "KAPTUR", "MEGANE", "ARKANA"],
    "LADA":      ["VESTA", "GRANTA", "NIVA", "XRAY", "LARGUS", "KALINA"],
    "MAZDA":     ["CX-5", "6", "3", "CX-9", "CX-30", "2"],
    "MITSUBISHI":["OUTLANDER", "ASX", "PAJERO", "ECLIPSE CROSS", "L200", "GALANT"],
    "FORD":      ["FOCUS", "MONDEO", "EXPLORER", "KUGA", "TRANSIT", "MUSTANG"],
    "HONDA":     ["CR-V", "CIVIC", "ACCORD", "HR-V", "PILOT", "FIT"],
    "SUBARU":    ["FORESTER", "OUTBACK", "IMPREZA", "XV", "LEGACY", "TRIBECA"],
    "LEXUS":     ["RX", "ES", "NX", "LX", "IS", "GX"],
    "GEELY":     ["ATLAS", "COOLRAY", "TUGELLA", "EMGRAND", "MONJARO", "OKAVANGO"],
    "CHERY":     ["TIGGO 7 PRO", "TIGGO 4 PRO", "TIGGO 8 PRO", "ARRIZO 8", "EXEED TXL"],
}


def _brands_kb() -> InlineKeyboardMarkup:
    brands = sorted(CATALOG.keys())
    rows = []
    # По 2 кнопки в ряд
    for i in range(0, len(brands), 2):
        row = [InlineKeyboardButton(text=b.title(), callback_data=f"fsm_brand:{b}") for b in brands[i:i+2]]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⏭ Любая марка", callback_data="fsm_brand:-")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _models_kb(brand: str) -> InlineKeyboardMarkup:
    models = CATALOG.get(brand, [])
    rows = []
    for i in range(0, len(models), 2):
        row = [InlineKeyboardButton(text=m.title(), callback_data=f"fsm_model:{m}") for m in models[i:i+2]]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⏭ Любая модель", callback_data="fsm_model:-")])
    rows.append([InlineKeyboardButton(text="◀️ Сменить марку", callback_data="fsm_back_brand")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(StateFilter(FilterForm.name))
async def fsm_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(FilterForm.brand)
    await message.answer(
        _step(2, 13, "Шаг 2 — Марка", "Выбери марку или пропусти:"),
        parse_mode="HTML",
        reply_markup=_brands_kb(),
    )


@router.callback_query(F.data.startswith("fsm_brand:"))
async def cb_fsm_brand(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":", 1)[1]
    if val == "-":
        await state.update_data(brand=None, model=None)
        await state.set_state(FilterForm.year_from)
        await call.message.edit_text(
            _step(4, 13, "Шаг 4 — Год выпуска от", "Например: <code>2018</code>"),
            parse_mode="HTML",
        )
    else:
        await state.update_data(brand=val)
        await state.set_state(FilterForm.model)
        await call.message.edit_text(
            _step(3, 13, f"Шаг 3 — Модель {val.title()}", "Выбери модель или пропусти:"),
            parse_mode="HTML",
            reply_markup=_models_kb(val),
        )
    await call.answer()


@router.callback_query(F.data == "fsm_back_brand")
async def cb_fsm_back_brand(call: CallbackQuery, state: FSMContext):
    await state.set_state(FilterForm.brand)
    await call.message.edit_text(
        _step(2, 13, "Шаг 2 — Марка", "Выбери марку или пропусти:"),
        parse_mode="HTML",
        reply_markup=_brands_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("fsm_model:"))
async def cb_fsm_model(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":", 1)[1]
    await state.update_data(model=None if val == "-" else val)
    await state.set_state(FilterForm.year_from)
    await call.message.edit_text(
        _step(4, 13, "Шаг 4 — Год выпуска от", "Например: <code>2018</code>"),
        parse_mode="HTML",
    )
    await call.answer()


# Fallback — текстовый ввод марки (если вдруг нужной нет в списке)
@router.message(StateFilter(FilterForm.brand))
async def fsm_brand_text(message: Message, state: FSMContext):
    val = message.text.strip()
    brand = None if val == "-" else val.upper()
    await state.update_data(brand=brand)
    await state.set_state(FilterForm.model)
    if brand and brand in CATALOG:
        await message.answer(
            _step(3, 13, f"Шаг 3 — Модель {brand.title()}", "Выбери модель:"),
            parse_mode="HTML",
            reply_markup=_models_kb(brand),
        )
    else:
        await message.answer(
            _step(3, 13, "Шаг 3 — Модель",
                  "Введи модель текстом или отправь «-»"),
            parse_mode="HTML",
        )


@router.message(StateFilter(FilterForm.model))
async def fsm_model_text(message: Message, state: FSMContext):
    val = message.text.strip()
    await state.update_data(model=None if val == "-" else val.upper())
    await state.set_state(FilterForm.year_from)
    await message.answer(
        _step(4, 13, "Шаг 4 — Год выпуска от",
              "Например: <code>2018</code>"),
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.year_from))
async def fsm_year_from(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи год числом или «-»")
        return
    await state.update_data(year_from=val)
    await state.set_state(FilterForm.year_to)
    await message.answer(
        _step(5, 13, "Шаг 5 — Год выпуска до",
              "Например: <code>2022</code>"),
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.year_to))
async def fsm_year_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи год числом или «-»")
        return
    await state.update_data(year_to=val)
    await state.set_state(FilterForm.price_from)
    await message.answer(
        _step(6, 13, "Шаг 6 — Цена от (₽)",
              "Например: <code>1000000</code>"),
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.price_from))
async def fsm_price_from(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи сумму числом или «-»")
        return
    await state.update_data(price_from=val)
    await state.set_state(FilterForm.price_to)
    await message.answer(
        _step(7, 13, "Шаг 7 — Цена до (₽)",
              "Например: <code>2000000</code>"),
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.price_to))
async def fsm_price_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи сумму числом или «-»")
        return
    await state.update_data(price_to=val)
    await state.set_state(FilterForm.mileage_from)
    await message.answer(
        _step(8, 13, "Шаг 8 — Пробег от (км)",
              "Например: <code>0</code>"),
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.mileage_from))
async def fsm_mileage_from(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи число или «-»")
        return
    await state.update_data(mileage_from=val)
    await state.set_state(FilterForm.mileage_to)
    await message.answer(
        _step(9, 13, "Шаг 9 — Пробег до (км)",
              "Например: <code>100000</code>"),
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.mileage_to))
async def fsm_mileage_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи число или «-»")
        return
    await state.update_data(mileage_to=val)
    await state.set_state(FilterForm.city)
    await message.answer(
        _step(10, 13, "Шаг 10 — Регион", "Выбери регион или пропусти:"),
        parse_mode="HTML",
        reply_markup=_regions_kb(),
    )


REGIONS = [
    "Москва", "Санкт-Петербург", "Московская обл.", "Краснодарский край",
    "Свердловская обл.", "Ростовская обл.", "Татарстан", "Башкортостан",
    "Новосибирская обл.", "Самарская обл.", "Нижегородская обл.", "Челябинская обл.",
    "Волгоградская обл.", "Красноярский край", "Саратовская обл.", "Пермский край",
    "Воронежская обл.", "Кемеровская обл.", "Ставропольский край", "Тюменская обл.",
    "Иркутская обл.", "Омская обл.", "Ленинградская обл.", "Приморский край",
    "Белгородская обл.", "Тверская обл.", "Ярославская обл.", "Калининградская обл.",
]


def _regions_kb() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(REGIONS), 2):
        row = [
            InlineKeyboardButton(text=r, callback_data=f"fsm_city:{r}")
            for r in REGIONS[i:i+2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⏭ Любой регион", callback_data="fsm_city:-")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("fsm_city:"))
async def cb_fsm_city(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":", 1)[1]
    await state.update_data(city=None if val == "-" else val)
    await state.set_state(FilterForm.transmission)
    await call.message.edit_text(
        _step(11, 13, "Шаг 11 — КПП", "Выбери или пропусти:"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Автомат",  callback_data="fsm_tr:AUTO"),
                InlineKeyboardButton(text="⚙️ Механика", callback_data="fsm_tr:MECHANICAL"),
            ],
            [
                InlineKeyboardButton(text="🤖 Робот",    callback_data="fsm_tr:ROBOT"),
                InlineKeyboardButton(text="〰️ Вариатор", callback_data="fsm_tr:VARIATOR"),
            ],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="fsm_tr:-")],
        ]),
    )
    await call.answer()


@router.message(StateFilter(FilterForm.city))
async def fsm_city(message: Message, state: FSMContext):
    val = message.text.strip()
    await state.update_data(city=None if val == "-" else val)
    await state.set_state(FilterForm.transmission)


@router.callback_query(F.data.startswith("fsm_tr:"))
async def cb_fsm_transmission(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":")[1]
    await state.update_data(transmission=None if val == "-" else val)
    await state.set_state(FilterForm.body_type)
    await call.message.edit_text(
        _step(12, 13, "Шаг 12 — Тип кузова", "Выбери или пропусти:"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🚗 Седан",        callback_data="fsm_bt:SEDAN"),
                InlineKeyboardButton(text="🚙 Внедорожник",  callback_data="fsm_bt:SUV"),
            ],
            [
                InlineKeyboardButton(text="🚗 Хэтчбек",     callback_data="fsm_bt:HATCHBACK"),
                InlineKeyboardButton(text="🚐 Универсал",    callback_data="fsm_bt:WAGON"),
            ],
            [
                InlineKeyboardButton(text="🏎 Купе",         callback_data="fsm_bt:COUPE"),
                InlineKeyboardButton(text="🛻 Пикап",        callback_data="fsm_bt:PICKUP"),
            ],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="fsm_bt:-")],
        ]),
    )
    await call.answer()


@router.message(StateFilter(FilterForm.transmission))
async def fsm_transmission_text(message: Message, state: FSMContext):
    val = message.text.strip().upper()
    valid = {"AUTO", "MECHANICAL", "ROBOT", "VARIATOR", "-"}
    if val not in valid:
        await message.answer("⚠️ Отправь: AUTO, MECHANICAL, ROBOT, VARIATOR или «-»")
        return
    await state.update_data(transmission=None if val == "-" else val)
    await state.set_state(FilterForm.body_type)
    await message.answer(
        _step(12, 13, "Шаг 12 — Тип кузова",
              "<code>SEDAN</code>  <code>SUV</code>  <code>HATCHBACK</code>\n"
              "<code>WAGON</code>  <code>COUPE</code>  <code>PICKUP</code>"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("fsm_bt:"))
async def cb_fsm_body(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":")[1]
    await state.update_data(body_type=None if val == "-" else val)
    await state.set_state(FilterForm.sources)
    await call.message.edit_text(
        _step(13, 13, "Шаг 13 — Источники", "Где искать объявления?"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔵 Auto.ru",       callback_data="fsm_src:autoru"),
                InlineKeyboardButton(text="🟠 Дром",          callback_data="fsm_src:drom"),
            ],
            [InlineKeyboardButton(text="🔵+🟠 Оба сайта",    callback_data="fsm_src:both")],
        ]),
    )
    await call.answer()


@router.message(StateFilter(FilterForm.body_type))
async def fsm_body_text(message: Message, state: FSMContext):
    val = message.text.strip().upper()
    valid = {"SEDAN", "SUV", "HATCHBACK", "WAGON", "COUPE", "MINIVAN", "PICKUP", "-"}
    if val not in valid:
        await message.answer("⚠️ Отправь тип кузова из списка или «-»")
        return
    await state.update_data(body_type=None if val == "-" else val)
    await state.set_state(FilterForm.sources)
    await message.answer(
        _step(13, 13, "Шаг 13 — Источники",
              "<code>autoru</code>  /  <code>drom</code>  /  <code>autoru,drom</code>"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("fsm_src:"))
async def cb_fsm_sources(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":")[1]
    sources = {"autoru": ["autoru"], "drom": ["drom"], "both": ["autoru", "drom"]}[val]
    await _finish_filter(call.message, state, sources, from_call=True)
    await call.answer()


@router.message(StateFilter(FilterForm.sources))
async def fsm_sources_text(message: Message, state: FSMContext):
    val = message.text.strip().lower()
    if val == "-":
        sources = ["autoru", "drom"]
    else:
        sources = [s.strip() for s in val.split(",") if s.strip() in {"autoru", "drom"}]
    if not sources:
        await message.answer("⚠️ Укажи хотя бы один источник: autoru и/или drom")
        return
    await _finish_filter(message, state, sources, from_call=False)


async def _finish_filter(msg_or_message, state: FSMContext, sources: list, from_call: bool):
    data = await state.get_data()
    await state.clear()

    f = await create_filter(
        user_id=OWNER_ID,
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

    text = (
        f"✅ <b>Фильтр «{f['name']}» создан!</b>\n\n"
        f"Бот начнёт присылать объявления при следующем запуске парсера."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 К фильтрам", callback_data="filters_list:0")],
        [InlineKeyboardButton(text="➕ Ещё фильтр", callback_data="filter_add")],
        [InlineKeyboardButton(text="🏠 Меню",       callback_data="main_menu")],
    ])

    if from_call:
        await msg_or_message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg_or_message.answer(text, parse_mode="HTML", reply_markup=kb)
