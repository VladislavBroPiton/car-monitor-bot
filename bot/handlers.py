import datetime
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
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    ForceReply,
)

from config import OWNER_ID, WEBHOOK_HOST
from db.repository import (
    get_active_filters,
    get_filter_by_id,
    create_filter,
    delete_filter,
    toggle_filter,
    update_filter_field,
    get_pool,
    add_favorite_from_seen,
    is_favorite,
    register_user,
    is_user_allowed,
    get_users,
    set_user_active,
    get_admin_ids,
    find_lots,
    get_relist_history,
    copart_price_stats,
    duplicate_filter,
)
from costs import estimate, format_breakdown
from parsers.copart import (
    damage_ru,
    title_ru,
    keys_ru,
    TITLE_GROUPS,
    DAMAGE_CODES,
    DAMAGE_JUNK,
    YARD_STATES,
    MAKES_NOT_ON_COPART,
    CopartParser,
    fetch_makes,
    fetch_models_multi,
    FETCH_LIMIT,
)
from parsers.base import SearchFilter

copart_parser = CopartParser()

logger = logging.getLogger(__name__)
router = Router()

PAGE_SIZE = 5

# ── FSM ───────────────────────────────────────────────────────────────────────

class EditForm(StatesGroup):
    choosing_field = State()
    entering_value = State()


class CopartForm(StatesGroup):
    """Отдельный мастер для аукциона: доллары, мили, без городов."""
    name       = State()
    brand      = State()
    model      = State()
    year_from  = State()
    year_to    = State()
    price_from = State()
    price_to   = State()
    mileage_to = State()
    titles     = State()   # тип документа, множественный выбор
    damage     = State()   # исключаемые повреждения
    yards      = State()   # штаты площадок
    options    = State()   # на ходу / купить сразу


COPART_STEPS = 12


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _is_owner(uid: int) -> bool:
    """
    Есть ли у пользователя доступ к боту.

    Раньше здесь было жёсткое сравнение с OWNER_ID, и всё, что делал бот,
    принадлежало одному аккаунту. Теперь пользователи заводятся в таблице
    users, а данные у каждого свои.
    """
    return await is_user_allowed(uid)


def _parse_int_or_none(text: str):
    text = text.strip().replace(" ", "").replace("\u2009", "")
    if text in ("-", "0", "нет", "skip", ""):
        return None
    try:
        return int(text)
    except ValueError:
        return False


def _opt(record, key):
    """Значение колонки, которой может не быть на неразмигрированной БД."""
    try:
        return record[key]
    except (KeyError, IndexError):
        return None


def _parse_date_or_none(text: str):
    """«2026-09-01» → date, «-» → None, мусор → False."""
    text = text.strip()
    if text in ("-", "нет", "skip", ""):
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return False


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def _main_menu_kb() -> InlineKeyboardMarkup:
    """Главный экран: весь бот — про аукцион Copart."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🚀 Открыть Mini App",
            web_app={"url": f"{WEBHOOK_HOST}/miniapp"}
        )],
        [InlineKeyboardButton(text="🟡 Лоты Copart",        callback_data="copart_lots:0")],
        [InlineKeyboardButton(text="➕🟡 Новый фильтр Copart", callback_data="copart_add")],
        [InlineKeyboardButton(text="📋 Мои фильтры Copart", callback_data="filters_list:0")],
        [
            InlineKeyboardButton(text="🔍 Поиск лота", callback_data="copart_search"),
            InlineKeyboardButton(text="📊 Оценки",     callback_data="copart_stats"),
        ],
        [InlineKeyboardButton(text="📈 Статистика",   callback_data="show_status")],
    ])


def _filters_kb(filters: list, page: int = 0) -> InlineKeyboardMarkup:
    """Фильтры аукциона постранично."""
    start = page * PAGE_SIZE
    chunk = filters[start: start + PAGE_SIZE]
    total = len(filters)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    rows = []
    for f in chunk:
        icon  = "✅" if f["is_active"] else "⏸"
        label = f"{icon} 🟡 {f['name']}"
        brands = list(_opt(f, "brands") or []) or ([f["brand"]] if f["brand"] else [])
        if brands:
            extra = f" +{len(brands) - 1}" if len(brands) > 1 else ""
            label += f"  ({brands[0]}{extra})"
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
        InlineKeyboardButton(text="➕🟡 Новый фильтр", callback_data="copart_add"),
        InlineKeyboardButton(text="🏠 Меню",           callback_data="main_menu"),
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
        [InlineKeyboardButton(text="🔎 Проверить сейчас",
                              callback_data=f"filter_check:{filter_id}")],
        [InlineKeyboardButton(text="📄 Дублировать",
                              callback_data=f"filter_copy:{filter_id}")],
        [
            InlineKeyboardButton(text="🗑 Удалить",  callback_data=f"filter_delete:{filter_id}"),
            InlineKeyboardButton(text="◀️ К списку", callback_data="filters_list:0"),
        ],
    ])


def _edit_menu_kb(filter_id: int) -> InlineKeyboardMarkup:
    """Меню выбора поля для редактирования. Единицы — как у аукциона."""
    fields = [
        ("📌 Название",     "name"),
        ("🚗 Марка",        "brand"),
        ("🔠 Модель",       "model"),
        ("📅 Год от",       "year_from"),
        ("📅 Год до",       "year_to"),
        ("💰 Цена от, $",   "price_from"),
        ("💰 Цена до, $",   "price_to"),
        ("🛣 Пробег, миль", "mileage_to"),
        ("📄 Документ",     "title_groups"),
        ("💥 Исключить",    "damage_exclude"),
        ("🏁 Площадки",     "yards"),
        ("🚀 На ходу",      "run_and_drive"),
        ("⚡️ Купить сразу", "buy_now_only"),
        ("🗓 Аукцион с",    "auction_date_from"),
        ("🗓 Аукцион по",   "auction_date_to"),
    ]
    rows = []
    for i in range(0, len(fields), 2):
        row = [InlineKeyboardButton(
            text=fields[i][0],
            callback_data=f"edit_field:{filter_id}:{fields[i][1]}")]
        if i + 1 < len(fields):
            row.append(InlineKeyboardButton(
                text=fields[i + 1][0],
                callback_data=f"edit_field:{filter_id}:{fields[i + 1][1]}"))
        rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Назад",
                                      callback_data=f"filter_info:{filter_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Множественный выбор для полей Copart ──────────────────────────────────────

def _multi_kb(field: str, options: list[tuple[str, str]], selected: list[str],
              filter_id: int, per_row: int = 2, presets: list = None) -> InlineKeyboardMarkup:
    """
    Клавиатура «отметь галочками». options — список (код, подпись).
    presets — быстрые наборы: список (подпись, callback_data).
    """
    rows = []
    for i in range(0, len(options), per_row):
        row = []
        for code, label in options[i:i + per_row]:
            mark = "✅ " if code in selected else "▫️ "
            row.append(InlineKeyboardButton(
                text=f"{mark}{label}", callback_data=f"cp_tog:{code}",
            ))
        rows.append(row)

    for preset in (presets or []):
        rows.append([InlineKeyboardButton(text=preset[0], callback_data=preset[1])])

    rows.append([
        InlineKeyboardButton(text="🧹 Сбросить", callback_data="cp_clear"),
        InlineKeyboardButton(text="💾 Готово",   callback_data="cp_done"),
    ])
    rows.append([InlineKeyboardButton(text="◀️ Отмена",
                                      callback_data=f"filter_edit:{filter_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _multi_options(field: str) -> tuple[list[tuple[str, str]], int, list, str]:
    """Варианты, ширина строки, пресеты и заголовок для поля множественного выбора."""
    if field == "title_groups":
        return (
            [(code, label) for code, (_, label) in TITLE_GROUPS.items()],
            1,
            [],
            "📄 <b>Тип документа</b>\n\nОтметь, какие подходят. "
            "Ничего не отмечено — берём любые.",
        )
    if field == "damage_exclude":
        return (
            sorted(((code, label) for code, (_, label) in DAMAGE_CODES.items()),
                   key=lambda x: x[1]),
            2,
            [("🗑 Отметить пожары, потоп и химию", "cp_preset:junk")],
            "💥 <b>Исключить повреждения</b>\n\nОтмеченные типы "
            "<b>не будут</b> попадать в выдачу.",
        )
    if field == "yards":
        return (
            [(s, s) for s in YARD_STATES],
            5,
            [],
            "🏁 <b>Площадки Copart</b>\n\nОтметь штаты и провинции. "
            "Ничего не отмечено — вся страна.\n"
            "<i>Чем ближе к порту вывоза, тем дешевле доставка.</i>",
        )
    return [], 2, [], ""


MULTI_FIELDS = {"title_groups", "damage_exclude", "yards"}


def _confirm_delete_kb(filter_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"filter_delete_confirm:{filter_id}"),
            InlineKeyboardButton(text="❌ Отмена",      callback_data=f"filter_info:{filter_id}"),
        ]
    ])


# ── Форматирование карточки фильтра ───────────────────────────────────────────

def _render_filter(f) -> str:
    """Карточка фильтра аукциона: доллары, мили, штаты площадок."""
    status = "✅ Активен" if f["is_active"] else "⏸ Приостановлен"

    def usd(v):
        return "$" + f"{v:,}".replace(",", " ") if v else "—"

    lines = [
        f"🟡 <b>{f['name']}</b>",
        "<i>фильтр аукциона Copart</i>",
        f"<code>{'─' * 24}</code>",
    ]

    brands = list(_opt(f, "brands") or []) or ([f["brand"]] if f["brand"] else [])
    models = list(_opt(f, "models") or []) or ([f["model"]] if f["model"] else [])
    if brands:
        lines.append(f"🚗 <b>Марки:</b>  {', '.join(brands)}")
    if models:
        lines.append(f"🔠 <b>Модели:</b>  {', '.join(models)}")

    yf, yt = f["year_from"], f["year_to"]
    if yf or yt:
        lines.append(f"📅 <b>Год:</b>  {yf or '—'} – {yt or '—'}")

    pf, pt = f["price_from"], f["price_to"]
    if pf or pt:
        lines.append(f"💰 <b>Цена:</b>  {usd(pf)} – {usd(pt)}")

    mt = f["mileage_to"]
    if mt:
        miles = f"{mt:,}".replace(",", " ")
        lines.append(f"🛣 <b>Пробег до:</b>  {miles} миль  "
                     f"<i>(≈{int(mt * 1.60934):,} км)</i>".replace(",", " "))

    titles = list(_opt(f, "title_groups") or [])
    if titles:
        names = [TITLE_GROUPS[c][1] for c in titles if c in TITLE_GROUPS]
        lines.append(f"📄 <b>Документ:</b>  {', '.join(names)}")

    excluded = list(_opt(f, "damage_exclude") or [])
    if excluded:
        names = [DAMAGE_CODES[c][1] for c in excluded if c in DAMAGE_CODES]
        lines.append(f"💥 <b>Исключено:</b>  {', '.join(names)}")

    yards = list(_opt(f, "yards") or [])
    if yards:
        lines.append(f"🏁 <b>Площадки:</b>  {', '.join(yards)}")

    af, at = _opt(f, "auction_date_from"), _opt(f, "auction_date_to")
    if af or at:
        lines.append(f"🗓 <b>Аукцион:</b>  {af or '—'} – {at or '—'}")

    flags = []
    if _opt(f, "run_and_drive"):
        flags.append("🚀 только на ходу")
    if _opt(f, "buy_now_only"):
        flags.append("⚡️ только «купить сразу»")
    if flags:
        lines.append("  ·  ".join(flags))

    lines.append(f"<code>{'─' * 24}</code>")
    lines.append(f"🔘 {status}")
    return "\n".join(lines)


# ── Кто я и полный сброс ──────────────────────────────────────────────────────

@router.message(Command("whoami"))
async def cmd_whoami(message: Message):
    """
    Свой Telegram id и что за ним числится.

    Нужен, когда бот показывает пустой список фильтров, а уведомления
    приходят: значит, фильтры принадлежат другому аккаунту.
    """
    user_id = message.from_user.id
    if not await _is_owner(user_id):
        return

    pool = await get_pool()
    mine = await pool.fetchval(
        "SELECT COUNT(*) FROM filters WHERE user_id = $1", user_id)
    seen = await pool.fetchval(
        "SELECT COUNT(*) FROM user_seen WHERE user_id = $1", user_id)
    total = await pool.fetchval("SELECT COUNT(*) FROM filters")
    others = await pool.fetch(
        """SELECT user_id, COUNT(*) AS cnt FROM filters
           WHERE user_id <> $1 GROUP BY user_id""", user_id)

    lines = [
        "🪪 <b>Это ты</b>",
        f"<code>{'─' * 24}</code>",
        f"Telegram id: <code>{user_id}</code>",
        f"Роль: {'администратор' if user_id in await get_admin_ids() else 'пользователь'}",
        "",
        f"Твоих фильтров: <b>{mine}</b>",
        f"Прислано объявлений: <b>{seen}</b>",
    ]
    if others:
        lines.append("")
        lines.append("⚠️ <b>Фильтры других аккаунтов</b>")
        for o in others:
            lines.append(f"   <code>{o['user_id']}</code> — {o['cnt']} шт.")
        lines.append("")
        lines.append("<i>Они продолжают работать и слать уведомления "
                     "своему владельцу. Управлять ими можно только "
                     "с того аккаунта.</i>")
    lines.append(f"\n<i>Всего фильтров в базе: {total}</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("reset"))
async def cmd_reset(message: Message):
    """Удалить все свои фильтры и историю — начать с чистого листа."""
    user_id = message.from_user.id
    if not await _is_owner(user_id):
        return

    pool = await get_pool()
    filters = await pool.fetchval(
        "SELECT COUNT(*) FROM filters WHERE user_id = $1", user_id)
    seen = await pool.fetchval(
        "SELECT COUNT(*) FROM user_seen WHERE user_id = $1", user_id)

    if not filters and not seen:
        await message.answer("Сбрасывать нечего — фильтров и истории нет.")
        return

    await message.answer(
        f"🧹 <b>Полный сброс</b>\n\n"
        f"Удалю у аккаунта <code>{user_id}</code>:\n"
        f"• фильтров: <b>{filters}</b>\n"
        f"• записей истории: <b>{seen}</b>\n"
        f"• избранное\n\n"
        f"<i>Каталог лотов и данные других аккаунтов не тронутся. "
        f"После сброса все подходящие лоты придут заново.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧹 Да, сбросить всё",
                                  callback_data="reset_confirm")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")],
        ]),
    )


@router.callback_query(F.data == "reset_confirm")
async def cb_reset_confirm(call: CallbackQuery):
    user_id = call.from_user.id
    if not await _is_owner(user_id):
        await call.answer("⛔", show_alert=True)
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM filters WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM user_seen WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM favorites WHERE user_id = $1", user_id)

    await call.message.edit_text(
        "🧹 <b>Сброшено.</b>\n\n"
        "Создай новый фильтр — при следующем обходе придут все подходящие лоты.\n\n"
        "<i>Обход идёт раз в 14 минут. Чтобы не ждать, открой "
        "/run_now на сайте бота.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕🟡 Новый фильтр Copart",
                                  callback_data="copart_add")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
        ]),
    )
    await call.answer("Готово")


# ── Управление пользователями (для администратора) ────────────────────────────

@router.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id not in await get_admin_ids():
        return

    users = await get_users()
    if not users:
        await message.answer("Пользователей пока нет.")
        return

    lines = [f"👥 <b>Пользователи</b>  <i>({len(users)})</i>",
             f"<code>{'─' * 26}</code>"]
    rows = []
    for u in users:
        mark = "✅" if u["is_active"] else "⛔"
        role = " 👑" if u["is_admin"] else ""
        who = f"@{u['username']}" if u["username"] else (u["first_name"] or "—")
        lines.append(f"{mark} {who}{role}  ·  <code>{u['user_id']}</code>  "
                     f"·  фильтров: {u['filters']}")
        if not u["is_admin"]:
            action = "off" if u["is_active"] else "on"
            label = "⛔ Отключить" if u["is_active"] else "✅ Включить"
            rows.append([InlineKeyboardButton(
                text=f"{label} {who}",
                callback_data=f"user_{action}:{u['user_id']}")])

    rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")])
    await message.answer("\n".join(lines), parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("user_on:") | F.data.startswith("user_off:"))
async def cb_user_toggle(call: CallbackQuery):
    if call.from_user.id not in await get_admin_ids():
        await call.answer("⛔", show_alert=True)
        return
    action, uid = call.data.split(":")
    active = action == "user_on"
    await set_user_active(int(uid), active)
    await call.answer("✅ Включён" if active else "⛔ Отключён")
    await cmd_users(call.message.model_copy(update={"from_user": call.from_user}))


# ── Команды ───────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    allowed, is_new = await register_user(user.id, user.username, user.first_name)

    if not allowed:
        await message.answer(
            "⛔ <b>Доступ приостановлен.</b>\n\n"
            "Обратись к администратору бота.",
            parse_mode="HTML",
        )
        return

    if is_new and user.id != OWNER_ID:
        # Владельцу полезно знать, кто подключился
        who = f"@{user.username}" if user.username else (user.first_name or "без имени")
        for admin in await get_admin_ids():
            try:
                await message.bot.send_message(
                    admin,
                    f"👤 <b>Новый пользователь</b>\n{who} · <code>{user.id}</code>\n\n"
                    f"У него свои фильтры и уведомления. "
                    f"Отключить — /users",
                    parse_mode="HTML",
                )
            except Exception:
                pass

    await message.answer(
        "👋 <b>Привет! Это Car Monitor Bot</b>\n\n"
        "🟡 <b>Основной источник — аукцион Copart.</b>\n"
        "Битые и залоговые авто из США и Канады. Я слежу за лотами "
        "и присылаю подходящие с фото, оценкой стоимости, характером "
        "повреждения и датой торгов.\n\n"

        "🚀 <b>Как начать:</b>\n"
        "1️⃣ <b>«➕🟡 Новый фильтр Copart»</b> — марка и модель выбираются "
        "из списка самого аукциона\n"
        "2️⃣ На последнем шаге можно <b>проверить</b>, что найдётся — "
        "не дожидаясь обхода\n"
        "3️⃣ Дальше бот сам проверяет аукцион каждые 14 минут\n\n"

        "📱 <b>Mini App</b> — лоты с фотографиями, быстрые фильтры "
        "и расчёт стоимости «под ключ». Кнопка ниже.",
        parse_mode="HTML",
        reply_markup=_main_menu_kb(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    if not await _is_owner(message.from_user.id):
        return
    await message.answer("🏠 <b>Главное меню</b>", parse_mode="HTML", reply_markup=_main_menu_kb())


@router.callback_query(F.data == "main_menu")
async def cb_main_menu(call: CallbackQuery):
    try:
        await call.message.edit_text("🏠 <b>Главное меню</b>", parse_mode="HTML", reply_markup=_main_menu_kb())
    except Exception:
        pass
    await call.answer()


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not await _is_owner(message.from_user.id):
        return
    await message.answer(
        "<b>📖 Как пользоваться ботом</b>\n"
        "<i>Основной источник — аукцион Copart</i>\n\n"

        "<b>Шаг 1 — Создай фильтр</b>\n"
        "«➕🟡 Новый фильтр Copart» → марка и модель выбираются из списка "
        "самого аукциона, цена в долларах, пробег в милях. "
        "Можно ограничить типом документа, повреждениями и штатами площадок.\n\n"

        "<b>Шаг 2 — Проверь до сохранения</b>\n"
        "На последнем шаге — «🔎 Сначала посмотреть, что найдётся». "
        "Покажет количество лотов и примеры, чтобы не ждать обхода зря.\n\n"

        "<b>Шаг 3 — Жди уведомлений</b>\n"
        "Бот проверяет аукцион каждые 14 минут и присылает лоты с фото, "
        "оценкой, повреждением и датой торгов.\n\n"

        "<b>Шаг 4 — Mini App</b>\n"
        "«🚀 Открыть Mini App» — лоты с фотографиями, быстрые фильтры "
        "(на ходу, чистый документ, купить сразу), расчёт «под ключ», "
        "избранное и настройки тихих часов.\n\n"

        "<b>Полезное в разделе 🟡 Лоты Copart:</b>\n"
        "🔍 поиск по номеру лота и VIN\n"
        "📊 разброс оценок по модели, году, повреждению\n"
        "🧮 расчёт итоговой стоимости\n\n"

        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/filters — фильтры Copart\n"
        "/status — статистика",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟡 Как настроить Copart",
                                  callback_data="copart_help")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
        ]),
    )


async def _status_text(user_id: int) -> str:
    """Статистика конкретного пользователя, а не всей базы."""
    pool = await get_pool()
    total_filters  = await pool.fetchval(
        "SELECT COUNT(*) FROM filters WHERE user_id=$1", user_id)
    active_filters = await pool.fetchval(
        "SELECT COUNT(*) FROM filters WHERE user_id=$1 AND is_active=TRUE", user_id)
    seen_total = await pool.fetchval(
        "SELECT COUNT(*) FROM user_seen WHERE user_id=$1", user_id)
    seen_24h = await pool.fetchval(
        "SELECT COUNT(*) FROM user_seen WHERE user_id=$1 "
        "AND created_at > NOW() - INTERVAL '24 hours'", user_id)
    seen_1h = await pool.fetchval(
        "SELECT COUNT(*) FROM user_seen WHERE user_id=$1 "
        "AND created_at > NOW() - INTERVAL '1 hour'", user_id)
    return (
        "<b>📊 Статистика</b>\n\n"
        f"<b>Фильтры</b>\n"
        f"  Всего: {total_filters}  ·  Активных: {active_filters}\n\n"
        f"<b>Прислано лотов</b>\n"
        f"  За час:    {seen_1h}\n"
        f"  За сутки:  {seen_24h}\n"
        f"  Всего:     {seen_total}"
    )


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not await _is_owner(message.from_user.id):
        return
    text = await _status_text(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data="show_status"),
        InlineKeyboardButton(text="🏠 Меню",     callback_data="main_menu"),
    ]]))


@router.callback_query(F.data == "show_status")
async def cb_show_status(call: CallbackQuery):
    text = await _status_text(call.from_user.id)
    try:
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="show_status"),
            InlineKeyboardButton(text="🏠 Меню",     callback_data="main_menu"),
        ]]))
    except Exception:
        pass
    await call.answer("Обновлено ✓")


# ── /filters ──────────────────────────────────────────────────────────────────

@router.message(Command("filters"))
async def cmd_filters(message: Message):
    if not await _is_owner(message.from_user.id):
        return
    filters = await get_active_filters(message.from_user.id)
    if not filters:
        await message.answer("🟡 <b>Фильтров пока нет.</b>", parse_mode="HTML",
                             reply_markup=EMPTY_FILTERS_KB)
        return
    await message.answer(
        _filters_header(filters),
        parse_mode="HTML",
        reply_markup=_filters_kb(filters, 0),
    )


def _filters_header(filters: list) -> str:
    return f"🟡 <b>Фильтры Copart</b>  <i>({len(filters)} шт.)</i>"


EMPTY_FILTERS_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕🟡 Создать фильтр Copart", callback_data="copart_add")],
    [InlineKeyboardButton(text="❓ Как настроить", callback_data="copart_help")],
    [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
])


@router.callback_query(F.data.startswith("filters_list:"))
async def cb_filters_list(call: CallbackQuery):
    page    = int(call.data.split(":")[1])
    filters = await get_active_filters(call.from_user.id)

    if not filters:
        await call.message.edit_text(
            "🟡 <b>Фильтров пока нет.</b>",
            parse_mode="HTML",
            reply_markup=EMPTY_FILTERS_KB,
        )
    else:
        await call.message.edit_text(
            _filters_header(filters),
            parse_mode="HTML",
            reply_markup=_filters_kb(filters, page),
        )
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ── Детали фильтра ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_info:"))
async def cb_filter_info(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    f = await get_filter_by_id(filter_id, call.from_user.id)
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
    await toggle_filter(filter_id, call.from_user.id, active=False)
    await call.answer("⏸ Приостановлен")
    f = await get_filter_by_id(filter_id, call.from_user.id)
    if f:
        await call.message.edit_text(_render_filter(f), parse_mode="HTML",
                                     reply_markup=_filter_detail_kb(filter_id, f["is_active"]))


@router.callback_query(F.data.startswith("filter_resume:"))
async def cb_filter_resume(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    await toggle_filter(filter_id, call.from_user.id, active=True)
    await call.answer("✅ Активен")
    f = await get_filter_by_id(filter_id, call.from_user.id)
    if f:
        await call.message.edit_text(_render_filter(f), parse_mode="HTML",
                                     reply_markup=_filter_detail_kb(filter_id, f["is_active"]))


# ── Удаление ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_delete:"))
async def cb_filter_delete(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    f = await get_filter_by_id(filter_id, call.from_user.id)
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
    deleted   = await delete_filter(filter_id, call.from_user.id)
    await call.answer("🗑 Удалён" if deleted else "Не найден", show_alert=True)
    filters = await get_active_filters(call.from_user.id)
    if filters:
        await call.message.edit_text(
            _filters_header(filters),
            parse_mode="HTML",
            reply_markup=_filters_kb(filters, page=0),
        )
    else:
        await call.message.edit_text(
            "🟡 <b>Фильтров нет. Создай первый:</b>",
            parse_mode="HTML",
            reply_markup=EMPTY_FILTERS_KB,
        )


# ── Редактирование фильтра ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("filter_edit:"))
async def cb_filter_edit(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    f = await get_filter_by_id(filter_id, call.from_user.id)
    if not f:
        await call.answer("Фильтр не найден", show_alert=True)
        return
    await call.message.edit_text(
        f"🟡 <b>Редактирование: «{f['name']}»</b>\n\nЧто хочешь изменить?",
        parse_mode="HTML",
        reply_markup=_edit_menu_kb(filter_id),
    )
    await call.answer()


@router.callback_query(F.data.startswith("edit_field:"))
async def cb_edit_field(call: CallbackQuery, state: FSMContext):
    _, filter_id_str, field = call.data.split(":", 2)
    filter_id = int(filter_id_str)
    f = await get_filter_by_id(filter_id, call.from_user.id)
    if not f:
        await call.answer("Фильтр не найден", show_alert=True)
        return

    await state.update_data(edit_filter_id=filter_id, edit_field=field)
    await state.set_state(EditForm.entering_value)

    # Поля с кнопками
    if field in MULTI_FIELDS:
        current = list(_opt(f, field) or [])
        await state.update_data(cp_selected=current)
        options, per_row, presets, header = _multi_options(field)
        await call.message.edit_text(
            header,
            parse_mode="HTML",
            reply_markup=_multi_kb(field, options, current, filter_id, per_row, presets),
        )
    elif field in ("run_and_drive", "buy_now_only"):
        titles = {
            "run_and_drive": (
                "🚀 <b>Только на ходу</b>\n\n"
                "Оставить лишь те лоты, которые заводятся и едут "
                "(отметка Run and Drive у Copart)."
            ),
            "buy_now_only": (
                "⚡️ <b>Только «купить сразу»</b>\n\n"
                "Оставить лишь лоты с фиксированной ценой Buy It Now — "
                "их можно взять без участия в торгах."
            ),
        }
        await call.message.edit_text(
            titles[field],
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Включить",  callback_data="edit_val:YES"),
                    InlineKeyboardButton(text="❌ Выключить", callback_data="edit_val:NONE"),
                ],
                [InlineKeyboardButton(text="◀️ Отмена",
                                      callback_data=f"filter_edit:{filter_id}")],
            ]),
        )
    else:
        # Текстовые поля. Единицы — как у аукциона: доллары и мили
        labels = {
            "name":  "📌 Введи новое название",
            "brand": "🚗 Марка латиницей, как у аукциона "
                     "(например <code>CHEVROLET</code>), или «-» чтобы убрать",
            "model": "🔠 Модель латиницей, как у аукциона "
                     "(например <code>CRUZE</code>), или «-» чтобы убрать",
            "year_from": "📅 Год от (число или «-»)",
            "year_to":   "📅 Год до (число или «-»)",
            "price_from": "💰 Цена от в долларах (число или «-»)",
            "price_to":   "💰 Цена до в долларах (число или «-»)",
            "mileage_to": "🛣 Пробег до в милях (число или «-»)",
            "auction_date_from": "🗓 Аукцион не раньше — дата в формате "
                                 "<code>ГГГГ-ММ-ДД</code> (или «-»)",
            "auction_date_to":   "🗓 Аукцион не позже — дата в формате "
                                 "<code>ГГГГ-ММ-ДД</code> (или «-»)",
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
    elif field in ("run_and_drive", "buy_now_only"):
        value = True if val_raw == "YES" else None
    else:
        value = val_raw if val_raw != "-" else None

    await update_filter_field(filter_id, call.from_user.id, field, value)
    await state.clear()

    f = await get_filter_by_id(filter_id, call.from_user.id)
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

    int_fields = {"year_from", "year_to", "price_from", "price_to",
                  "mileage_from", "mileage_to"}
    date_fields = {"auction_date_from", "auction_date_to"}

    if field in int_fields:
        value = _parse_int_or_none(raw)
        if value is False:
            await message.answer("⚠️ Введи число или «-»")
            return
    elif field in date_fields:
        value = _parse_date_or_none(raw)
        if value is False:
            await message.answer("⚠️ Введи дату в формате ГГГГ-ММ-ДД, например "
                                 "<code>2026-09-01</code>, или «-»", parse_mode="HTML")
            return
    elif raw == "-":
        value = None
    else:
        value = raw

    # Марка и модель хранятся дважды: одиночным полем и списком.
    # Парсер читает список, поэтому правка одного поля без второго
    # просто ничего бы не изменила.
    if field in ("brand", "model"):
        value = value.upper() if value else None
        plural = "brands" if field == "brand" else "models"
        await update_filter_field(filter_id, message.from_user.id, plural,
                                  [value] if value else None)
        if field == "brand":
            # Модели предыдущей марки к новой не относятся
            await update_filter_field(filter_id, message.from_user.id, "model", None)
            await update_filter_field(filter_id, message.from_user.id, "models", None)

    await update_filter_field(filter_id, message.from_user.id, field, value)
    await state.clear()

    f = await get_filter_by_id(filter_id, message.from_user.id)
    await message.answer(
        _render_filter(f),
        parse_mode="HTML",
        reply_markup=_filter_detail_kb(filter_id, f["is_active"]),
    )


# ── Множественный выбор: переключение, пресеты, сохранение ────────────────────

async def _redraw_multi(call: CallbackQuery, state: FSMContext, selected: list[str]):
    data = await state.get_data()
    field     = data["edit_field"]
    filter_id = data["edit_filter_id"]
    options, per_row, presets, header = _multi_options(field)
    chosen = ", ".join(selected) if selected else "ничего"
    try:
        await call.message.edit_text(
            f"{header}\n\nВыбрано: <b>{chosen}</b>",
            parse_mode="HTML",
            reply_markup=_multi_kb(field, options, selected, filter_id, per_row, presets),
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("cp_tog:"), StateFilter(EditForm.entering_value))
async def cb_cp_toggle(call: CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("cp_selected", []))
    if code in selected:
        selected.remove(code)
    else:
        selected.append(code)
    await state.update_data(cp_selected=selected)
    await _redraw_multi(call, state, selected)
    await call.answer()


@router.callback_query(F.data.startswith("cp_preset:"), StateFilter(EditForm.entering_value))
async def cb_cp_preset(call: CallbackQuery, state: FSMContext):
    preset = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("cp_selected", []))
    if preset == "junk":
        for code in DAMAGE_JUNK:
            if code not in selected:
                selected.append(code)
    await state.update_data(cp_selected=selected)
    await _redraw_multi(call, state, selected)
    await call.answer("Отмечено")


@router.callback_query(F.data == "cp_clear", StateFilter(EditForm.entering_value))
async def cb_cp_clear(call: CallbackQuery, state: FSMContext):
    await state.update_data(cp_selected=[])
    await _redraw_multi(call, state, [])
    await call.answer("Сброшено")


@router.callback_query(F.data == "cp_done", StateFilter(EditForm.entering_value))
async def cb_cp_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    field     = data["edit_field"]
    filter_id = data["edit_filter_id"]
    selected  = list(data.get("cp_selected", []))

    await update_filter_field(filter_id, call.from_user.id, field, selected or None)
    await state.clear()

    f = await get_filter_by_id(filter_id, call.from_user.id)
    await call.message.edit_text(
        _render_filter(f),
        parse_mode="HTML",
        reply_markup=_filter_detail_kb(filter_id, f["is_active"]),
    )
    await call.answer("✅ Сохранено")


# ── Мастер фильтра Copart ─────────────────────────────────────────────────────

def _cp_step(n: int, title: str, hint: str, typed: bool = True) -> str:
    """typed=False — шаг только с кнопками, подсказка про «-» там лишняя."""
    bar = "▓" * n + "░" * (COPART_STEPS - n)
    skip = "<i>«-» — пропустить</i>\n" if typed else ""
    return (
        f"🟡 <b>Новый фильтр Copart</b>\n"
        f"<b>{title}</b>\n"
        f"<code>{bar}</code>  {n}/{COPART_STEPS}\n"
        f"{skip}\n"
        f"{hint}"
    )


# Последний шаг мастера. Оба ограничения неочевидны, поэтому объясняем
# их прямо в сообщении, а не прячем в справку.
COPART_OPTIONS_HINT = (
    "Два необязательных ограничения. Если сомневаешься — "
    "жми <b>«Присылать все»</b>.\n\n"

    "🚀 <b>На ходу</b>\n"
    "Аукцион проверяет часть машин и ставит отметку «заводится и едет своим "
    "ходом». У остальных двигатель может не запускаться вовсе — только "
    "на эвакуаторе.\n\n"

    "⚡️ <b>Купить сразу</b>\n"
    "Обычно лот уходит с торгов: ставки, конкуренция, цена заранее неизвестна. "
    "У части лотов есть фиксированная цена — можно забрать без аукциона."
)


def _cpw_options_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Только те, что на ходу",
                              callback_data="cpw_opt:rnd")],
        [InlineKeyboardButton(text="⚡️ Только с фиксированной ценой",
                              callback_data="cpw_opt:buy")],
        [InlineKeyboardButton(text="🚀+⚡️ На ходу и с фиксированной ценой",
                              callback_data="cpw_opt:both")],
        [InlineKeyboardButton(text="✅ Присылать все — без ограничений",
                              callback_data="cpw_opt:none")],
        [InlineKeyboardButton(text="🔎 Сначала посмотреть, что найдётся",
                              callback_data="cpw_check")],
    ])


def _cp_multi_kb(options: list[tuple[str, str]], selected: list[str],
                 per_row: int = 2, presets: list = None) -> InlineKeyboardMarkup:
    """Клавиатура множественного выбора внутри мастера."""
    rows = []
    for i in range(0, len(options), per_row):
        row = []
        for code, label in options[i:i + per_row]:
            mark = "✅ " if code in selected else "▫️ "
            row.append(InlineKeyboardButton(text=f"{mark}{label}",
                                            callback_data=f"cpw_tog:{code}"))
        rows.append(row)
    for preset in (presets or []):
        rows.append([InlineKeyboardButton(text=preset[0], callback_data=preset[1])])
    rows.append([
        InlineKeyboardButton(text="⏭ Пропустить", callback_data="cpw_skip"),
        InlineKeyboardButton(text="▶️ Далее",     callback_data="cpw_next"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "copart_add")
async def cb_copart_add(call: CallbackQuery, state: FSMContext):
    if not await _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    await state.clear()
    await state.update_data(user_id=call.from_user.id)
    await state.set_state(CopartForm.name)
    await call.message.edit_text(
        _cp_step(1, "Шаг 1 — Название",
                 "Как назовём фильтр?\nНапример: <code>Camry на ходу</code>"),
        parse_mode="HTML",
    )
    await call.answer()


# Марки и модели берём у самого аукциона, поэтому кнопки строим списком
# с числом лотов — выбирать из существующего надёжнее, чем угадывать написание
CATALOG_PAGE = 12


def _catalog_kb(items: list, page: int, pick: str, nav: str, any_cb: str,
                selected: list[str] = None, done_cb: str = None,
                back_cb: str = None, clear_cb: str = None,
                find_cb: str = None) -> InlineKeyboardMarkup:
    """
    Страница справочника: кнопки «НАЗВАНИЕ · N» по две в ряд.
    Отмеченные помечаются галочкой — можно выбрать несколько.
    """
    selected = selected or []
    pages = max(1, (len(items) + CATALOG_PAGE - 1) // CATALOG_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * CATALOG_PAGE

    rows = []
    chunk = items[start:start + CATALOG_PAGE]
    for i in range(0, len(chunk), 2):
        row = []
        for j, (name, count) in enumerate(chunk[i:i + 2]):
            mark = "✅ " if name in selected else ""
            label = name if len(name) <= 14 else name[:13] + "…"
            row.append(InlineKeyboardButton(
                text=f"{mark}{label} · {count}",
                callback_data=f"{pick}:{start + i + j}",
            ))
        rows.append(row)

    if pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"{nav}:{page-1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"{nav}:{page+1}"))
        rows.append(nav_row)

    # Поиск — отдельной кнопкой: в inline-клавиатуре нет поля ввода,
    # и подсказки «напиши пару букв» пользователь просто не замечает
    search_row = []
    if find_cb:
        search_row.append(InlineKeyboardButton(text="🔍 Поиск по названию",
                                               callback_data=find_cb))
    if clear_cb:
        search_row.append(InlineKeyboardButton(text="✕ Сбросить",
                                               callback_data=clear_cb))
    if search_row:
        rows.append(search_row)

    if selected and done_cb:
        rows.append([InlineKeyboardButton(
            text=f"▶️ Далее ({len(selected)} выбрано)", callback_data=done_cb)])

    last = [InlineKeyboardButton(text="⏭ Не важно", callback_data=any_cb)]
    if back_cb:
        last.insert(0, InlineKeyboardButton(text="◀️ Назад", callback_data=back_cb))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def search_catalog(items: list, query: str) -> list:
    """
    Отбор по подстроке. Совпадения с начала названия идут первыми:
    по запросу «CR» сначала CRUZE и CR-V, а потом уже MICRO CRUISER.
    """
    q = (query or "").strip().upper()
    if not q:
        return items
    starts = [it for it in items if it[0].upper().startswith(q)]
    inside = [it for it in items if q in it[0].upper()
              and not it[0].upper().startswith(q)]
    return starts + inside


def _search_hint(query: str, found: int, total: int) -> str:
    """Строка о состоянии поиска — чтобы было видно, что список отфильтрован."""
    if query:
        return (f"\n🔍 Поиск: <b>{query}</b> — найдено {found} из {total}"
                if found else
                f"\n🔍 По запросу <b>{query}</b> ничего не нашлось")
    return ""


async def _show_makes(msg, state: FSMContext, page: int = 0, edit: bool = False):
    data   = await state.get_data()
    picked = list(data.get("brands", []))
    query  = data.get("mk_query", "")

    makes = await fetch_makes()
    shown = search_catalog(makes, query)

    hint = (f"Список берётся прямо с аукциона — {len(makes)} марок, "
            f"рядом число лотов.\n"
            f"<b>Можно отметить несколько.</b>\n"
            f"🔍 <i>Не листай — жми «Поиск по названию» "
            f"или просто напиши пару букв в поле сообщений внизу</i>")
    hint += _search_hint(query, len(shown), len(makes))
    if picked:
        hint += f"\n\nВыбрано: <b>{', '.join(picked)}</b>"

    kb = _catalog_kb(shown, page, "cpw_mk", "cpw_mk_pg", "cpw_mk_any",
                     selected=picked, done_cb="cpw_mk_done",
                     clear_cb="cpw_mk_clear" if query else None,
                     find_cb="cpw_mk_find")
    text = _cp_step(2, "Шаг 2 — Марка", hint)
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


async def _show_models(msg, state: FSMContext, page: int = 0, edit: bool = False):
    data   = await state.get_data()
    brands = list(data.get("brands", []))
    picked = list(data.get("models", []))

    async def send(text, kb):
        if edit:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await msg.answer(text, parse_mode="HTML", reply_markup=kb)

    simple_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад",    callback_data="cpw_back:brand"),
        InlineKeyboardButton(text="⏭ Не важно", callback_data="cpw_md_any"),
    ]])

    if not brands:
        await send(_cp_step(3, "Шаг 3 — Модель",
                            "Марка не выбрана — отправь модель текстом или пропусти."),
                   simple_kb)
        return

    # Модели собираем по всем выбранным маркам: фильтр по модели
    # применяется ко всем маркам сразу, поэтому и выбирать надо из всех
    models = await fetch_models_multi(brands)
    if not models:
        await send(_cp_step(3, "Шаг 3 — Модель",
                            f"Для <b>{', '.join(brands)}</b> список моделей "
                            f"не пришёл. Отправь текстом или пропусти."),
                   simple_kb)
        return

    query = data.get("md_query", "")
    shown = search_catalog(models, query)

    title = ("Шаг 3 — Модель " + brands[0]) if len(brands) == 1 else "Шаг 3 — Модели"
    hint = (f"{len(models)} моделей на аукционе. "
            f"<b>Можно отметить несколько.</b>\n"
            f"🔍 <i>Не листай — жми «Поиск по названию» "
            f"или просто напиши пару букв в поле сообщений внизу</i>")
    if len(brands) > 1:
        hint += (f"\n\n<i>Марок выбрано {len(brands)} — "
                 f"{', '.join(brands)}. В списке модели их всех: "
                 f"отметь по одной от каждой марки, например "
                 f"CRUZE и CX-5.</i>")
    hint += _search_hint(query, len(shown), len(models))
    if picked:
        hint += f"\n\nВыбрано: <b>{', '.join(picked)}</b>"

    await send(_cp_step(3, title, hint),
               _catalog_kb(shown, page, "cpw_md", "cpw_md_pg", "cpw_md_any",
                           selected=picked, done_cb="cpw_md_done",
                           back_cb="cpw_back:brand",
                           clear_cb="cpw_md_clear" if query else None,
                           find_cb="cpw_md_find"))


# Куда возвращает «Назад» с каждого шага. Раньше ошибка на шаге 4
# означала пройти мастер заново.
CPW_BACK = {
    "brand":      CopartForm.brand,
    "model":      CopartForm.model,
    "year_from":  CopartForm.year_from,
    "year_to":    CopartForm.year_to,
    "price_from": CopartForm.price_from,
    "price_to":   CopartForm.price_to,
    "mileage_to": CopartForm.mileage_to,
}


@router.callback_query(F.data.startswith("cpw_back:"))
async def cpw_back(call: CallbackQuery, state: FSMContext):
    target = call.data.split(":", 1)[1]
    step = CPW_BACK.get(target)
    if not step:
        await call.answer()
        return

    await state.set_state(step)
    if target == "brand":
        await _show_makes(call.message, state, edit=True)
    elif target == "model":
        await _show_models(call.message, state, edit=True)
    else:
        await call.message.edit_text(
            _cp_step(*CPW_TEXT_STEPS[target]),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="◀️ Назад",
                                     callback_data=f"cpw_back:{CPW_PREV[target]}")
            ]]),
        )
    await call.answer()


# Тексты текстовых шагов — чтобы «Назад» рисовал ровно то же, что и вперёд
CPW_TEXT_STEPS = {
    "year_from":  (4, "Шаг 4 — Год от", "Например: <code>2015</code>"),
    "year_to":    (5, "Шаг 5 — Год до", "Например: <code>2022</code>"),
    "price_from": (6, "Шаг 6 — Цена от, $",
                   "Цена <b>в долларах</b> — так же, как на аукционе.\n"
                   "Например: <code>3000</code>"),
    "price_to":   (7, "Шаг 7 — Цена до, $", "Например: <code>12000</code>"),
    "mileage_to": (8, "Шаг 8 — Пробег до, миль",
                   "Одометр на аукционе <b>в милях</b>.\n"
                   "Например: <code>90000</code>  (это примерно 145 000 км)"),
}

CPW_PREV = {
    "year_from": "model", "year_to": "year_from", "price_from": "year_to",
    "price_to": "price_from", "mileage_to": "price_to",
}


def _cpw_text_kb(step_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад",
                             callback_data=f"cpw_back:{CPW_PREV[step_key]}")
    ]])


@router.message(StateFilter(CopartForm.name))
async def cpw_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:64])
    await state.set_state(CopartForm.brand)
    await _show_makes(message, state)


@router.callback_query(F.data.startswith("cpw_mk_pg:"), StateFilter(CopartForm.brand))
async def cpw_make_page(call: CallbackQuery, state: FSMContext):
    await _show_makes(call.message, state, int(call.data.split(":")[1]), edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("cpw_mk:"), StateFilter(CopartForm.brand))
async def cpw_make_pick(call: CallbackQuery, state: FSMContext):
    """Отметить марку. Справочник закэширован — индекс разрешаем без сети."""
    data = await state.get_data()
    # Индекс относится к тому списку, из которого построены кнопки:
    # при активном поиске он отфильтрован
    makes = search_catalog(await fetch_makes(), data.get("mk_query", ""))
    idx = int(call.data.split(":")[1])
    if not (0 <= idx < len(makes)):
        await call.answer()
        return

    name = makes[idx][0]
    picked = list(data.get("brands", []))
    if name in picked:
        picked.remove(name)
    else:
        picked.append(name)
    await state.update_data(brands=picked)

    page = idx // CATALOG_PAGE
    await _show_makes(call.message, state, page, edit=True)
    await call.answer(f"Выбрано: {len(picked)}" if picked else "Снято")


@router.callback_query(F.data == "cpw_mk_done", StateFilter(CopartForm.brand))
async def cpw_make_done(call: CallbackQuery, state: FSMContext):
    await _cpw_after_brands(call.message, state, edit=True)
    await call.answer()


@router.callback_query(F.data == "cpw_mk_any", StateFilter(CopartForm.brand))
async def cpw_make_any(call: CallbackQuery, state: FSMContext):
    await state.update_data(brands=[])
    await _cpw_after_brands(call.message, state, edit=True)
    await call.answer()


@router.callback_query(F.data.in_({"cpw_mk_find", "cpw_md_find"}))
async def cpw_open_search(call: CallbackQuery, state: FSMContext):
    """
    Открыть поиск. У inline-клавиатуры нет поля ввода, поэтому просим
    Telegram открыть обычное поле сообщений с подсказкой — так понятно,
    куда писать.
    """
    is_make = call.data == "cpw_mk_find"
    example = "toy" if is_make else "cru"
    what = "марки" if is_make else "модели"

    await call.message.answer(
        f"🔍 <b>Поиск {what}</b>\n\n"
        f"Отправь сообщением пару букв — например <code>{example}</code>. "
        f"Покажу подходящие.",
        parse_mode="HTML",
        reply_markup=ForceReply(input_field_placeholder=f"например: {example}"),
    )
    await call.answer()


@router.callback_query(F.data == "cpw_mk_clear", StateFilter(CopartForm.brand))
async def cpw_make_clear(call: CallbackQuery, state: FSMContext):
    await state.update_data(mk_query="")
    await _show_makes(call.message, state, 0, edit=True)
    await call.answer("Показаны все марки")


@router.message(StateFilter(CopartForm.brand))
async def cpw_brand_text(message: Message, state: FSMContext):
    """
    Текст на шаге марки — это поиск по справочнику, а не готовое значение:
    марок 400, листать их постранично мучительно.
    Перечисление через запятую по-прежнему задаёт марки напрямую.
    """
    raw = message.text.strip()

    if raw == "-":
        await state.update_data(brands=[], mk_query="")
        await _cpw_after_brands(message, state, edit=False)
        return

    if "," in raw:
        brands = [b.strip().upper() for b in raw.split(",") if b.strip()]
        await state.update_data(brands=brands, mk_query="")
        await _cpw_after_brands(message, state, edit=False)
        return

    await state.update_data(mk_query=raw)
    makes = await fetch_makes()
    if not search_catalog(makes, raw):
        # Такой марки на аукционе нет — но пользователь мог знать лучше
        await message.answer(
            f"🔍 <b>{raw.upper()}</b> в справочнике аукциона не нашлось.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Всё равно искать «{raw.upper()}»",
                                      callback_data="cpw_mk_force")],
                [InlineKeyboardButton(text="🔍✕ Показать все марки",
                                      callback_data="cpw_mk_clear")],
            ]),
        )
        return
    await _show_makes(message, state, 0, edit=False)


@router.callback_query(F.data == "cpw_mk_force", StateFilter(CopartForm.brand))
async def cpw_make_force(call: CallbackQuery, state: FSMContext):
    """Взять введённое как марку, даже если её нет в справочнике."""
    data = await state.get_data()
    raw = (data.get("mk_query") or "").strip().upper()
    await state.update_data(brands=[raw] if raw else [], mk_query="")
    await _cpw_after_brands(call.message, state, edit=True)
    await call.answer()


async def _cpw_after_brands(msg, state: FSMContext, edit: bool):
    data = await state.get_data()
    brands = list(data.get("brands", []))
    await state.set_state(CopartForm.model)

    absent = [b for b in brands if b in MAKES_NOT_ON_COPART]
    if brands and len(absent) == len(brands):
        text = _cp_step(3, "Шаг 3 — Модель",
                        f"⚠️ <b>{', '.join(absent)}</b> на Copart не встречается — "
                        f"это рынок США. Фильтр создастся, но лотов не будет.\n\n"
                        f"Отправь модель текстом или пропусти.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Назад",    callback_data="cpw_back:brand"),
            InlineKeyboardButton(text="⏭ Не важно", callback_data="cpw_md_any"),
        ]])
        if edit:
            await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
        else:
            await msg.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    await _show_models(msg, state, edit=edit)


@router.callback_query(F.data.startswith("cpw_md_pg:"), StateFilter(CopartForm.model))
async def cpw_model_page(call: CallbackQuery, state: FSMContext):
    await _show_models(call.message, state, int(call.data.split(":")[1]), edit=True)
    await call.answer()


@router.callback_query(F.data.startswith("cpw_md:"), StateFilter(CopartForm.model))
async def cpw_model_pick(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    brands = list(data.get("brands", []))
    # Как и с марками — индекс из отфильтрованного поиском списка
    models = search_catalog(await fetch_models_multi(brands),
                            data.get("md_query", ""))
    idx = int(call.data.split(":")[1])
    if not (0 <= idx < len(models)):
        await call.answer()
        return

    name = models[idx][0]
    picked = list(data.get("models", []))
    if name in picked:
        picked.remove(name)
    else:
        picked.append(name)
    await state.update_data(models=picked)

    await _show_models(call.message, state, idx // CATALOG_PAGE, edit=True)
    await call.answer(f"Выбрано: {len(picked)}" if picked else "Снято")


@router.callback_query(F.data == "cpw_md_done", StateFilter(CopartForm.model))
async def cpw_model_done(call: CallbackQuery, state: FSMContext):
    await _cpw_next_after_model(call.message, state, edit=True)
    await call.answer()


@router.callback_query(F.data == "cpw_md_any", StateFilter(CopartForm.model))
async def cpw_model_any(call: CallbackQuery, state: FSMContext):
    await state.update_data(models=[])
    await _cpw_next_after_model(call.message, state, edit=True)
    await call.answer()


@router.callback_query(F.data == "cpw_md_clear", StateFilter(CopartForm.model))
async def cpw_model_clear(call: CallbackQuery, state: FSMContext):
    await state.update_data(md_query="")
    await _show_models(call.message, state, 0, edit=True)
    await call.answer("Показаны все модели")


@router.message(StateFilter(CopartForm.model))
async def cpw_model(message: Message, state: FSMContext):
    """Текст на шаге модели ищет по справочнику — их бывает под 400."""
    raw = message.text.strip()
    data = await state.get_data()
    brands = list(data.get("brands", []))

    if raw == "-":
        await state.update_data(models=[], md_query="")
        await _cpw_next_after_model(message, state, edit=False)
        return

    if "," in raw or not brands:
        models = [m.strip().upper() for m in raw.split(",") if m.strip()]
        await state.update_data(models=models, md_query="")
        await _cpw_next_after_model(message, state, edit=False)
        return

    await state.update_data(md_query=raw)
    catalog = await fetch_models_multi(brands)
    if not search_catalog(catalog, raw):
        await message.answer(
            f"🔍 <b>{raw.upper()}</b> среди моделей "
            f"{', '.join(brands)} не нашлось.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Всё равно искать «{raw.upper()}»",
                                      callback_data="cpw_md_force")],
                [InlineKeyboardButton(text="🔍✕ Показать все модели",
                                      callback_data="cpw_md_clear")],
            ]),
        )
        return
    await _show_models(message, state, 0, edit=False)


@router.callback_query(F.data == "cpw_md_force", StateFilter(CopartForm.model))
async def cpw_model_force(call: CallbackQuery, state: FSMContext):
    """Взять введённое как модель — парсер отберёт по названию лота."""
    data = await state.get_data()
    raw = (data.get("md_query") or "").strip().upper()
    await state.update_data(models=[raw] if raw else [], md_query="")
    await _cpw_next_after_model(call.message, state, edit=True)
    await call.answer()


async def _cpw_next_after_model(msg, state: FSMContext, edit: bool):
    await state.set_state(CopartForm.year_from)
    text = _cp_step(4, "Шаг 4 — Год от", "Например: <code>2015</code>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="cpw_back:model")
    ]])
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(StateFilter(CopartForm.year_from))
async def cpw_year_from(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи число или «-»")
        return
    await state.update_data(year_from=val)
    await state.set_state(CopartForm.year_to)
    await message.answer(
        _cp_step(*CPW_TEXT_STEPS["year_to"]),
        parse_mode="HTML", reply_markup=_cpw_text_kb("year_to"),
    )


@router.message(StateFilter(CopartForm.year_to))
async def cpw_year_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи число или «-»")
        return
    await state.update_data(year_to=val)
    await state.set_state(CopartForm.price_from)
    await message.answer(
        _cp_step(*CPW_TEXT_STEPS["price_from"]),
        parse_mode="HTML", reply_markup=_cpw_text_kb("price_from"),
    )


@router.message(StateFilter(CopartForm.price_from))
async def cpw_price_from(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи число или «-»")
        return
    await state.update_data(price_from=val)
    await state.set_state(CopartForm.price_to)
    await message.answer(
        _cp_step(*CPW_TEXT_STEPS["price_to"]),
        parse_mode="HTML", reply_markup=_cpw_text_kb("price_to"),
    )


@router.message(StateFilter(CopartForm.price_to))
async def cpw_price_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи число или «-»")
        return
    await state.update_data(price_to=val)
    await state.set_state(CopartForm.mileage_to)
    await message.answer(
        _cp_step(*CPW_TEXT_STEPS["mileage_to"]),
        parse_mode="HTML", reply_markup=_cpw_text_kb("mileage_to"),
    )


@router.message(StateFilter(CopartForm.mileage_to))
async def cpw_mileage_to(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи число или «-»")
        return
    await state.update_data(mileage_to=val, cp_sel=[])
    await state.set_state(CopartForm.titles)
    options = [(c, label) for c, (_, label) in TITLE_GROUPS.items()]
    await message.answer(
        _cp_step(9, "Шаг 9 — Тип документа",
                 "Отметь подходящие. Ничего не отмечено — берём любые.\n\n"
                 "<i>Документ определяет, можно ли машину восстановить "
                 "и поставить на учёт.</i>", typed=False),
        parse_mode="HTML",
        reply_markup=_cp_multi_kb(options, [], per_row=1),
    )


@router.callback_query(F.data.startswith("cpw_tog:"))
async def cpw_toggle(call: CallbackQuery, state: FSMContext):
    code = call.data.split(":", 1)[1]
    data = await state.get_data()
    sel = list(data.get("cp_sel", []))
    if code in sel:
        sel.remove(code)
    else:
        sel.append(code)
    await state.update_data(cp_sel=sel)
    await _cpw_redraw(call, state, sel)
    await call.answer()


@router.callback_query(F.data == "cpw_preset_junk")
async def cpw_preset(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    sel = list(data.get("cp_sel", []))
    for code in DAMAGE_JUNK:
        if code not in sel:
            sel.append(code)
    await state.update_data(cp_sel=sel)
    await _cpw_redraw(call, state, sel)
    await call.answer("Отмечено")


async def _cpw_redraw(call: CallbackQuery, state: FSMContext, sel: list[str]):
    """Перерисовать текущий шаг множественного выбора."""
    current = await state.get_state()
    if current == CopartForm.titles.state:
        options, per_row, presets = (
            [(c, label) for c, (_, label) in TITLE_GROUPS.items()], 1, None)
    elif current == CopartForm.damage.state:
        options = sorted(((c, label) for c, (_, label) in DAMAGE_CODES.items()),
                         key=lambda x: x[1])
        per_row, presets = 2, [("🗑 Отметить пожары, потоп и химию", "cpw_preset_junk")]
    elif current == CopartForm.yards.state:
        options, per_row, presets = [(s, s) for s in YARD_STATES], 5, None
    else:
        return
    try:
        await call.message.edit_reply_markup(
            reply_markup=_cp_multi_kb(options, sel, per_row, presets))
    except Exception:
        pass


@router.callback_query(F.data.in_({"cpw_next", "cpw_skip"}))
async def cpw_advance(call: CallbackQuery, state: FSMContext):
    """Сохранить выбор текущего шага и перейти к следующему."""
    data = await state.get_data()
    sel = [] if call.data == "cpw_skip" else list(data.get("cp_sel", []))
    current = await state.get_state()

    if current == CopartForm.titles.state:
        await state.update_data(title_groups=sel, cp_sel=[])
        await state.set_state(CopartForm.damage)
        options = sorted(((c, label) for c, (_, label) in DAMAGE_CODES.items()),
                         key=lambda x: x[1])
        await call.message.edit_text(
            _cp_step(10, "Шаг 10 — Исключить повреждения",
                     "Отмеченные типы <b>не попадут</b> в выдачу.\n\n"
                     "<i>Горелые, утопленники и химия обычно не подлежат "
                     "восстановлению.</i>", typed=False),
            parse_mode="HTML",
            reply_markup=_cp_multi_kb(
                options, [], 2, [("🗑 Отметить пожары, потоп и химию", "cpw_preset_junk")]),
        )

    elif current == CopartForm.damage.state:
        await state.update_data(damage_exclude=sel, cp_sel=[])
        await state.set_state(CopartForm.yards)
        await call.message.edit_text(
            _cp_step(11, "Шаг 11 — Площадки",
                     "Отметь штаты и провинции. Ничего не отмечено — вся страна.\n\n"
                     "<i>Чем ближе площадка к порту вывоза, "
                     "тем дешевле доставка.</i>", typed=False),
            parse_mode="HTML",
            reply_markup=_cp_multi_kb([(s, s) for s in YARD_STATES], [], 5),
        )

    elif current == CopartForm.yards.state:
        await state.update_data(yards=sel)
        await state.set_state(CopartForm.options)
        await call.message.edit_text(
            _cp_step(12, "Шаг 12 — Состояние и способ покупки", COPART_OPTIONS_HINT,
                 typed=False),
            parse_mode="HTML",
            reply_markup=_cpw_options_kb(),
        )
    await call.answer()


def _filter_from_wizard(data: dict, opt: str = "none") -> SearchFilter:
    """Собрать SearchFilter из состояния мастера — для предпросмотра."""
    return SearchFilter(
        id=0, user_id=data.get("user_id") or OWNER_ID,
        name=data.get("name") or "проверка", kind="copart",
        brands=data.get("brands") or [], models=data.get("models") or [],
        brand=(data.get("brands") or [None])[0],
        model=(data.get("models") or [None])[0],
        year_from=data.get("year_from"), year_to=data.get("year_to"),
        price_from=data.get("price_from"), price_to=data.get("price_to"),
        mileage_to=data.get("mileage_to"), sources=["copart"],
        title_groups=data.get("title_groups") or [],
        damage_exclude=data.get("damage_exclude") or [],
        yards=data.get("yards") or [],
        run_and_drive=True if opt in ("rnd", "both") else None,
        buy_now_only=True if opt in ("buy", "both") else None,
    )


def _render_preview(result: dict) -> str:
    """Человеческий вывод предпросмотра — с подсказкой, если что-то не так."""
    if result.get("note"):
        return f"🔎 <b>Проверка</b>\n\n⚠️ {result['note']}"

    total, matched, checked = result["total"], result["matched"], result["checked"]

    if total == 0:
        return (
            "🔎 <b>Проверка</b>\n\n"
            "❌ <b>Ничего не найдено.</b>\n\n"
            "Скорее всего, фильтр слишком узкий. Попробуй расширить год, "
            "убрать ограничение по документу или по площадкам."
        )

    lines = [f"🔎 <b>Проверка</b>\n",
             f"На аукционе подходит: <b>{total:,}</b>".replace(",", " ") + " лотов"]

    if matched < checked:
        lines.append(f"Из первых {checked} прошло твои ограничения "
                     f"по цене и модели: <b>{matched}</b>")

    if total > FETCH_LIMIT:
        lines.append(
            f"\n⚠️ Это много. За один обход бот забирает {FETCH_LIMIT} лотов — "
            f"остальные не увидит. Лучше сузить фильтр."
        )
    elif matched == 0:
        lines.append("\n⚠️ Лоты есть, но ни один не прошёл по цене. "
                     "Проверь границы — они в долларах.")

    for lot in result["sample"]:
        price = _fmt_usd(lot.buy_now_price or lot.price)
        lines.append(
            f"\n<b>{lot.title}</b>\n"
            f"<code>Лот {lot.external_id}</code> · {price}"
            + (f" · {damage_ru(lot.damage_description)}" if lot.damage_description else "")
        )

    return "\n".join(lines)


@router.callback_query(F.data == "cpw_check", StateFilter(CopartForm.options))
async def cpw_check(call: CallbackQuery, state: FSMContext):
    """Показать, что найдётся, не сохраняя фильтр."""
    await call.answer("Проверяю...")
    data = await state.get_data()
    try:
        result = await copart_parser.preview(_filter_from_wizard(data))
    except Exception as e:
        logger.error(f"предпросмотр: {e}")
        await call.message.answer("⚠️ Не удалось проверить, попробуй ещё раз")
        return

    await call.message.answer(
        _render_preview(result),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Вернуться к шагу 12",
                                 callback_data="cpw_back_opts"),
        ]]),
    )


@router.callback_query(F.data == "cpw_back_opts", StateFilter(CopartForm.options))
async def cpw_back_opts(call: CallbackQuery, state: FSMContext):
    await call.message.answer(
        _cp_step(12, "Шаг 12 — Состояние и способ покупки", COPART_OPTIONS_HINT,
                 typed=False),
        parse_mode="HTML",
        reply_markup=_cpw_options_kb(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("cpw_opt:"), StateFilter(CopartForm.options))
async def cpw_finish(call: CallbackQuery, state: FSMContext):
    opt = call.data.split(":", 1)[1]
    data = await state.get_data()
    await state.clear()

    f = await create_filter(
        user_id=call.from_user.id,
        name=data["name"],
        kind="copart",
        brand=(data.get("brands") or [None])[0],
        model=(data.get("models") or [None])[0],
        brands=data.get("brands") or None,
        models=data.get("models") or None,
        year_from=data.get("year_from"),
        year_to=data.get("year_to"),
        price_from=data.get("price_from"),
        price_to=data.get("price_to"),
        mileage_to=data.get("mileage_to"),
        sources=["copart"],
        title_groups=data.get("title_groups") or None,
        damage_exclude=data.get("damage_exclude") or None,
        yards=data.get("yards") or None,
        run_and_drive=True if opt in ("rnd", "both") else None,
        buy_now_only=True if opt in ("buy", "both") else None,
    )

    await call.message.edit_text(
        f"✅ <b>Фильтр Copart «{f['name']}» создан!</b>\n\n"
        f"{_render_filter(f)}\n\n"
        f"<i>Лоты придут после ближайшего обхода.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            # Прямая ссылка на карточку — оттуда правка, проверка и удаление
            [InlineKeyboardButton(text="⚙️ Открыть фильтр",
                                  callback_data=f"filter_info:{f['id']}")],
            [
                InlineKeyboardButton(text="📋 Все фильтры", callback_data="filters_list:0"),
                InlineKeyboardButton(text="🟡 Лоты",        callback_data="copart_lots:0"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
        ]),
    )
    await call.answer("✅ Создан")


@router.callback_query(F.data.startswith("filter_copy:"))
async def cb_filter_copy(call: CallbackQuery):
    """Создать копию фильтра — «то же самое, но другая марка»."""
    if not await _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return

    filter_id = int(call.data.split(":")[1])
    copy = await duplicate_filter(filter_id, call.from_user.id)
    if not copy:
        await call.answer("Не удалось скопировать", show_alert=True)
        return

    await call.message.answer(
        f"📄 <b>Копия создана</b>\n\n{_render_filter(copy)}\n\n"
        f"<i>Поменяй что нужно — например марку — и фильтр готов.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить копию",
                                  callback_data=f"filter_edit:{copy['id']}")],
            [InlineKeyboardButton(text="📋 К списку", callback_data="filters_list:0")],
        ]),
    )
    await call.answer("📄 Скопировано")


@router.callback_query(F.data.startswith("filter_check:"))
async def cb_filter_check(call: CallbackQuery):
    """Проверить сохранённый фильтр Copart — сколько лотов он ловит сейчас."""
    if not await _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return

    filter_id = int(call.data.split(":")[1])
    record = await get_filter_by_id(filter_id, call.from_user.id)
    if not record:
        await call.answer("Фильтр не найден", show_alert=True)
        return

    await call.answer("Проверяю...")
    try:
        result = await copart_parser.preview(SearchFilter.from_record(record))
    except Exception as e:
        logger.error(f"проверка фильтра {filter_id}: {e}")
        await call.message.answer("⚠️ Не удалось проверить, попробуй ещё раз")
        return

    await call.message.answer(
        f"<b>«{record['name']}»</b>\n\n" + _render_preview(result),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить фильтр",
                                  callback_data=f"filter_edit:{filter_id}")],
            [InlineKeyboardButton(text="◀️ К фильтру",
                                  callback_data=f"filter_info:{filter_id}")],
        ]),
    )


# ── Расчёт стоимости «под ключ» ───────────────────────────────────────────────

@router.callback_query(F.data.startswith("cost:"))
async def cb_cost(call: CallbackQuery):
    if not await _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return

    external_id = call.data.split(":", 1)[1]
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT title, price, buy_now_price, url FROM seen_listings
           WHERE source = 'copart' AND external_id = $1""",
        external_id,
    )
    if not row:
        await call.answer("Лот не найден", show_alert=True)
        return

    # За базу берём цену «купить сразу», если она есть — она точная
    base = row["buy_now_price"] or row["price"]
    breakdown = estimate(base)
    if not breakdown:
        await call.answer("У лота не указана цена — считать не от чего",
                          show_alert=True)
        return

    await call.message.answer(
        f"<b>{row['title'] or 'Лот'}</b>\n"
        f"<code>Лот {external_id}</code>\n\n"
        + format_breakdown(breakdown),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔗 Открыть лот", url=row["url"]),
        ]]),
    )
    await call.answer()


# ── Статистика по оценкам Copart ──────────────────────────────────────────────

STATS_GROUPS = [
    ("model",       "🚗 По модели"),
    ("year",        "📅 По году"),
    ("damage",      "💥 По повреждению"),
    ("title_group", "📄 По документу"),
    ("state",       "🏁 По штату"),
]


def _stats_kb(active: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, label in STATS_GROUPS:
        mark = "▶️ " if code == active else ""
        row.append(InlineKeyboardButton(text=f"{mark}{label}",
                                        callback_data=f"copart_stats:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="🟡 Лоты", callback_data="copart_lots:0"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("copart_stats"))
async def cb_copart_stats(call: CallbackQuery):
    if not await _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return

    parts = call.data.split(":")
    group = parts[1] if len(parts) > 1 else "model"
    label = dict(STATS_GROUPS).get(group, group)

    rows = await copart_price_stats(call.from_user.id, group)
    if not rows:
        await call.message.edit_text(
            "📊 <b>Оценки Copart</b>\n\n"
            "Данных пока мало. Статистика появится, когда наберётся "
            "хотя бы по два лота в группе.",
            parse_mode="HTML",
            reply_markup=_stats_kb(group),
        )
        await call.answer()
        return

    lines = [
        f"📊 <b>Оценки Copart — {label.split(' ', 1)[1]}</b>",
        "<i>оценочная стоимость лота, не цена продажи</i>",
        f"<code>{'─' * 26}</code>",
    ]
    for r in rows:
        bucket = (r["bucket"] or "—").strip() or "—"
        lines.append(f"<b>{bucket}</b>  ·  {r['cnt']} шт.")
        lines.append(
            f"   {_fmt_usd(r['min_price'])} – {_fmt_usd(r['max_price'])}"
            f"   ср. <b>{_fmt_usd(r['avg_price'])}</b>"
        )
        extra = []
        if r["avg_repair"]:
            extra.append(f"ремонт ~{_fmt_usd(r['avg_repair'])}")
        if r["avg_mileage"]:
            extra.append(f"{r['avg_mileage']:,}".replace(",", " ") + " миль")
        if extra:
            lines.append("   <i>" + "  ·  ".join(extra) + "</i>")

    await call.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_stats_kb(group),
    )
    await call.answer()


# ── Поиск лота по номеру, VIN или названию ────────────────────────────────────

class LotSearch(StatesGroup):
    query = State()


@router.callback_query(F.data == "copart_search")
async def cb_copart_search(call: CallbackQuery, state: FSMContext):
    if not await _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    await state.set_state(LotSearch.query)
    await call.message.edit_text(
        "🔍 <b>Поиск лота</b>\n\n"
        "Отправь <b>номер лота</b>, <b>VIN</b> или часть названия.\n\n"
        "Примеры:\n"
        "<code>41514795</code>\n"
        "<code>2GNFLNEK9C6</code>\n"
        "<code>CRUZE LT</code>\n\n"
        "<i>VIN на Copart частично скрыт, поэтому ищем по началу.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="◀️ Отмена", callback_data="copart_lots:0")
        ]]),
    )
    await call.answer()


@router.message(StateFilter(LotSearch.query))
async def cb_lot_search_run(message: Message, state: FSMContext):
    if not await _is_owner(message.from_user.id):
        return
    await state.clear()
    query = message.text.strip()

    rows = await find_lots(message.from_user.id, query)
    if not rows:
        await message.answer(
            f"🔍 По запросу «{query}» ничего не нашлось.\n\n"
            "<i>Поиск идёт по уже сохранённым лотам — тем, что бот присылал "
            "по твоим фильтрам.</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Ещё раз", callback_data="copart_search")],
                [InlineKeyboardButton(text="🏠 Меню",   callback_data="main_menu")],
            ]),
        )
        return

    parts = [f"🔍 <b>Найдено: {len(rows)}</b>\n<code>{'─' * 24}</code>"]
    for row in rows:
        parts.append(_render_copart_lot(row))
        # Показываем историю перевыставлений, если машина уже была на торгах
        vin = _opt(row, "vin")
        if vin:
            history = await get_relist_history(vin)
            if len(history) > 1:
                parts[-1] += f"\n🔁 На торгах {len(history)} раз(а): " + ", ".join(
                    h["external_id"] for h in history
                )

    await message.answer(
        "\n\n".join(parts),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Ещё раз", callback_data="copart_search")],
            [InlineKeyboardButton(text="🏠 Меню",   callback_data="main_menu")],
        ]),
    )


# ── Инлайн-режим: поиск лота из любого чата ───────────────────────────────────
#
# Работает как «@имя_бота 41514795» или «@имя_бота CAMRY».
# Чтобы заработало, инлайн-режим нужно один раз включить у @BotFather:
# /setinline → выбрать бота → задать подсказку, например «номер лота или VIN».

@router.inline_query()
async def inline_lot_search(query: InlineQuery):
    if query.from_user.id != query.from_user.id:
        await query.answer([], cache_time=5, is_personal=True)
        return

    text = (query.query or "").strip()
    if len(text) < 2:
        await query.answer(
            [], cache_time=5, is_personal=True,
            switch_pm_text="Введи номер лота, VIN или марку",
            switch_pm_parameter="start",
        )
        return

    try:
        rows = await find_lots(query.from_user.id, text, limit=20)
    except Exception as e:
        logger.error(f"инлайн-поиск «{text}»: {e}")
        await query.answer([], cache_time=5, is_personal=True)
        return

    results = []
    for row in rows:
        buy_now = _opt(row, "buy_now_price")
        price = _fmt_usd(buy_now or row["price"])
        desc_parts = [price]
        if row["year"]:
            desc_parts.append(f"{row['year']} г.")
        damage = _opt(row, "damage_description")
        if damage:
            desc_parts.append(damage_ru(damage))
        if row["city"]:
            desc_parts.append(row["city"])

        results.append(InlineQueryResultArticle(
            id=str(row["external_id"])[:64],
            title=row["title"] or f"Лот {row['external_id']}",
            description="  ·  ".join(desc_parts),
            thumbnail_url=_opt(row, "image_url") or None,
            input_message_content=InputTextMessageContent(
                message_text=_render_copart_lot(row),
                parse_mode="HTML",
                disable_web_page_preview=True,
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🔗 Открыть лот", url=row["url"]),
            ]]),
        ))

    await query.answer(results, cache_time=30, is_personal=True)


# ── Copart ────────────────────────────────────────────────────────────────────

COPART_PAGE_SIZE = 5


def _fmt_usd(v: Optional[int]) -> str:
    if not v:
        return "оценка не указана"
    return "$" + f"{v:,}".replace(",", " ")


def _render_copart_lot(row) -> str:
    """Одна карточка лота для списка в чате."""
    parts = [f"🟡 <b>{row['title'] or 'Лот'}</b>"]

    # У лотов «купить сразу» главная цена — фиксированная, оценки часто нет
    buy_now = _opt(row, "buy_now_price")
    price_str = f"⚡️ сразу {_fmt_usd(buy_now)}" if buy_now else _fmt_usd(row["price"])
    parts.append(f"<code>Лот {row['external_id']}</code> · {price_str}")

    specs = []
    if row["year"]:
        specs.append(f"{row['year']} г.")
    if row["mileage"]:
        specs.append(f"{row['mileage']:,}".replace(",", " ") + " миль")
    if (_opt(row, "odometer_brand") or "").upper() == "NOT ACTUAL" and row["mileage"]:
        specs.append("⚠️ пробег не подтверждён")
    if specs:
        parts.append("📋 " + "  ·  ".join(specs))

    state = [s for s in (title_ru(_opt(row, "title_group")),
                         "🚀 На ходу" if _opt(row, "run_and_drive") else "",
                         keys_ru(_opt(row, "has_keys"))) if s]
    if state:
        parts.append("  ·  ".join(state))

    damage = _opt(row, "damage_description")
    if damage:
        parts.append(f"💥 {damage_ru(damage)}")

    auction = _opt(row, "auction_date")
    if auction:
        moscow = auction + datetime.timedelta(hours=3)
        parts.append(f"🗓 {moscow.strftime('%d.%m.%Y в %H:%M МСК')}")

    if row["city"]:
        parts.append(f"🏁 {row['city']}")

    parts.append(f'<a href="{row["url"]}">Открыть лот →</a>')
    return "\n".join(parts)


COPART_HELP = (
    "🟡 <b>Copart — как настроить</b>\n"
    "<code>────────────────────────</code>\n"
    "Аукцион битых и залоговых авто из США и Канады. "
    "Лоты берутся напрямую с сайта, ничего дополнительно подключать не нужно.\n\n"

    "<b>1. Создать фильтр</b>\n"
    "«➕🟡 Новый фильтр Copart» — мастер из 12 шагов. Марка и модель "
    "выбираются из списка самого аукциона, с числом лотов рядом.\n\n"

    "<b>2. Что задаётся</b>\n"
    "🚗 <b>Марка и модель</b> — из справочника аукциона. Можно отметить "
    "несколько сразу: например Camry + Accord.\n"
    "Если точной модели в справочнике нет, бот поищет по марке "
    "и отберёт нужное по названию лота.\n\n"
    "💰 <b>Цена</b> — в <b>долларах</b>, как на самом аукционе. "
    "Учти: это <b>оценочная стоимость</b> авто, а не ставка на торгах — "
    "текущие ставки Copart показывает только зарегистрированным.\n\n"
    "🛣 <b>Пробег</b> — в <b>милях</b>: одометр на аукционе в милях.\n\n"
    "📅 <b>Год</b> — как обычно.\n\n"
    "📄 <b>Документ</b> — чистый, salvage или «не восстановить».\n\n"
    "💥 <b>Исключить повреждения</b> — сразу отсечь пожары, потоп и химию.\n\n"
    "🏁 <b>Площадки</b> — штаты и провинции хранения. "
    "Чем ближе к порту вывоза, тем дешевле доставка.\n\n"
    "🗓 <b>Аукцион с / по</b> — дата торгов в формате "
    "<code>ГГГГ-ММ-ДД</code>, например <code>2026-09-01</code>. "
    "Оставь пустым, если дата неважна.\n\n"

    "<b>3. Проверить до сохранения</b>\n"
    "На последнем шаге — «🔎 Сначала посмотреть, что найдётся». "
    "Покажет количество лотов и примеры, чтобы не ждать обхода зря. "
    "У готового фильтра то же самое делает «🔎 Проверить сейчас».\n\n"

    "<b>4. Чего там нет</b>\n"
    "Это рынок США, поэтому марок <b>Lada, Skoda, Renault, Geely, Chery</b> "
    "на аукционе не бывает — по ним бот запрос даже не отправляет.\n\n"

    "<b>5. Что придёт</b>\n"
    "Фото лота, номер, оценка в $, цена «купить сразу», пробег в милях, "
    "характер повреждения, тип документа, наличие ключей, "
    "дата торгов по Москве, площадка хранения и прямая ссылка.\n\n"

    "Все найденные лоты — кнопка <b>«🟡 Лоты Copart»</b> в главном меню "
    "или одноимённый раздел в Mini App."
)


@router.callback_query(F.data == "copart_help")
async def cb_copart_help(call: CallbackQuery):
    if not await _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    try:
        await call.message.edit_text(
            COPART_HELP,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕🟡 Создать фильтр Copart",
                                      callback_data="copart_add")],
                [
                    InlineKeyboardButton(text="📋 Мои фильтры", callback_data="filters_list:0"),
                    InlineKeyboardButton(text="🟡 Лоты",       callback_data="copart_lots:0"),
                ],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
            ]),
        )
    except Exception:
        pass
    await call.answer()


@router.callback_query(F.data.startswith("copart_lots:"))
async def cb_copart_lots(call: CallbackQuery):
    if not await _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return

    page = int(call.data.split(":")[1])
    pool = await get_pool()

    total = await pool.fetchval(
        "SELECT COUNT(*) FROM seen_listings WHERE source = 'copart'"
    )
    rows = await pool.fetch(
        """SELECT * FROM seen_listings
           WHERE source = 'copart'
           ORDER BY created_at DESC
           LIMIT $1 OFFSET $2""",
        COPART_PAGE_SIZE, page * COPART_PAGE_SIZE,
    )

    if not total:
        await call.message.edit_text(
            "🟡 <b>Copart</b>\n\n"
            "Лотов пока нет.\n\n"
            "Создай фильтр Copart — бот подберёт лоты и пришлёт их сюда "
            "после ближайшего обхода (раз в 14 минут).",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕🟡 Создать фильтр Copart",
                                      callback_data="copart_add")],
                [
                    InlineKeyboardButton(text="📋 Мои фильтры",
                                         callback_data="filters_list:0"),
                    InlineKeyboardButton(text="❓ Как настроить",
                                         callback_data="copart_help"),
                ],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
            ]),
        )
        await call.answer()
        return

    pages = (total + COPART_PAGE_SIZE - 1) // COPART_PAGE_SIZE
    header = f"🟡 <b>Copart</b> — {total} лотов\n<code>{'─' * 24}</code>"
    body = "\n\n".join(_render_copart_lot(r) for r in rows)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"copart_lots:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"copart_lots:{page+1}"))

    await call.message.edit_text(
        f"{header}\n\n{body}",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            nav,
            [
                InlineKeyboardButton(text="🔍 Поиск лота", callback_data="copart_search"),
                InlineKeyboardButton(text="📊 Оценки",     callback_data="copart_stats"),
            ],
            [
                InlineKeyboardButton(text="📋 Мои фильтры", callback_data="filters_list:0"),
                InlineKeyboardButton(text="❓ Справка",      callback_data="copart_help"),
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
        ]),
    )
    await call.answer()


# ── Избранное / скрыть ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fav:"))
async def cb_fav_add(call: CallbackQuery):
    """
    Добавить в избранное. Данные берём из seen_listings по (source, external_id) —
    объявление туда уже записано к моменту отправки уведомления.
    """
    parts = call.data.split(":", 2)
    if len(parts) < 3:
        await call.answer("Не удалось определить объявление", show_alert=True)
        return

    source, external_id = parts[1], parts[2]

    try:
        added = await add_favorite_from_seen(call.from_user.id, source, external_id)
    except Exception as e:
        logger.error(f"избранное: ошибка сохранения {source}/{external_id}: {e}")
        await call.answer("⚠️ Не удалось сохранить", show_alert=True)
        return

    if added:
        await call.answer("⭐️ Добавлено в избранное")
    elif await is_favorite(call.from_user.id, source, external_id):
        await call.answer("⭐️ Уже в избранном")
    else:
        # Строки в seen_listings нет — например, объявление успели вычистить
        await call.answer("⚠️ Объявление больше не найдено", show_alert=True)


@router.callback_query(F.data.startswith("hide:"))
async def cb_listing_hide(call: CallbackQuery):
    await call.message.delete()
    await call.answer("🚫 Скрыто")
