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
    get_filter_by_id,
    create_filter,
    delete_filter,
    toggle_filter,
    update_filter_field,
    get_pool,
)

logger = logging.getLogger(__name__)
router = Router()

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
    cities       = State()   # множественный выбор
    transmission = State()
    body_type    = State()
    sources      = State()


class EditForm(StatesGroup):
    choosing_field = State()
    entering_value = State()


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

REGIONS = [
    "Москва", "Санкт-Петербург", "Московская обл.", "Краснодарский край",
    "Свердловская обл.", "Ростовская обл.", "Татарстан", "Башкортостан",
    "Новосибирская обл.", "Самарская обл.", "Нижегородская обл.", "Челябинская обл.",
    "Волгоградская обл.", "Красноярский край", "Саратовская обл.", "Пермский край",
    "Воронежская обл.", "Кемеровская обл.", "Ставропольский край", "Тюменская обл.",
    "Иркутская обл.", "Омская обл.", "Ленинградская обл.", "Приморский край",
    "Белгородская обл.", "Тверская обл.", "Ярославская обл.", "Калининградская обл.",
    "Волгоград", "Волжский", "Камышин", "Михайловка",
]

# ── Клавиатуры ────────────────────────────────────────────────────────────────

def _main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои фильтры",   callback_data="filters_list:0")],
        [InlineKeyboardButton(text="➕ Новый фильтр",  callback_data="filter_add")],
        [InlineKeyboardButton(text="📊 Статистика",    callback_data="show_status")],
    ])


def _filters_kb(filters: list, page: int = 0) -> InlineKeyboardMarkup:
    start  = page * PAGE_SIZE
    chunk  = filters[start: start + PAGE_SIZE]
    total  = len(filters)
    pages  = (total + PAGE_SIZE - 1) // PAGE_SIZE

    rows = []
    for f in chunk:
        icon  = "✅" if f["is_active"] else "⏸"
        label = f"{icon} {f['name']}"
        if f["brand"]:
            label += f"  ({f['brand']}"
            label += f" {f['model']}" if f["model"] else ""
            label += ")"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"filter_info:{f['id']}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"filters_list:{page-1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
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
    toggle = (
        InlineKeyboardButton(text="⏸ Пауза",    callback_data=f"filter_pause:{filter_id}")
        if is_active else
        InlineKeyboardButton(text="▶️ Включить", callback_data=f"filter_resume:{filter_id}")
    )
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            toggle,
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"filter_edit:{filter_id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить",  callback_data=f"filter_delete:{filter_id}"),
            InlineKeyboardButton(text="◀️ К списку", callback_data="filters_list:0"),
        ],
    ])


def _edit_menu_kb(filter_id: int) -> InlineKeyboardMarkup:
    """Меню выбора поля для редактирования."""
    fields = [
        ("📌 Название",    "name"),
        ("🚗 Марка",       "brand"),
        ("🔠 Модель",      "model"),
        ("📅 Год от",      "year_from"),
        ("📅 Год до",      "year_to"),
        ("💰 Цена от",     "price_from"),
        ("💰 Цена до",     "price_to"),
        ("🛣 Пробег от",   "mileage_from"),
        ("🛣 Пробег до",   "mileage_to"),
        ("📍 Города",      "cities"),
        ("⚙️ КПП",         "transmission"),
        ("🚘 Кузов",       "body_type"),
        ("📡 Источники",   "sources"),
    ]
    rows = []
    for i in range(0, len(fields), 2):
        row = [
            InlineKeyboardButton(
                text=fields[i][0],
                callback_data=f"edit_field:{filter_id}:{fields[i][1]}"
            )
        ]
        if i + 1 < len(fields):
            row.append(InlineKeyboardButton(
                text=fields[i+1][0],
                callback_data=f"edit_field:{filter_id}:{fields[i+1][1]}"
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"filter_info:{filter_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_delete_kb(filter_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"filter_delete_confirm:{filter_id}"),
            InlineKeyboardButton(text="❌ Отмена",      callback_data=f"filter_info:{filter_id}"),
        ]
    ])


def _cities_kb(selected: list[str]) -> InlineKeyboardMarkup:
    """Клавиатура множественного выбора городов."""
    rows = []
    for i in range(0, len(REGIONS), 2):
        row = []
        for r in REGIONS[i:i+2]:
            check = "✅ " if r in selected else ""
            row.append(InlineKeyboardButton(
                text=f"{check}{r}",
                callback_data=f"fsm_city_toggle:{r}",
            ))
        rows.append(row)
    rows.append([
        InlineKeyboardButton(
            text=f"✔️ Готово ({len(selected)} выбрано)" if selected else "⏭ Пропустить (все города)",
            callback_data="fsm_city_done",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _brands_kb() -> InlineKeyboardMarkup:
    brands = sorted(CATALOG.keys())
    rows = []
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


# ── Форматирование карточки фильтра ───────────────────────────────────────────

def _render_filter(f) -> str:
    status = "✅ Активен" if f["is_active"] else "⏸ Приостановлен"
    srcs   = "  ".join(SOURCE_LABELS.get(s, s) for s in (f["sources"] or []))

    lines = [
        f"<b>📌 {f['name']}</b>",
        f"<code>{'─' * 24}</code>",
    ]
    if f["brand"] or f["model"]:
        car = " ".join(filter(None, [f["brand"], f["model"]]))
        lines.append(f"🚗 <b>Марка/Модель:</b>  {car}")
    yf, yt = f["year_from"], f["year_to"]
    if yf or yt:
        lines.append(f"📅 <b>Год:</b>  {yf or '—'} – {yt or '—'}")
    pf, pt = f["price_from"], f["price_to"]
    if pf or pt:
        lines.append(f"💰 <b>Цена:</b>  {_fmt_price(pf)} – {_fmt_price(pt)}")
    mf, mt = f["mileage_from"], f["mileage_to"]
    if mf or mt:
        lines.append(f"🛣 <b>Пробег:</b>  {_fmt_mileage(mf)} – {_fmt_mileage(mt)}")

    cities = list(f["cities"] or [])
    if cities:
        lines.append(f"📍 <b>Города:</b>  {', '.join(cities)}")

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


def _step(n: int, total: int, title: str, hint: str, skip: bool = True) -> str:
    bar  = "▓" * n + "░" * (total - n)
    skip_note = "\n<i>«-» — пропустить</i>" if skip else ""
    return (
        f"<b>{title}</b>\n"
        f"<code>{bar}</code>  {n}/{total}"
        f"{skip_note}\n\n"
        f"{hint}"
    )


# ── Команды ───────────────────────────────────────────────────────────────────

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
    await message.answer("🏠 <b>Главное меню</b>", parse_mode="HTML", reply_markup=_main_menu_kb())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    await call.message.edit_text("🏠 <b>Главное меню</b>", parse_mode="HTML", reply_markup=_main_menu_kb())
    await call.answer()


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
        "Каждые 14 мин cron-job.org стучится на /run,\n"
        "бот парсит площадки по твоим фильтрам\n"
        "и присылает только <i>новые</i> объявления.\n\n"
        "При создании фильтра отправляй «-» чтобы пропустить поле.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")
        ]]),
    )


async def _status_text() -> str:
    pool = await get_pool()
    total_filters  = await pool.fetchval("SELECT COUNT(*) FROM filters WHERE user_id=$1", OWNER_ID)
    active_filters = await pool.fetchval("SELECT COUNT(*) FROM filters WHERE user_id=$1 AND is_active=TRUE", OWNER_ID)
    seen_total = await pool.fetchval("SELECT COUNT(*) FROM seen_listings")
    seen_24h   = await pool.fetchval("SELECT COUNT(*) FROM seen_listings WHERE created_at > NOW() - INTERVAL '24 hours'")
    seen_1h    = await pool.fetchval("SELECT COUNT(*) FROM seen_listings WHERE created_at > NOW() - INTERVAL '1 hour'")
    return (
        "<b>📊 Статистика</b>\n\n"
        f"<b>Фильтры</b>\n"
        f"  Всего: {total_filters}  ·  Активных: {active_filters}\n\n"
        f"<b>Просмотрено объявлений</b>\n"
        f"  За час:    {seen_1h}\n"
        f"  За сутки:  {seen_24h}\n"
        f"  Всего:     {seen_total}"
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not _is_owner(message.from_user.id):
        return
    text = await _status_text()
    await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="show_status"),
        InlineKeyboardButton(text="🏠 Меню",     callback_data="main_menu"),
    ]]))


@router.callback_query(F.data == "show_status")
async def cb_show_status(call: CallbackQuery):
    text = await _status_text()
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="show_status"),
        InlineKeyboardButton(text="🏠 Меню",     callback_data="main_menu"),
    ]]))
    await call.answer("Обновлено ✓")


# ── /filters ──────────────────────────────────────────────────────────────────

@router.message(Command("filters"))
async def cmd_filters(message: Message):
    if not _is_owner(message.from_user.id):
        return
    filters = await get_active_filters(OWNER_ID)
    if not filters:
        await message.answer(
            "📋 Фильтров пока нет. Создай первый:",
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
    f = await get_filter_by_id(filter_id, OWNER_ID)
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
    await call.answer("⏸ Приостановлен")
    f = await get_filter_by_id(filter_id, OWNER_ID)
    if f:
        await call.message.edit_text(_render_filter(f), parse_mode="HTML",
                                     reply_markup=_filter_detail_kb(filter_id, f["is_active"]))


@router.callback_query(F.data.startswith("filter_resume:"))
async def cb_filter_resume(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    await toggle_filter(filter_id, OWNER_ID, active=True)
    await call.answer("✅ Активен")
    f = await get_filter_by_id(filter_id, OWNER_ID)
    if f:
        await call.message.edit_text(_render_filter(f), parse_mode="HTML",
                                     reply_markup=_filter_detail_kb(filter_id, f["is_active"]))


# ── Удаление ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_delete:"))
async def cb_filter_delete(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    f = await get_filter_by_id(filter_id, OWNER_ID)
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
            "📋 Фильтров нет. Создай первый:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать фильтр", callback_data="filter_add")],
                [InlineKeyboardButton(text="🏠 Меню",           callback_data="main_menu")],
            ]),
        )


# ── Редактирование фильтра ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_edit:"))
async def cb_filter_edit(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    f = await get_filter_by_id(filter_id, OWNER_ID)
    if not f:
        await call.answer("Фильтр не найден", show_alert=True)
        return
    await call.message.edit_text(
        f"✏️ <b>Редактирование: «{f['name']}»</b>\n\nЧто хочешь изменить?",
        parse_mode="HTML",
        reply_markup=_edit_menu_kb(filter_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("edit_field:"))
async def cb_edit_field(call: CallbackQuery, state: FSMContext):
    _, filter_id_str, field = call.data.split(":", 2)
    filter_id = int(filter_id_str)
    f = await get_filter_by_id(filter_id, OWNER_ID)
    if not f:
        await call.answer("Фильтр не найден", show_alert=True)
        return

    await state.update_data(edit_filter_id=filter_id, edit_field=field)
    await state.set_state(EditForm.entering_value)

    # Поля с кнопками
    if field == "transmission":
        await call.message.edit_text(
            "⚙️ Выбери КПП:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Автомат",  callback_data="edit_val:AUTO"),
                    InlineKeyboardButton(text="⚙️ Механика", callback_data="edit_val:MECHANICAL"),
                ],
                [
                    InlineKeyboardButton(text="🤖 Робот",    callback_data="edit_val:ROBOT"),
                    InlineKeyboardButton(text="〰️ Вариатор", callback_data="edit_val:VARIATOR"),
                ],
                [InlineKeyboardButton(text="❌ Убрать фильтр", callback_data="edit_val:NONE")],
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"filter_edit:{filter_id}")],
            ]),
        )
    elif field == "body_type":
        await call.message.edit_text(
            "🚘 Выбери кузов:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚗 Седан",       callback_data="edit_val:SEDAN"),
                    InlineKeyboardButton(text="🚙 Внедорожник", callback_data="edit_val:SUV"),
                ],
                [
                    InlineKeyboardButton(text="🚗 Хэтчбек",    callback_data="edit_val:HATCHBACK"),
                    InlineKeyboardButton(text="🚐 Универсал",   callback_data="edit_val:WAGON"),
                ],
                [
                    InlineKeyboardButton(text="🏎 Купе",        callback_data="edit_val:COUPE"),
                    InlineKeyboardButton(text="🛻 Пикап",       callback_data="edit_val:PICKUP"),
                ],
                [InlineKeyboardButton(text="❌ Убрать фильтр", callback_data="edit_val:NONE")],
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"filter_edit:{filter_id}")],
            ]),
        )
    elif field == "sources":
        await call.message.edit_text(
            "📡 Выбери источники:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔵+🟠 Оба", callback_data="edit_val:both")],
                [
                    InlineKeyboardButton(text="🔵 Auto.ru", callback_data="edit_val:autoru"),
                    InlineKeyboardButton(text="🟠 Дром",    callback_data="edit_val:drom"),
                ],
                [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"filter_edit:{filter_id}")],
            ]),
        )
    elif field == "cities":
        current = list(f["cities"] or [])
        await state.update_data(edit_cities=current)
        await call.message.edit_text(
            f"📍 Выбери города (можно несколько).\nВыбрано: {', '.join(current) if current else 'все'}",
            reply_markup=_cities_kb(current),
        )
    elif field == "brand":
        await call.message.edit_text("🚗 Выбери марку:", reply_markup=_brands_kb())
    else:
        # Текстовые поля
        labels = {
            "name": "📌 Введи новое название",
            "model": "🔠 Введи модель (или «-» чтобы убрать)",
            "year_from": "📅 Год от (число или «-»)",
            "year_to": "📅 Год до (число или «-»)",
            "price_from": "💰 Цена от в ₽ (число или «-»)",
            "price_to": "💰 Цена до в ₽ (число или «-»)",
            "mileage_from": "🛣 Пробег от в км (число или «-»)",
            "mileage_to": "🛣 Пробег до в км (число или «-»)",
        }
        hint = labels.get(field, f"Введи новое значение для «{field}»")
        current_val = f[field]
        current_str = f"\nТекущее: <b>{current_val}</b>" if current_val is not None else ""
        await call.message.answer(
            f"{hint}{current_str}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Отмена", callback_data=f"filter_edit:{filter_id}")
            ]]),
        )
    await call.answer()


@router.callback_query(F.data.startswith("edit_val:"), StateFilter(EditForm.entering_value))
async def cb_edit_val(call: CallbackQuery, state: FSMContext):
    val_raw = call.data.split(":", 1)[1]
    data = await state.get_data()
    filter_id = data["edit_filter_id"]
    field     = data["edit_field"]

    if val_raw == "NONE":
        value = None
    elif field == "sources":
        value = {"both": ["autoru", "drom"], "autoru": ["autoru"], "drom": ["drom"]}[val_raw]
    elif field == "brand":
        value = val_raw if val_raw != "-" else None
        # Сбрасываем модель при смене марки
        await update_filter_field(filter_id, OWNER_ID, "model", None)
    else:
        value = val_raw if val_raw != "-" else None

    await update_filter_field(filter_id, OWNER_ID, field, value)
    await state.clear()

    f = await get_filter_by_id(filter_id, OWNER_ID)
    await call.message.edit_text(
        _render_filter(f),
        parse_mode="HTML",
        reply_markup=_filter_detail_kb(filter_id, f["is_active"]),
    )
    await call.answer("✅ Сохранено")


@router.message(StateFilter(EditForm.entering_value))
async def fsm_edit_text(message: Message, state: FSMContext):
    data      = await state.get_data()
    filter_id = data["edit_filter_id"]
    field     = data["edit_field"]
    raw       = message.text.strip()

    int_fields = {"year_from", "year_to", "price_from", "price_to", "mileage_from", "mileage_to"}

    if field in int_fields:
        value = _parse_int_or_none(raw)
        if value is False:
            await message.answer("⚠️ Введи число или «-»")
            return
    elif raw == "-":
        value = None
    else:
        value = raw

    await update_filter_field(filter_id, OWNER_ID, field, value)
    await state.clear()

    f = await get_filter_by_id(filter_id, OWNER_ID)
    await message.answer(
        _render_filter(f),
        parse_mode="HTML",
        reply_markup=_filter_detail_kb(filter_id, f["is_active"]),
    )


# ── Выбор марки при редактировании ───────────────────────────────────────────

@router.callback_query(F.data.startswith("fsm_brand:"), StateFilter(EditForm.entering_value))
async def cb_edit_brand(call: CallbackQuery, state: FSMContext):
    brand = call.data.split(":", 1)[1]
    data = await state.get_data()
    filter_id = data["edit_filter_id"]

    if brand == "-":
        await update_filter_field(filter_id, OWNER_ID, "brand", None)
        await update_filter_field(filter_id, OWNER_ID, "model", None)
        await state.clear()
        f = await get_filter_by_id(filter_id, OWNER_ID)
        await call.message.edit_text(_render_filter(f), parse_mode="HTML",
                                     reply_markup=_filter_detail_kb(filter_id, f["is_active"]))
        await call.answer("✅ Сохранено")
    else:
        await update_filter_field(filter_id, OWNER_ID, "brand", brand)
        await update_filter_field(filter_id, OWNER_ID, "model", None)
        await state.update_data(edit_field="model")
        await call.message.edit_text(
            f"🔠 Выбери модель {brand.title()}:",
            reply_markup=_models_kb(brand),
        )
        await call.answer()


@router.callback_query(F.data.startswith("fsm_model:"), StateFilter(EditForm.entering_value))
async def cb_edit_model(call: CallbackQuery, state: FSMContext):
    model = call.data.split(":", 1)[1]
    data = await state.get_data()
    filter_id = data["edit_filter_id"]
    value = None if model == "-" else model
    await update_filter_field(filter_id, OWNER_ID, "model", value)
    await state.clear()
    f = await get_filter_by_id(filter_id, OWNER_ID)
    await call.message.edit_text(_render_filter(f), parse_mode="HTML",
                                 reply_markup=_filter_detail_kb(filter_id, f["is_active"]))
    await call.answer("✅ Сохранено")


# ── Города при редактировании ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fsm_city_toggle:"), StateFilter(EditForm.entering_value))
async def cb_edit_city_toggle(call: CallbackQuery, state: FSMContext):
    city = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("edit_cities", []))
    if city in selected:
        selected.remove(city)
    else:
        selected.append(city)
    await state.update_data(edit_cities=selected)
    await call.message.edit_reply_markup(reply_markup=_cities_kb(selected))
    await call.answer(f"{'✅ ' + city if city in selected else '❌ ' + city}")


@router.callback_query(F.data == "fsm_city_done", StateFilter(EditForm.entering_value))
async def cb_edit_city_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    filter_id = data["edit_filter_id"]
    selected  = data.get("edit_cities", [])
    await update_filter_field(filter_id, OWNER_ID, "cities", selected if selected else None)
    await state.clear()
    f = await get_filter_by_id(filter_id, OWNER_ID)
    await call.message.edit_text(_render_filter(f), parse_mode="HTML",
                                 reply_markup=_filter_detail_kb(filter_id, f["is_active"]))
    await call.answer("✅ Города сохранены")


# ── FSM: создание фильтра ─────────────────────────────────────────────────────

@router.callback_query(F.data == "filter_add")
async def cb_filter_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(FilterForm.name)
    await state.update_data(fsm_cities=[])
    await call.message.answer(
        _step(1, 13, "Шаг 1 — Название фильтра",
              "Как назовёшь этот поиск?\n"
              "Например: <i>Camry бюджетная</i> или <i>Круз Волгоград</i>",
              skip=False),
        parse_mode="HTML",
    )
    await call.answer()


@router.message(StateFilter(FilterForm.name))
async def fsm_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(FilterForm.brand)
    await message.answer(
        _step(2, 13, "Шаг 2 — Марка", "Выбери марку или пропусти:"),
        parse_mode="HTML",
        reply_markup=_brands_kb(),
    )


@router.callback_query(F.data.startswith("fsm_brand:"), StateFilter(FilterForm.brand))
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
            _step(3, 13, f"Шаг 3 — Модель {val.title()}", "Выбери модель:"),
            parse_mode="HTML",
            reply_markup=_models_kb(val),
        )
    await call.answer()


@router.callback_query(F.data == "fsm_back_brand", StateFilter(FilterForm.model))
async def cb_fsm_back_brand(call: CallbackQuery, state: FSMContext):
    await state.set_state(FilterForm.brand)
    await call.message.edit_text(
        _step(2, 13, "Шаг 2 — Марка", "Выбери марку:"),
        parse_mode="HTML",
        reply_markup=_brands_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("fsm_model:"), StateFilter(FilterForm.model))
async def cb_fsm_model(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":", 1)[1]
    await state.update_data(model=None if val == "-" else val)
    await state.set_state(FilterForm.year_from)
    await call.message.edit_text(
        _step(4, 13, "Шаг 4 — Год выпуска от", "Например: <code>2018</code>"),
        parse_mode="HTML",
    )
    await call.answer()


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
            _step(3, 13, "Шаг 3 — Модель", "Введи модель текстом или «-»"),
            parse_mode="HTML",
        )


@router.message(StateFilter(FilterForm.model))
async def fsm_model_text(message: Message, state: FSMContext):
    val = message.text.strip()
    await state.update_data(model=None if val == "-" else val.upper())
    await state.set_state(FilterForm.year_from)
    await message.answer(
        _step(4, 13, "Шаг 4 — Год выпуска от", "Например: <code>2018</code>"),
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
        _step(5, 13, "Шаг 5 — Год выпуска до", "Например: <code>2022</code>"),
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
        _step(6, 13, "Шаг 6 — Цена от (₽)", "Например: <code>500000</code>"),
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
        _step(7, 13, "Шаг 7 — Цена до (₽)", "Например: <code>1500000</code>"),
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
        _step(8, 13, "Шаг 8 — Пробег от (км)", "Отправь «-» чтобы пропустить"),
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
        _step(9, 13, "Шаг 9 — Пробег до (км)", "Например: <code>150000</code>"),
        parse_mode="HTML",
    )


@router.message(StateFilter(FilterForm.mileage_to))
async def fsm_mileage_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи число или «-»")
        return
    await state.update_data(mileage_to=val)
    await state.set_state(FilterForm.cities)
    data = await state.get_data()
    selected = data.get("fsm_cities", [])
    await message.answer(
        _step(10, 13, "Шаг 10 — Города",
              "Выбери один или несколько городов.\nМожно пропустить — будут все города."),
        parse_mode="HTML",
        reply_markup=_cities_kb(selected),
    )


@router.callback_query(F.data.startswith("fsm_city_toggle:"), StateFilter(FilterForm.cities))
async def cb_fsm_city_toggle(call: CallbackQuery, state: FSMContext):
    city = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("fsm_cities", []))
    if city in selected:
        selected.remove(city)
    else:
        selected.append(city)
    await state.update_data(fsm_cities=selected)
    await call.message.edit_reply_markup(reply_markup=_cities_kb(selected))
    await call.answer()


@router.callback_query(F.data == "fsm_city_done", StateFilter(FilterForm.cities))
async def cb_fsm_city_done(call: CallbackQuery, state: FSMContext):
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
                InlineKeyboardButton(text="🚗 Седан",       callback_data="fsm_bt:SEDAN"),
                InlineKeyboardButton(text="🚙 Внедорожник", callback_data="fsm_bt:SUV"),
            ],
            [
                InlineKeyboardButton(text="🚗 Хэтчбек",    callback_data="fsm_bt:HATCHBACK"),
                InlineKeyboardButton(text="🚐 Универсал",   callback_data="fsm_bt:WAGON"),
            ],
            [
                InlineKeyboardButton(text="🏎 Купе",        callback_data="fsm_bt:COUPE"),
                InlineKeyboardButton(text="🛻 Пикап",       callback_data="fsm_bt:PICKUP"),
            ],
            [InlineKeyboardButton(text="⏭ Пропустить", callback_data="fsm_bt:-")],
        ]),
    )
    await call.answer()


@router.message(StateFilter(FilterForm.transmission))
async def fsm_transmission_text(message: Message, state: FSMContext):
    val = message.text.strip().upper()
    if val not in {"AUTO", "MECHANICAL", "ROBOT", "VARIATOR", "-"}:
        await message.answer("⚠️ Отправь: AUTO, MECHANICAL, ROBOT, VARIATOR или «-»")
        return
    await state.update_data(transmission=None if val == "-" else val)
    await state.set_state(FilterForm.body_type)


@router.callback_query(F.data.startswith("fsm_bt:"))
async def cb_fsm_body(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":")[1]
    await state.update_data(body_type=None if val == "-" else val)
    await state.set_state(FilterForm.sources)
    await call.message.edit_text(
        _step(13, 13, "Шаг 13 — Источники", "Где искать?"),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔵+🟠 Оба сайта", callback_data="fsm_src:both")],
            [
                InlineKeyboardButton(text="🔵 Auto.ru", callback_data="fsm_src:autoru"),
                InlineKeyboardButton(text="🟠 Дром",    callback_data="fsm_src:drom"),
            ],
        ]),
    )
    await call.answer()


@router.message(StateFilter(FilterForm.body_type))
async def fsm_body_text(message: Message, state: FSMContext):
    val = message.text.strip().upper()
    if val not in {"SEDAN", "SUV", "HATCHBACK", "WAGON", "COUPE", "MINIVAN", "PICKUP", "-"}:
        await message.answer("⚠️ Отправь тип из списка или «-»")
        return
    await state.update_data(body_type=None if val == "-" else val)
    await state.set_state(FilterForm.sources)


@router.callback_query(F.data.startswith("fsm_src:"))
async def cb_fsm_sources(call: CallbackQuery, state: FSMContext):
    val = call.data.split(":")[1]
    sources = {"autoru": ["autoru"], "drom": ["drom"], "both": ["autoru", "drom"]}[val]
    await _finish_filter(call.message, state, sources, from_call=True)
    await call.answer()


@router.message(StateFilter(FilterForm.sources))
async def fsm_sources_text(message: Message, state: FSMContext):
    val = message.text.strip().lower()
    sources = ["autoru", "drom"] if val == "-" else [
        s.strip() for s in val.split(",") if s.strip() in {"autoru", "drom"}
    ]
    if not sources:
        await message.answer("⚠️ Укажи хотя бы один источник")
        return
    await _finish_filter(message, state, sources, from_call=False)


async def _finish_filter(msg, state: FSMContext, sources: list, from_call: bool):
    data = await state.get_data()
    await state.clear()

    cities = data.get("fsm_cities") or []

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
        cities=cities if cities else None,
        transmission=data.get("transmission"),
        body_type=data.get("body_type"),
        sources=sources,
    )

    cities_str = f", ".join(cities) if cities else "все города"
    text = (
        f"✅ <b>Фильтр «{f['name']}» создан!</b>\n\n"
        f"📍 Города: {cities_str}\n"
        f"Объявления начнут приходить при следующем запуске."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 К фильтрам", callback_data="filters_list:0")],
        [InlineKeyboardButton(text="➕ Ещё фильтр", callback_data="filter_add")],
        [InlineKeyboardButton(text="🏠 Меню",       callback_data="main_menu")],
    ])

    if from_call:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ── Избранное / скрыть ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fav_add:"))
async def cb_fav_add(call: CallbackQuery):
    await call.answer("⭐️ Добавлено в избранное (скоро)")


@router.callback_query(F.data.startswith("listing_hide:"))
async def cb_listing_hide(call: CallbackQuery):
    await call.message.delete()
    await call.answer("🚫 Скрыто")
