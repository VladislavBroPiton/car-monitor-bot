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
    find_lots,
    get_relist_history,
    copart_price_stats,
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
    fetch_models,
    FETCH_LIMIT,
)
from parsers.base import SearchFilter

copart_parser = CopartParser()

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
    "avito":  "🟢 Авито",
    "copart": "🟡 Copart",
}

# Наборы источников для кнопок выбора (в боте и в FSM создания фильтра)
SOURCE_SETS = {
    "all":    ["autoru", "drom", "avito"],
    "both":   ["autoru", "drom"],
    "autoru": ["autoru"],
    "drom":   ["drom"],
    "avito":  ["avito"],
    "copart": ["copart"],
    "every":  ["autoru", "drom", "avito", "copart"],
}


def _sources_kb(prefix: str, cancel_row: list = None) -> InlineKeyboardMarkup:
    """Клавиатура выбора источников. prefix — «fsm_src» или «edit_val»."""
    rows = [
        [InlineKeyboardButton(text="🔵+🟠+🟢 Все российские", callback_data=f"{prefix}:all")],
        [
            InlineKeyboardButton(text="🔵 Auto.ru", callback_data=f"{prefix}:autoru"),
            InlineKeyboardButton(text="🟢 Авито",   callback_data=f"{prefix}:avito"),
        ],
        [
            InlineKeyboardButton(text="🟠 Дром",    callback_data=f"{prefix}:drom"),
            InlineKeyboardButton(text="🟡 Copart",  callback_data=f"{prefix}:copart"),
        ],
        [InlineKeyboardButton(text="🌍 Всё вместе", callback_data=f"{prefix}:every")],
    ]
    if cancel_row:
        rows.append(cancel_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

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

# Двухуровневый справочник: регион → список городов
REGION_CITIES: dict[str, list[str]] = {
    # ── Центральный федеральный округ ─────────────────────────────────────────
    "Москва":               ["Москва", "Зеленоград", "Троицк", "Щербинка"],
    "Московская обл.":      ["Балашиха", "Подольск", "Химки", "Королёв", "Мытищи", "Люберцы", "Красногорск", "Одинцово", "Электросталь", "Коломна", "Серпухов", "Домодедово", "Щёлково", "Ногинск", "Раменское"],
    "Белгородская обл.":    ["Белгород", "Старый Оскол", "Губкин", "Шебекино", "Алексеевка"],
    "Брянская обл.":        ["Брянск", "Клинцы", "Сельцо", "Новозыбков", "Дятьково"],
    "Владимирская обл.":    ["Владимир", "Ковров", "Муром", "Александров", "Вязники", "Гусь-Хрустальный"],
    "Воронежская обл.":     ["Воронеж", "Борисоглебск", "Россошь", "Лиски", "Семилуки", "Нововоронеж"],
    "Ивановская обл.":      ["Иваново", "Кинешма", "Шуя", "Вичуга", "Фурманов"],
    "Калужская обл.":       ["Калуга", "Обнинск", "Людиново", "Малоярославец", "Козельск"],
    "Костромская обл.":     ["Кострома", "Шарья", "Буй", "Галич", "Нерехта"],
    "Курская обл.":         ["Курск", "Железногорск", "Курчатов", "Щигры", "Льгов"],
    "Липецкая обл.":        ["Липецк", "Елец", "Грязи", "Данков", "Лебедянь"],
    "Орловская обл.":       ["Орёл", "Ливны", "Мценск", "Болхов"],
    "Рязанская обл.":       ["Рязань", "Касимов", "Сасово", "Скопин", "Ряжск"],
    "Смоленская обл.":      ["Смоленск", "Вязьма", "Сафоново", "Рославль", "Десногорск"],
    "Тамбовская обл.":      ["Тамбов", "Мичуринск", "Котовск", "Рассказово", "Моршанск"],
    "Тверская обл.":        ["Тверь", "Ржев", "Вышний Волочёк", "Кимры", "Конаково"],
    "Тульская обл.":        ["Тула", "Новомосковск", "Донской", "Алексин", "Щёкино"],
    "Ярославская обл.":     ["Ярославль", "Рыбинск", "Переславль-Залесский", "Тутаев", "Углич"],
    # ── Северо-Западный федеральный округ ────────────────────────────────────
    "Санкт-Петербург":      ["Санкт-Петербург", "Колпино", "Пушкин", "Петергоф", "Красное Село"],
    "Ленинградская обл.":   ["Гатчина", "Выборг", "Тихвин", "Сосновый Бор", "Кириши", "Волхов", "Всеволожск"],
    "Архангельская обл.":   ["Архангельск", "Северодвинск", "Котлас", "Коряжма", "Новодвинск"],
    "Вологодская обл.":     ["Вологда", "Череповец", "Великий Устюг", "Сокол", "Бабаево"],
    "Калининградская обл.": ["Калининград", "Черняховск", "Советск", "Балтийск", "Гусев"],
    "Карелия":              ["Петрозаводск", "Сортавала", "Кондопога", "Сегежа", "Костомукша"],
    "Коми":                 ["Сыктывкар", "Ухта", "Воркута", "Инта", "Печора"],
    "Мурманская обл.":      ["Мурманск", "Апатиты", "Мончегорск", "Североморск", "Кандалакша"],
    "Новгородская обл.":    ["Великий Новгород", "Боровичи", "Старая Русса", "Валдай"],
    "Псковская обл.":       ["Псков", "Великие Луки", "Остров", "Порхов"],
    # ── Южный федеральный округ ───────────────────────────────────────────────
    "Краснодарский край":   ["Краснодар", "Сочи", "Новороссийск", "Армавир", "Ейск", "Анапа", "Геленджик", "Тимашёвск", "Кропоткин"],
    "Волгоградская обл.":   ["Волгоград", "Волжский", "Камышин", "Михайловка", "Урюпинск", "Фролово", "Калач-на-Дону", "Николаевск"],
    "Астраханская обл.":    ["Астрахань", "Ахтубинск", "Знаменск", "Нариманов", "Камызяк"],
    "Ростовская обл.":      ["Ростов-на-Дону", "Таганрог", "Шахты", "Новочеркасск", "Волгодонск", "Батайск", "Новошахтинск", "Каменск-Шахтинский"],
    "Адыгея":               ["Майкоп", "Адыгейск", "Белореченск"],
    "Калмыкия":             ["Элиста", "Городовиковск", "Лагань"],
    "Крым":                 ["Симферополь", "Севастополь", "Ялта", "Керчь", "Евпатория", "Феодосия", "Джанкой"],
    # ── Северо-Кавказский федеральный округ ──────────────────────────────────
    "Ставропольский край":  ["Ставрополь", "Пятигорск", "Кисловодск", "Невинномысск", "Ессентуки", "Михайловск", "Будённовск"],
    "Дагестан":             ["Махачкала", "Дербент", "Хасавюрт", "Каспийск", "Избербаш"],
    "Кабардино-Балкария":   ["Нальчик", "Прохладный", "Баксан", "Нарткала"],
    "Карачаево-Черкесия":   ["Черкесск", "Карачаевск", "Усть-Джегута"],
    "Северная Осетия":      ["Владикавказ", "Моздок", "Беслан", "Алагир"],
    "Чечня":                ["Грозный", "Гудермес", "Аргун", "Шали"],
    "Ингушетия":            ["Магас", "Назрань", "Малгобек", "Карабулак"],
    # ── Приволжский федеральный округ ────────────────────────────────────────
    "Татарстан":            ["Казань", "Набережные Челны", "Нижнекамск", "Альметьевск", "Зеленодольск", "Бугульма", "Елабуга", "Лениногорск"],
    "Башкортостан":         ["Уфа", "Стерлитамак", "Салават", "Нефтекамск", "Октябрьский", "Белебей", "Туймазы", "Ишимбай"],
    "Нижегородская обл.":   ["Нижний Новгород", "Дзержинск", "Арзамас", "Саров", "Бор", "Кстово", "Павлово", "Выкса"],
    "Самарская обл.":       ["Самара", "Тольятти", "Сызрань", "Новокуйбышевск", "Чапаевск", "Жигулёвск", "Кинель", "Отрадный"],
    "Саратовская обл.":     ["Саратов", "Энгельс", "Балаково", "Балашов", "Вольск", "Ртищево"],
    "Пермский край":        ["Пермь", "Березники", "Соликамск", "Чайковский", "Лысьва", "Кунгур", "Краснокамск"],
    "Оренбургская обл.":    ["Оренбург", "Орск", "Новотроицк", "Бузулук", "Бугуруслан"],
    "Пензенская обл.":      ["Пенза", "Кузнецк", "Заречный", "Сердобск", "Нижний Ломов"],
    "Кировская обл.":       ["Киров", "Кирово-Чепецк", "Вятские Поляны", "Слободской", "Котельнич"],
    "Удмуртия":             ["Ижевск", "Сарапул", "Воткинск", "Глазов", "Можга"],
    "Чувашия":              ["Чебоксары", "Новочебоксарск", "Канаш", "Алатырь", "Шумерля"],
    "Марий Эл":             ["Йошкар-Ола", "Волжск", "Козьмодемьянск"],
    "Мордовия":             ["Саранск", "Рузаевка", "Ковылкино", "Краснослободск"],
    "Ульяновская обл.":     ["Ульяновск", "Димитровград", "Инза", "Барыш", "Сенгилей"],
    # ── Уральский федеральный округ ───────────────────────────────────────────
    "Свердловская обл.":    ["Екатеринбург", "Нижний Тагил", "Каменск-Уральский", "Первоуральск", "Серов", "Новоуральск", "Асбест", "Берёзовский"],
    "Челябинская обл.":     ["Челябинск", "Магнитогорск", "Миасс", "Озёрск", "Троицк", "Копейск", "Златоуст", "Сатка", "Кыштым"],
    "Тюменская обл.":       ["Тюмень", "Тобольск", "Ишим", "Ялуторовск"],
    "Курганская обл.":      ["Курган", "Шадринск", "Далматово", "Катайск"],
    "ХМАО":                 ["Сургут", "Нижневартовск", "Нефтеюганск", "Ханты-Мансийск", "Когалым", "Лангепас"],
    "ЯНАО":                 ["Новый Уренгой", "Ноябрьск", "Нефтеюганск", "Салехард", "Надым", "Муравленко"],
    # ── Сибирский федеральный округ ───────────────────────────────────────────
    "Новосибирская обл.":   ["Новосибирск", "Бердск", "Искитим", "Куйбышев", "Барабинск", "Татарск"],
    "Красноярский край":    ["Красноярск", "Норильск", "Ачинск", "Железногорск", "Канск", "Минусинск", "Зеленогорск"],
    "Кемеровская обл.":     ["Кемерово", "Новокузнецк", "Прокопьевск", "Белово", "Ленинск-Кузнецкий", "Киселёвск", "Юрга", "Анжеро-Судженск"],
    "Иркутская обл.":       ["Иркутск", "Братск", "Ангарск", "Усть-Илимск", "Шелехов", "Усолье-Сибирское"],
    "Омская обл.":          ["Омск", "Тара", "Калачинск", "Исилькуль"],
    "Томская обл.":         ["Томск", "Северск", "Стрежевой", "Асино", "Колпашево"],
    "Алтайский край":       ["Барнаул", "Бийск", "Рубцовск", "Новоалтайск", "Заринск"],
    "Алтай":                ["Горно-Алтайск", "Майма", "Горняк"],
    "Хакасия":              ["Абакан", "Черногорск", "Саяногорск", "Абаза", "Сорск"],
    "Тыва":                 ["Кызыл", "Ак-Довурак", "Туран"],
    "Забайкальский край":   ["Чита", "Краснокаменск", "Борзя", "Петровск-Забайкальский"],
    "Бурятия":              ["Улан-Удэ", "Северобайкальск", "Гусиноозёрск", "Бабушкин"],
    # ── Дальневосточный федеральный округ ────────────────────────────────────
    "Приморский край":      ["Владивосток", "Находка", "Уссурийск", "Артём", "Арсеньев", "Партизанск"],
    "Хабаровский край":     ["Хабаровск", "Комсомольск-на-Амуре", "Амурск", "Николаевск-на-Амуре"],
    "Сахалинская обл.":     ["Южно-Сахалинск", "Корсаков", "Холмск", "Оха", "Невельск"],
    "Амурская обл.":        ["Благовещенск", "Белогорск", "Свободный", "Тында", "Зея"],
    "Якутия":               ["Якутск", "Мирный", "Нерюнгри", "Ленск", "Алдан"],
    "Камчатский край":      ["Петропавловск-Камчатский", "Елизово", "Вилючинск"],
    "Магаданская обл.":     ["Магадан", "Сусуман", "Омсукчан"],
    "Еврейская АО":         ["Биробиджан", "Облучье"],
    "Тверская обл.":        ["Тверь", "Ржев", "Вышний Волочёк", "Кимры", "Конаково"],
    "Ярославская обл.":     ["Ярославль", "Рыбинск", "Переславль-Залесский", "Тутаев", "Углич"],
    "Калининградская обл.": ["Калининград", "Черняховск", "Советск", "Балтийск", "Гусев"],
}

# ── Клавиатуры ────────────────────────────────────────────────────────────────

def _main_menu_kb() -> InlineKeyboardMarkup:
    """
    Главный экран построен вокруг Copart. Российские площадки временно
    убраны в отдельное подменю одной кнопкой внизу — они продолжают
    работать, просто не занимают первый экран.
    """
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
        [InlineKeyboardButton(text="🇷🇺 Другие площадки", callback_data="ru_menu")],
    ])


def _ru_menu_kb() -> InlineKeyboardMarkup:
    """Подменю российских площадок — всё, что раньше было на главном экране."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Фильтры по России", callback_data="ru_filters:0")],
        [InlineKeyboardButton(text="➕ Новый фильтр по России", callback_data="filter_add")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")],
    ])


@router.callback_query(F.data == "ru_menu")
async def cb_ru_menu(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    try:
        await call.message.edit_text(
            "🇷🇺 <b>Российские площадки</b>\n"
            "<i>🔵 Auto.ru · 🟢 Авито · 🟠 Дром</i>\n\n"
            "Временно убраны с главного экрана — основной источник теперь "
            "Copart. Уже созданные фильтры продолжают работать и присылать "
            "объявления как обычно.",
            parse_mode="HTML",
            reply_markup=_ru_menu_kb(),
        )
    except Exception:
        pass
    await call.answer()


def split_by_kind(filters: list) -> tuple[list, list]:
    """Разделить фильтры на аукционные и российские."""
    copart = [f for f in filters if _is_copart_filter(f)]
    ru     = [f for f in filters if not _is_copart_filter(f)]
    return copart, ru


def _filters_kb(filters: list, page: int = 0, kind: str = "copart",
                ru_count: int = 0) -> InlineKeyboardMarkup:
    """
    Список фильтров одного типа. Российские вынесены в отдельный список,
    чтобы главный экран оставался про Copart.
    """
    nav_cb = "filters_list" if kind == "copart" else "ru_filters"

    start  = page * PAGE_SIZE
    chunk  = filters[start: start + PAGE_SIZE]
    total  = len(filters)
    pages  = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

    rows = []
    for f in chunk:
        icon  = "✅" if f["is_active"] else "⏸"
        kind_icon = "🟡 " if _is_copart_filter(f) else ""
        label = f"{icon} {kind_icon}{f['name']}"
        if f["brand"]:
            label += f"  ({f['brand']}"
            label += f" {f['model']}" if f["model"] else ""
            label += ")"
        rows.append([
            InlineKeyboardButton(text=label, callback_data=f"filter_info:{f['id']}")
        ])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"{nav_cb}:{page-1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"{nav_cb}:{page+1}"))
    if nav:
        rows.append(nav)

    if kind == "copart":
        rows.append([
            InlineKeyboardButton(text="➕🟡 Новый фильтр", callback_data="copart_add"),
            InlineKeyboardButton(text="🏠 Меню",           callback_data="main_menu"),
        ])
        if ru_count:
            rows.append([InlineKeyboardButton(
                text=f"🇷🇺 Другие площадки ({ru_count})", callback_data="ru_filters:0")])
    else:
        rows.append([
            InlineKeyboardButton(text="➕ Новый фильтр", callback_data="filter_add"),
            InlineKeyboardButton(text="◀️ Назад",        callback_data="ru_menu"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _filter_detail_kb(filter_id: int, is_active: bool,
                      is_copart: bool = False) -> InlineKeyboardMarkup:
    toggle = (
        InlineKeyboardButton(text="⏸ Пауза",    callback_data=f"filter_pause:{filter_id}")
        if is_active else
        InlineKeyboardButton(text="▶️ Включить", callback_data=f"filter_resume:{filter_id}")
    )
    rows = [[
        toggle,
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"filter_edit:{filter_id}"),
    ]]
    if is_copart:
        rows.append([InlineKeyboardButton(
            text="🔎 Проверить сейчас", callback_data=f"filter_check:{filter_id}")])
    rows.append([
        InlineKeyboardButton(text="🗑 Удалить",  callback_data=f"filter_delete:{filter_id}"),
        InlineKeyboardButton(text="◀️ К списку", callback_data="filters_list:0"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _edit_menu_kb(filter_id: int, is_copart: bool = False) -> InlineKeyboardMarkup:
    """Меню выбора поля для редактирования."""
    if is_copart:
        # У фильтра аукциона свой набор: без городов, КПП, кузова и источников
        copart_only = [
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
        for i in range(0, len(copart_only), 2):
            row = [InlineKeyboardButton(
                text=copart_only[i][0],
                callback_data=f"edit_field:{filter_id}:{copart_only[i][1]}")]
            if i + 1 < len(copart_only):
                row.append(InlineKeyboardButton(
                    text=copart_only[i + 1][0],
                    callback_data=f"edit_field:{filter_id}:{copart_only[i + 1][1]}"))
            rows.append(row)
        rows.append([InlineKeyboardButton(text="◀️ Назад",
                                          callback_data=f"filter_info:{filter_id}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

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
    # Поля, которые работают только с Copart
    copart_fields = [
        ("🗓 Аукцион с",   "auction_date_from"),
        ("🗓 Аукцион по",  "auction_date_to"),
        ("📄 Документ",    "title_groups"),
        ("💥 Исключить",   "damage_exclude"),
        ("🏁 Площадки",    "yards"),
        ("🚀 На ходу",     "run_and_drive"),
        ("⚡️ Купить сразу", "buy_now_only"),
    ]
    def pairs(items: list) -> list:
        out = []
        for i in range(0, len(items), 2):
            row = [InlineKeyboardButton(
                text=items[i][0],
                callback_data=f"edit_field:{filter_id}:{items[i][1]}",
            )]
            if i + 1 < len(items):
                row.append(InlineKeyboardButton(
                    text=items[i + 1][0],
                    callback_data=f"edit_field:{filter_id}:{items[i + 1][1]}",
                ))
            out.append(row)
        return out

    rows = pairs(fields)
    rows.append([InlineKeyboardButton(text="— 🟡 только Copart —", callback_data="noop")])
    rows += pairs(copart_fields)
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"filter_info:{filter_id}")])
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


def _regions_list_kb(selected: list[str]) -> InlineKeyboardMarkup:
    """Список регионов — первый уровень выбора."""
    rows = []
    regions = list(REGION_CITIES.keys())
    for i in range(0, len(regions), 2):
        row = []
        for r in regions[i:i+2]:
            # Показываем сколько городов из этого региона уже выбрано
            count = sum(1 for c in REGION_CITIES[r] if c in selected)
            label = f"✅ {r} ({count})" if count else r
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"fsm_region_open:{r}",
            ))
        rows.append(row)
    total = len(selected)
    rows.append([
        InlineKeyboardButton(
            text=f"✔️ Готово ({total} город{'ов' if total != 1 else ''})" if total else "⏭ Пропустить (все города)",
            callback_data="fsm_city_done",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cities_in_region_kb(region: str, selected: list[str]) -> InlineKeyboardMarkup:
    """Города конкретного региона — второй уровень."""
    cities = REGION_CITIES.get(region, [])
    rows = []
    for i in range(0, len(cities), 2):
        row = []
        for c in cities[i:i+2]:
            check = "✅ " if c in selected else ""
            row.append(InlineKeyboardButton(
                text=f"{check}{c}",
                callback_data=f"fsm_city_toggle:{c}",
            ))
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="◀️ Назад к регионам", callback_data="fsm_regions_back")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cities_kb(selected: list[str]) -> InlineKeyboardMarkup:
    """Алиас для обратной совместимости — открывает список регионов."""
    return _regions_list_kb(selected)


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

def _is_copart_filter(f) -> bool:
    return (_opt(f, "kind") or "ru") == "copart"


def _render_filter(f) -> str:
    if _is_copart_filter(f):
        return _render_copart_filter(f)

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

    af, at = _opt(f, "auction_date_from"), _opt(f, "auction_date_to")
    if af or at:
        lines.append(f"🗓 <b>Аукцион:</b>  {af or '—'} – {at or '—'}")

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

    if _opt(f, "run_and_drive"):
        lines.append("🚀 <b>Только на ходу</b>")
    if _opt(f, "buy_now_only"):
        lines.append("⚡️ <b>Только «купить сразу»</b>")

    lines.append(f"<code>{'─' * 24}</code>")
    lines.append(f"📡 {srcs}")
    lines.append(f"🔘 {status}")
    return "\n".join(lines)


def _render_copart_filter(f) -> str:
    """Карточка отдельного фильтра аукциона: доллары, мили, без городов."""
    status = "✅ Активен" if f["is_active"] else "⏸ Приостановлен"

    def usd(v):
        return "$" + f"{v:,}".replace(",", " ") if v else "—"

    lines = [
        f"🟡 <b>{f['name']}</b>",
        "<i>фильтр аукциона Copart</i>",
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
        "и расчёт стоимости «под ключ». Кнопка ниже.\n\n"

        "<i>🇷🇺 Auto.ru, Авито и Дром временно убраны с главного экрана — "
        "они под кнопкой «Другие площадки». Созданные раньше фильтры "
        "продолжают работать.</i>",
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
    try:
        await call.message.edit_text("🏠 <b>Главное меню</b>", parse_mode="HTML", reply_markup=_main_menu_kb())
    except Exception:
        pass
    await call.answer()


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _is_owner(message.from_user.id):
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
        "/status — статистика\n\n"

        "<i>🇷🇺 Auto.ru, Авито и Дром — под кнопкой «Другие площадки». "
        "Прежние фильтры работают, просто не занимают главный экран.</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟡 Как настроить Copart",
                                  callback_data="copart_help")],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
        ]),
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
    copart, ru = split_by_kind(filters)
    await message.answer(
        _filters_header(copart, ru),
        parse_mode="HTML",
        reply_markup=_filters_kb(copart, 0, "copart", len(ru)),
    )


def _filters_header(copart: list, ru: list) -> str:
    head = f"🟡 <b>Фильтры Copart</b>  <i>({len(copart)} шт.)</i>"
    if ru:
        head += f"\n<i>Ещё {len(ru)} по российским площадкам — кнопка внизу</i>"
    return head


EMPTY_COPART_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕🟡 Создать фильтр Copart", callback_data="copart_add")],
    [InlineKeyboardButton(text="🇷🇺 Другие площадки", callback_data="ru_menu")],
    [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")],
])


@router.callback_query(F.data.startswith("filters_list:"))
async def cb_filters_list(call: CallbackQuery):
    """Основной список — фильтры Copart."""
    page    = int(call.data.split(":")[1])
    filters = await get_active_filters(OWNER_ID)
    copart, ru = split_by_kind(filters)

    if not copart:
        note = (f"\n\nПо российским площадкам есть {len(ru)} — "
                f"они в «Другие площадки»." if ru else "")
        await call.message.edit_text(
            f"🟡 <b>Фильтров Copart пока нет.</b>{note}",
            parse_mode="HTML",
            reply_markup=EMPTY_COPART_KB,
        )
    else:
        await call.message.edit_text(
            _filters_header(copart, ru),
            parse_mode="HTML",
            reply_markup=_filters_kb(copart, page, "copart", len(ru)),
        )
    await call.answer()


@router.callback_query(F.data.startswith("ru_filters:"))
async def cb_ru_filters(call: CallbackQuery):
    """Список фильтров по российским площадкам — за отдельной кнопкой."""
    page    = int(call.data.split(":")[1])
    filters = await get_active_filters(OWNER_ID)
    _, ru = split_by_kind(filters)

    if not ru:
        await call.message.edit_text(
            "🇷🇺 <b>Фильтров по российским площадкам нет.</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Создать", callback_data="filter_add")],
                [InlineKeyboardButton(text="◀️ Назад",  callback_data="ru_menu")],
            ]),
        )
    else:
        await call.message.edit_text(
            f"🇷🇺 <b>Фильтры по России</b>  <i>({len(ru)} шт.)</i>\n"
            f"<i>🔵 Auto.ru · 🟢 Авито · 🟠 Дром</i>",
            parse_mode="HTML",
            reply_markup=_filters_kb(ru, page, "ru"),
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
        reply_markup=_filter_detail_kb(filter_id, f["is_active"], _is_copart_filter(f)),
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
                                     reply_markup=_filter_detail_kb(filter_id, f["is_active"], _is_copart_filter(f)))


@router.callback_query(F.data.startswith("filter_resume:"))
async def cb_filter_resume(call: CallbackQuery):
    filter_id = int(call.data.split(":")[1])
    await toggle_filter(filter_id, OWNER_ID, active=True)
    await call.answer("✅ Активен")
    f = await get_filter_by_id(filter_id, OWNER_ID)
    if f:
        await call.message.edit_text(_render_filter(f), parse_mode="HTML",
                                     reply_markup=_filter_detail_kb(filter_id, f["is_active"], _is_copart_filter(f)))


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
    is_cp = _is_copart_filter(f)
    head = "🟡 " if is_cp else "✏️ "
    await call.message.edit_text(
        f"{head}<b>Редактирование: «{f['name']}»</b>\n\nЧто хочешь изменить?",
        parse_mode="HTML",
        reply_markup=_edit_menu_kb(filter_id, is_copart=is_cp),
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
            reply_markup=_sources_kb(
                "edit_val",
                [InlineKeyboardButton(text="◀️ Отмена",
                                      callback_data=f"filter_edit:{filter_id}")],
            ),
        )
    elif field in MULTI_FIELDS:
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
    elif field == "cities":
        current = list(f["cities"] or [])
        await state.update_data(edit_cities=current)
        await call.message.edit_text(
            f"📍 Выбери города (можно несколько).\nВыбрано: {', '.join(current) if current else 'все'}",
            reply_markup=_regions_list_kb(current),
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
            "auction_date_from": "🗓 Аукцион не раньше — дата в формате "
                                 "<code>ГГГГ-ММ-ДД</code> (или «-»)\n"
                                 "<i>Только для Copart</i>",
            "auction_date_to":   "🗓 Аукцион не позже — дата в формате "
                                 "<code>ГГГГ-ММ-ДД</code> (или «-»)\n"
                                 "<i>Только для Copart</i>",
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
    elif field == "sources":
        value = SOURCE_SETS[val_raw]
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
        reply_markup=_filter_detail_kb(filter_id, f["is_active"], _is_copart_filter(f)),
    )
    await call.answer("✅ Сохранено")


@router.message(StateFilter(EditForm.entering_value))
async def fsm_edit_text(message: Message, state: FSMContext):
    data      = await state.get_data()
    filter_id = data["edit_filter_id"]
    field     = data["edit_field"]
    raw       = message.text.strip()

    int_fields = {"year_from", "year_to", "price_from", "price_to", "mileage_from", "mileage_to"}
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

    await update_filter_field(filter_id, OWNER_ID, field, value)
    await state.clear()

    f = await get_filter_by_id(filter_id, OWNER_ID)
    await message.answer(
        _render_filter(f),
        parse_mode="HTML",
        reply_markup=_filter_detail_kb(filter_id, f["is_active"], _is_copart_filter(f)),
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

    await update_filter_field(filter_id, OWNER_ID, field, selected or None)
    await state.clear()

    f = await get_filter_by_id(filter_id, OWNER_ID)
    await call.message.edit_text(
        _render_filter(f),
        parse_mode="HTML",
        reply_markup=_filter_detail_kb(filter_id, f["is_active"], _is_copart_filter(f)),
    )
    await call.answer("✅ Сохранено")


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
                                     reply_markup=_filter_detail_kb(filter_id, f["is_active"], _is_copart_filter(f)))
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
                                 reply_markup=_filter_detail_kb(filter_id, f["is_active"], _is_copart_filter(f)))
    await call.answer("✅ Сохранено")


# ── Города при редактировании ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fsm_region_open:"), StateFilter(EditForm.entering_value))
async def cb_edit_region_open(call: CallbackQuery, state: FSMContext):
    region = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("edit_cities", []))
    await state.update_data(edit_current_region=region)
    await call.message.edit_text(
        f"📍 <b>{region}</b>\nВыбери города:",
        parse_mode="HTML",
        reply_markup=_cities_in_region_kb(region, selected),
    )
    await call.answer()


@router.callback_query(F.data == "fsm_regions_back", StateFilter(EditForm.entering_value))
async def cb_edit_regions_back(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(data.get("edit_cities", []))
    filter_id = data.get("edit_filter_id")
    await call.message.edit_text(
        f"📍 Выбери города (можно несколько).\nВыбрано: {', '.join(selected) if selected else 'все'}",
        reply_markup=_regions_list_kb(selected),
    )
    await call.answer()


@router.callback_query(F.data.startswith("fsm_city_toggle:"), StateFilter(EditForm.entering_value))
async def cb_edit_city_toggle(call: CallbackQuery, state: FSMContext):
    city = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("edit_cities", []))
    region = data.get("edit_current_region", "")
    if city in selected:
        selected.remove(city)
    else:
        selected.append(city)
    await state.update_data(edit_cities=selected)
    await call.message.edit_reply_markup(reply_markup=_cities_in_region_kb(region, selected))
    await call.answer()


@router.callback_query(F.data == "fsm_city_done", StateFilter(EditForm.entering_value))
async def cb_edit_city_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    filter_id = data["edit_filter_id"]
    selected  = data.get("edit_cities", [])
    await update_filter_field(filter_id, OWNER_ID, "cities", selected if selected else None)
    await state.clear()
    f = await get_filter_by_id(filter_id, OWNER_ID)
    await call.message.edit_text(_render_filter(f), parse_mode="HTML",
                                 reply_markup=_filter_detail_kb(filter_id, f["is_active"], _is_copart_filter(f)))
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
              "Выбери регион, затем города внутри него.\nМожно выбрать города из нескольких регионов."),
        parse_mode="HTML",
        reply_markup=_regions_list_kb(selected),
    )


@router.callback_query(F.data.startswith("fsm_region_open:"), StateFilter(FilterForm.cities))
async def cb_fsm_region_open(call: CallbackQuery, state: FSMContext):
    region = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("fsm_cities", []))
    await state.update_data(fsm_current_region=region)
    await call.message.edit_text(
        f"📍 <b>{region}</b>\nВыбери города:",
        parse_mode="HTML",
        reply_markup=_cities_in_region_kb(region, selected),
    )
    await call.answer()


@router.callback_query(F.data == "fsm_regions_back", StateFilter(FilterForm.cities))
async def cb_fsm_regions_back(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = list(data.get("fsm_cities", []))
    await call.message.edit_text(
        _step(10, 13, "Шаг 10 — Города",
              "Выбери регион, затем города внутри него. Можно выбрать города из нескольких регионов."),
        parse_mode="HTML",
        reply_markup=_regions_list_kb(selected),
    )
    await call.answer()


@router.callback_query(F.data.startswith("fsm_city_toggle:"), StateFilter(FilterForm.cities))
async def cb_fsm_city_toggle(call: CallbackQuery, state: FSMContext):
    city = call.data.split(":", 1)[1]
    data = await state.get_data()
    selected = list(data.get("fsm_cities", []))
    region = data.get("fsm_current_region", "")
    if city in selected:
        selected.remove(city)
    else:
        selected.append(city)
    await state.update_data(fsm_cities=selected)
    await call.message.edit_reply_markup(reply_markup=_cities_in_region_kb(region, selected))
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
        _step(13, 13, "Шаг 13 — Источники",
              "Где искать?\n<i>🟡 Copart — аукцион битых авто из США, "
              "цены в долларах</i>"),
        parse_mode="HTML",
        reply_markup=_sources_kb("fsm_src"),
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
    sources = SOURCE_SETS[val]
    await _finish_filter(call.message, state, sources, from_call=True)
    await call.answer()


@router.message(StateFilter(FilterForm.sources))
async def fsm_sources_text(message: Message, state: FSMContext):
    val = message.text.strip().lower()
    known = set(SOURCE_LABELS)
    sources = ["autoru", "drom"] if val == "-" else [
        s.strip() for s in val.split(",") if s.strip() in known
    ]
    if not sources:
        await message.answer(
            "⚠️ Укажи хотя бы один источник: autoru, drom, avito, copart"
        )
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
    if not _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    await state.clear()
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


def _catalog_kb(items: list, page: int, pick: str, nav: str,
                any_cb: str) -> InlineKeyboardMarkup:
    """Страница справочника: кнопки «НАЗВАНИЕ · N» по две в ряд."""
    pages = max(1, (len(items) + CATALOG_PAGE - 1) // CATALOG_PAGE)
    page = max(0, min(page, pages - 1))
    start = page * CATALOG_PAGE

    rows = []
    chunk = items[start:start + CATALOG_PAGE]
    for i in range(0, len(chunk), 2):
        row = []
        for j, (name, count) in enumerate(chunk[i:i + 2]):
            label = name if len(name) <= 16 else name[:15] + "…"
            row.append(InlineKeyboardButton(
                text=f"{label} · {count}",
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

    rows.append([InlineKeyboardButton(text="⏭ Не важно", callback_data=any_cb)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_makes(msg, state: FSMContext, page: int = 0, edit: bool = False):
    makes = await fetch_makes()
    text = _cp_step(
        2, "Шаг 2 — Марка",
        f"Список берётся прямо с аукциона — {len(makes)} марок, "
        f"рядом число лотов.\nМожно и отправить текстом: <code>TOYOTA</code>",
    )
    kb = _catalog_kb(makes, page, "cpw_mk", "cpw_mk_pg", "cpw_mk_any")
    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


async def _show_models(msg, state: FSMContext, page: int = 0, edit: bool = False):
    data = await state.get_data()
    brand = data.get("brand")

    if not brand:
        text = _cp_step(3, "Шаг 3 — Модель",
                        "Марка не выбрана — отправь модель текстом или пропусти.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏭ Не важно", callback_data="cpw_md_any")
        ]])
    else:
        models = await fetch_models(brand)
        if not models:
            text = _cp_step(3, "Шаг 3 — Модель",
                            f"Для <b>{brand}</b> список моделей не пришёл. "
                            f"Отправь текстом или пропусти.")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⏭ Не важно", callback_data="cpw_md_any")
            ]])
        else:
            text = _cp_step(3, f"Шаг 3 — Модель {brand}",
                            f"{len(models)} моделей на аукционе, рядом число лотов.")
            kb = _catalog_kb(models, page, "cpw_md", "cpw_md_pg", "cpw_md_any")

    if edit:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await msg.answer(text, parse_mode="HTML", reply_markup=kb)


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
    # Справочник закэширован, поэтому индекс разрешаем без обращения к сети
    makes = await fetch_makes()
    idx = int(call.data.split(":")[1])
    brand = makes[idx][0] if 0 <= idx < len(makes) else None
    await _cpw_set_brand(call.message, state, brand, edit=True)
    await call.answer()


@router.callback_query(F.data == "cpw_mk_any", StateFilter(CopartForm.brand))
async def cpw_make_any(call: CallbackQuery, state: FSMContext):
    await _cpw_set_brand(call.message, state, None, edit=True)
    await call.answer()


@router.message(StateFilter(CopartForm.brand))
async def cpw_brand_text(message: Message, state: FSMContext):
    raw = message.text.strip().upper()
    await _cpw_set_brand(message, state, None if raw == "-" else raw, edit=False)


async def _cpw_set_brand(msg, state: FSMContext, brand, edit: bool):
    await state.update_data(brand=brand)
    await state.set_state(CopartForm.model)

    if brand and brand in MAKES_NOT_ON_COPART:
        await state.update_data(brand=brand)
        text = _cp_step(3, "Шаг 3 — Модель",
                        f"⚠️ <b>{brand}</b> на Copart не встречается — это рынок США. "
                        f"Фильтр создастся, но лотов не будет.\n\n"
                        f"Отправь модель текстом или пропусти.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏭ Не важно", callback_data="cpw_md_any")
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
    models = await fetch_models(data.get("brand") or "")
    idx = int(call.data.split(":")[1])
    model = models[idx][0] if 0 <= idx < len(models) else None
    await _cpw_next_after_model(call.message, state, model, edit=True)
    await call.answer()


@router.callback_query(F.data == "cpw_md_any", StateFilter(CopartForm.model))
async def cpw_model_any(call: CallbackQuery, state: FSMContext):
    await _cpw_next_after_model(call.message, state, None, edit=True)
    await call.answer()


@router.message(StateFilter(CopartForm.model))
async def cpw_model(message: Message, state: FSMContext):
    raw = message.text.strip().upper()
    await _cpw_next_after_model(message, state, None if raw == "-" else raw, edit=False)


async def _cpw_next_after_model(msg, state: FSMContext, model, edit: bool):
    await state.update_data(model=model)
    await state.set_state(CopartForm.year_from)
    text = _cp_step(4, "Шаг 4 — Год от", "Например: <code>2015</code>")
    if edit:
        await msg.edit_text(text, parse_mode="HTML")
    else:
        await msg.answer(text, parse_mode="HTML")


@router.message(StateFilter(CopartForm.year_from))
async def cpw_year_from(message: Message, state: FSMContext):
    val = _parse_int_or_none(message.text)
    if val is False:
        await message.answer("⚠️ Введи число или «-»")
        return
    await state.update_data(year_from=val)
    await state.set_state(CopartForm.year_to)
    await message.answer(
        _cp_step(5, "Шаг 5 — Год до", "Например: <code>2022</code>"),
        parse_mode="HTML",
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
        _cp_step(6, "Шаг 6 — Цена от, $",
                 "Цена <b>в долларах</b> — так же, как на аукционе.\n"
                 "Например: <code>3000</code>\n\n"
                 "<i>Это оценочная стоимость лота либо цена «купить сразу», "
                 "а не текущая ставка — ставки Copart показывает "
                 "только зарегистрированным.</i>"),
        parse_mode="HTML",
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
        _cp_step(7, "Шаг 7 — Цена до, $", "Например: <code>12000</code>"),
        parse_mode="HTML",
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
        _cp_step(8, "Шаг 8 — Пробег до, миль",
                 "Одометр на аукционе <b>в милях</b>, поэтому и здесь мили.\n"
                 "Например: <code>90000</code>  (это примерно 145 000 км)"),
        parse_mode="HTML",
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
        id=0, user_id=OWNER_ID, name=data.get("name") or "проверка", kind="copart",
        brand=data.get("brand"), model=data.get("model"),
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
        user_id=OWNER_ID,
        name=data["name"],
        kind="copart",
        brand=data.get("brand"),
        model=data.get("model"),
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


@router.callback_query(F.data.startswith("filter_check:"))
async def cb_filter_check(call: CallbackQuery):
    """Проверить сохранённый фильтр Copart — сколько лотов он ловит сейчас."""
    if not _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return

    filter_id = int(call.data.split(":")[1])
    record = await get_filter_by_id(filter_id, OWNER_ID)
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
    if not _is_owner(call.from_user.id):
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
    if not _is_owner(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return

    parts = call.data.split(":")
    group = parts[1] if len(parts) > 1 else "model"
    label = dict(STATS_GROUPS).get(group, group)

    rows = await copart_price_stats(group)
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
    if not _is_owner(call.from_user.id):
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
    if not _is_owner(message.from_user.id):
        return
    await state.clear()
    query = message.text.strip()

    rows = await find_lots(query)
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

    "<b>1. Включить источник</b>\n"
    "По умолчанию Copart <b>выключен</b> — его выбирают явно:\n"
    "• <b>новый фильтр:</b> «➕ Новый фильтр» → пройти шаги → на шаге 13 "
    "«Источники» нажать <b>«🟡 Copart»</b> (только аукцион) "
    "или <b>«🌍 Всё вместе»</b> (аукцион + российские площадки)\n"
    "• <b>готовый фильтр:</b> «📋 Мои фильтры» → выбрать фильтр → "
    "<b>«✏️ Изменить»</b> → <b>«📡 Источники»</b> → «🟡 Copart»\n\n"

    "<b>2. Заполнить поля</b>\n"
    "🚗 <b>Марка и модель</b> — только <b>латиницей</b>, как на самом аукционе: "
    "<code>CHEVROLET</code>, <code>TOYOTA</code>, <code>BMW</code>, "
    "<code>MERCEDES</code>, <code>FORD</code>.\n"
    "Если точной модели в справочнике Copart нет, бот сам поищет по марке "
    "и отберёт нужное по названию лота.\n\n"
    "💰 <b>Цена</b> — вводится в <b>рублях</b>, бот сам переводит в доллары. "
    "Учти: это <b>оценочная стоимость</b> авто, а не ставка на торгах — "
    "текущие ставки Copart показывает только зарегистрированным.\n\n"
    "🛣 <b>Пробег</b> — вводится в <b>километрах</b>, бот сам переводит в мили "
    "(одометр на аукционе в милях).\n\n"
    "📅 <b>Год</b> — как обычно.\n\n"
    "🗓 <b>Аукцион с / по</b> — только для Copart. Дата в формате "
    "<code>ГГГГ-ММ-ДД</code>, например <code>2026-09-01</code>. "
    "Оставь пустым, если дата торгов неважна.\n\n"
    "📍 <b>Города</b> — на Copart <b>не влияют</b>: площадки находятся в США "
    "и Канаде. Поле работает только для Auto.ru и Авито.\n\n"

    "<b>3. Чего там нет</b>\n"
    "Это рынок США, поэтому марок <b>Lada, Skoda, Renault, Geely, Chery</b> "
    "на аукционе не бывает — по ним бот запрос даже не отправляет.\n\n"

    "<b>4. Что придёт</b>\n"
    "Номер лота, марка и год, оценка в $, пробег в милях, "
    "характер повреждения, дата торгов по Москве, площадка хранения "
    "и прямая ссылка на лот.\n\n"

    "Все найденные лоты — кнопка <b>«🟡 Copart»</b> в главном меню "
    "или одноимённый раздел в Mini App."
)


@router.callback_query(F.data == "copart_help")
async def cb_copart_help(call: CallbackQuery):
    if not _is_owner(call.from_user.id):
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
    if not _is_owner(call.from_user.id):
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
        added = await add_favorite_from_seen(OWNER_ID, source, external_id)
    except Exception as e:
        logger.error(f"избранное: ошибка сохранения {source}/{external_id}: {e}")
        await call.answer("⚠️ Не удалось сохранить", show_alert=True)
        return

    if added:
        await call.answer("⭐️ Добавлено в избранное")
    elif await is_favorite(OWNER_ID, source, external_id):
        await call.answer("⭐️ Уже в избранном")
    else:
        # Строки в seen_listings нет — например, объявление успели вычистить
        await call.answer("⚠️ Объявление больше не найдено", show_alert=True)


@router.callback_query(F.data.startswith("hide:"))
async def cb_listing_hide(call: CallbackQuery):
    await call.message.delete()
    await call.answer("🚫 Скрыто")
