# parsers/copart.py — парсер аукциона Copart через его публичный JSON API
#
# Endpoint найден через DevTools (Network → Fetch/XHR) на странице поиска
# https://www.copart.com/lotSearchResults:
#
#   POST https://www.copart.com/public/lots/search-results
#   Content-Type: application/json
#
# Это тот же самый запрос, который делает сам сайт при поиске авто.
# Авторизация и капча не нужны, но есть нюанс с IP:
#   • с обычного (домашнего) IP endpoint отвечает JSON сразу;
#   • с IP дата-центра — например с Render — Incapsula может вернуть
#     HTML-заглушку со статусом 200. Поэтому сессия сначала прогревается
#     обычным заходом на страницу поиска (см. _copart_session), а если и это
#     не помогло, запрос повторяется через ScraperAPI при заданном ключе.
# (Поддомен api.copart.com отдаёт 403 — он для мобильных приложений с ключом.)
#
# Тело запроса:
#   {
#     "query":        ["*"],                       # либо текст при freeFormSearch
#     "filter":       {"MAKE": [...], "YEAR": [...]},   # Solr-выражения по группам
#     "sort":         ["auction_date_type asc"],
#     "page":         0,
#     "size":         100,
#     "start":        0,
#     "freeFormSearch": false,
#     ...
#   }
#
# Ответ: {"returnCode":1,"data":{"results":{"totalElements":N,"content":[ {лот}, ... ]}}}
#
# Группы фильтров (взяты из facetFields того же ответа):
#   MAKE  lot_make_desc:"CHEVROLET"
#   MODL  lot_model_desc:"CRUZE"
#   YEAR  lot_year:[2015 TO 2020]
#   ODM   odometer_reading_received:[0 TO 150000]        (в милях)
#   SDAT  auction_date_utc:[NOW TO NOW+7DAY]
#   LOC   yard_name:"FL - MIAMI"  либо  yard_name:FL*    (весь штат)
#   TITL  title_group_code:TITLEGROUP_C                  (C чистый / S salvage / J не восстановить)
#   FETI  lot_condition_code:CERT-D                      (Run and Drive — на ходу)
#   FETI  buy_it_now_code:B1                             (можно купить сразу)
#   PRID  damage_type_code:DAMAGECODE_FR                 (или -DAMAGECODE_BN — исключить)
#   TMTP  transmission_type:"AUTOMATIC"
#   BODY  body_style:"4DR SPOR"
#
# Несколько выражений внутри одной группы объединяются по ИЛИ,
# разные группы — по И. Отрицание пишется через «-» перед полем.
#
# Поля лота (сокращения Copart):
#   ln / lotNumberStr — номер лота        ld  — заголовок «2016 CHEVROLET CRUZE LT»
#   lcy — год                             mkn — марка          lm — модель
#   dd  — описание повреждения            tgd — тип документа («SALVAGE TITLE»)
#   orr — пробег (одометр, мили)          ord — ACTUAL / NOT ACTUAL
#   la  — оценочная стоимость             rc  — оценка стоимости ремонта
#   bnp — цена «купить сразу»             cuc — валюта (USD/CAD)
#   lcc — состояние лота, CERT-D = на ходу (Run and Drive)
#   hk  — ключи: YES / NO / EXEMPT        fv  — VIN (частично замаскирован)
#   ad  — дата аукциона (epoch ms)        at — время, tz — таймзона
#   yn  — площадка «FL - MIAMI»           locCity / locState / locCountry
#   ldu — slug для ссылки                 tims — картинка (см. IMAGE_SIZE)
#   tsmn — КПП                            ft — топливо, drv — привод, clr — цвет
#   egn — двигатель «2.4L  4»             cy — число цилиндров

import asyncio
import datetime
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import quote

import aiohttp

from config import USD_RUB_RATE, SCRAPER_API_KEY
from rates import cached_rate
from parsers.base import BaseParser, Listing, SearchFilter

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.copart.com/public/lots/search-results"
LOT_URL    = "https://www.copart.com/ru/lot/{lot}"

PAGE_SIZE = 100     # максимум, который отдаёт endpoint за один запрос
MAX_PAGES = 3       # 300 лотов на фильтр — дальше уже неактуальные торги
TIMEOUT   = aiohttp.ClientTimeout(total=45)

# Сколько лотов максимум забираем по одному фильтру за обход.
# Отдельная константа, чтобы снаружи не пересчитывать произведение —
# в bot/handlers.py свои PAGE_SIZE и MAX_PAGES для пагинации меню.
FETCH_LIMIT = PAGE_SIZE * MAX_PAGES

# Copart отдаёт одну картинку в трёх размерах — отличается только суффикс:
#   _thb ~5 КБ (превью)   _ful ~78 КБ (обычное)   _hrs ~270 КБ (высокое)
# В API приходит _thb; для карточек берём _ful — читаемо и не тяжело.
IMAGE_SIZE = "_ful"

# Тип документа: код группы → как показываем
TITLE_GROUPS = {
    "C": ("TITLEGROUP_C", "✅ Чистый документ"),
    "S": ("TITLEGROUP_S", "⚠️ Salvage (конструктивная гибель)"),
    "J": ("TITLEGROUP_J", "⛔ Восстановлению не подлежит"),
}

# Тип документа из поля лота tgd → короткая русская подпись
TITLE_RU = {
    "CLEAN TITLE":      "✅ Чистый документ",
    "SALVAGE TITLE":    "⚠️ Salvage",
    "NON-REPAIRABLE":   "⛔ Не восстановить",
    "NON REPAIRABLE":   "⛔ Не восстановить",
    "CERTIFICATE OF DESTRUCTION": "⛔ Под утилизацию",
}

# Наличие ключей
KEYS_RU = {"YES": "🔑 Ключи есть", "NO": "🚫 Без ключей", "EXEMPT": "🔑 Ключи не требуются"}

# Повреждения Copart — единая таблица, чтобы название в фильтре и в карточке
# не разъезжались: короткий код → (код для фильтра, значение поля dd, по-русски)
DAMAGE = {
    "AO": ("DAMAGECODE_AO", "ALL OVER",             "Круговые"),
    "BC": ("DAMAGECODE_BC", "BIOHAZARD/CHEMICAL",   "Биоопасность/химия"),
    "BN": ("DAMAGECODE_BN", "BURN",                 "Пожар"),
    "BE": ("DAMAGECODE_BE", "BURN - ENGINE",        "Пожар (двигатель)"),
    "BI": ("DAMAGECODE_BI", "BURN - INTERIOR",      "Пожар (салон)"),
    "DH": ("DAMAGECODE_DH", "DAMAGE HISTORY",       "История повреждений"),
    "FD": ("DAMAGECODE_FD", "FRAME DAMAGE",         "Повреждение рамы"),
    "FR": ("DAMAGECODE_FR", "FRONT END",            "Перед"),
    "HL": ("DAMAGECODE_HL", "HAIL",                 "Град"),
    "MC": ("DAMAGECODE_MC", "MECHANICAL",           "Механическая неисправность"),
    "MN": ("DAMAGECODE_MN", "MINOR DENT/SCRATCHES", "Мелкие вмятины/царапины"),
    "VI": ("DAMAGECODE_VI", "MISSING/ALTERED VIN",  "Отсутствует/изменён VIN"),
    "NW": ("DAMAGECODE_NW", "NORMAL WEAR",          "Обычный износ"),
    "PR": ("DAMAGECODE_PR", "PARTIAL REPAIR",       "Частично отремонтирован"),
    "RR": ("DAMAGECODE_RR", "REAR END",             "Зад"),
    "RJ": ("DAMAGECODE_RJ", "REJECTED REPAIR",      "Отклонённый ремонт"),
    "VP": ("DAMAGECODE_VP", "REPLACED VIN",         "Заменён VIN"),
    "RO": ("DAMAGECODE_RO", "ROLLOVER",             "Переворот"),
    "SD": ("DAMAGECODE_SD", "SIDE",                 "Бок"),
    "ST": ("DAMAGECODE_ST", "STRIPPED",             "Разукомплектован"),
    "TP": ("DAMAGECODE_TP", "TOP/ROOF",             "Крыша"),
    "UN": ("DAMAGECODE_UN", "UNDERCARRIAGE",        "Днище"),
    "UK": ("DAMAGECODE_UK", "UNKNOWN",              "Неизвестно"),
    "VN": ("DAMAGECODE_VN", "VANDALISM",            "Вандализм"),
    "WA": ("DAMAGECODE_WA", "WATER/FLOOD",          "Вода/потоп"),
}

# короткий код → (полный код, русское название) — для сборки фильтра и кнопок
DAMAGE_CODES = {k: (v[0], v[2]) for k, v in DAMAGE.items()}

# Повреждения, которые чаще всего хочется отсечь сразу
DAMAGE_JUNK = ["BN", "BE", "BI", "BC", "WA"]

# Штаты и провинции, где есть площадки Copart
YARD_STATES = [
    "AB", "AK", "AL", "AR", "AZ", "CA", "CN", "CO", "CT", "DC", "DE", "FL",
    "GA", "HI", "IA", "ID", "IL", "IN", "KS", "KY", "LA", "MA", "MD", "ME",
    "MI", "MN", "MO", "MS", "MT", "NB", "NC", "ND", "NE", "NH", "NJ", "NM",
    "NS", "NV", "NY", "OH", "OK", "ON", "OR", "PA", "QC", "RI", "SC", "SD",
    "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY",
]

# Привод и топливо — для строки характеристик
DRIVE_RU = {
    "ALL WHEEL DRIVE":        "Полный",
    "FOUR BY FOUR":           "4x4",
    "4X4 W/REAR WHEEL DRV":   "4x4",
    "FRONT WHEEL DRIVE":      "Передний",
    "REAR WHEEL DRIVE":       "Задний",
    "4X2":                    "4x2",
}
FUEL_RU = {
    "GAS": "Бензин", "GASOLINE": "Бензин", "DIESEL": "Дизель",
    "HYBRID ENGINE": "Гибрид", "HYBRID": "Гибрид", "ELECTRIC": "Электро",
    "FLEXIBLE FUEL": "Битопливный", "COMPRESSED NATURAL GAS": "Метан",
}

# Названия марок в каталоге бота → значения lot_make_desc у Copart.
# Несколько значений = OR внутри одной группы фильтра.
MAKE_MAP: dict[str, list[str]] = {
    "MERCEDES":   ["MERCEDES", "MERCEDES BENZ", "MERCEDES-BENZ"],
    "VW":         ["VOLKSWAGEN"],
    "CHEVY":      ["CHEVROLET"],
    "LAND ROVER": ["LAND ROVER", "LAND-ROVER"],
}

# Марок, которых на Copart нет вовсе (рынок США/Канады) — экономим запрос
MAKES_NOT_ON_COPART = {"LADA", "SKODA", "RENAULT", "GEELY", "CHERY", "UAZ", "GAZ"}

# Значение поля dd → по-русски. Собирается из DAMAGE, плюс варианты,
# которые встречаются в выдаче, но своего кода в справочнике не имеют.
DAMAGE_RU: dict[str, str] = {v[1]: v[2] for v in DAMAGE.values()}
DAMAGE_RU.update({
    "ELECTRICAL":     "Электрика",
    "SUSPENSION":     "Подвеска",
    "NON REPAIRABLE": "Не восстановить",
})

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.copart.com",
    "Referer": "https://www.copart.com/lotSearchResults",
    "User-Agent": USER_AGENT,
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

# Заголовки обычного перехода по ссылке — для прогрева сессии
PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": USER_AGENT,
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}

WARMUP_URL = "https://www.copart.com/lotSearchResults"


def damage_ru(value: Optional[str]) -> str:
    """«FRONT END» → «Перед». Незнакомые коды возвращаем как есть."""
    if not value:
        return ""
    return DAMAGE_RU.get(value.strip().upper(), value.strip().title())


def title_ru(value: Optional[str]) -> str:
    """«SALVAGE TITLE» → «⚠️ Salvage»."""
    if not value:
        return ""
    return TITLE_RU.get(value.strip().upper(), value.strip().title())


def keys_ru(value: Optional[str]) -> str:
    """«YES» → «🔑 Ключи есть»."""
    if not value:
        return ""
    return KEYS_RU.get(value.strip().upper(), "")


# ── Сборка запроса ────────────────────────────────────────────────────────────

def _make_values(brand: str) -> list[str]:
    """Варианты написания марки у Copart."""
    key = brand.strip().upper()
    return MAKE_MAP.get(key, [key])


KM_IN_MILE = 1.60934


def _is_native(f: SearchFilter) -> bool:
    """
    Отдельный фильтр Copart — значения уже в «родных» единицах аукциона:
    цена в долларах, пробег в милях. У общего фильтра — рубли и километры.
    """
    return getattr(f, "kind", "ru") == "copart"


def _km_to_miles(km: Optional[int]) -> Optional[int]:
    """Границы пробега из фильтра (км) → мили, как их хранит Copart."""
    if not km:
        return None
    return int(km / KM_IN_MILE)


def _solr_range(lo, hi) -> str:
    """Диапазон Solr: [2015 TO 2020], [2015 TO *], [* TO 2020]."""
    return f"[{lo if lo is not None else '*'} TO {hi if hi is not None else '*'}]"


def _date_range(f: SearchFilter) -> Optional[str]:
    """Диапазон даты аукциона в формате Solr для auction_date_utc."""
    if not f.auction_date_from and not f.auction_date_to:
        return None
    lo = f"{f.auction_date_from.isoformat()}T00:00:00Z" if f.auction_date_from else "NOW/DAY"
    hi = f"{f.auction_date_to.isoformat()}T23:59:59Z"   if f.auction_date_to   else "*"
    return f"auction_date_utc:[{lo} TO {hi}]"


def selected_brands(f: SearchFilter) -> list[str]:
    """Марки фильтра: список, если задан, иначе одиночное поле."""
    if getattr(f, "brands", None):
        return [b.strip().upper() for b in f.brands if b and b.strip()]
    return [f.brand.strip().upper()] if f.brand else []


def selected_models(f: SearchFilter) -> list[str]:
    if getattr(f, "models", None):
        return [m.strip().upper() for m in f.models if m and m.strip()]
    return [f.model.strip().upper()] if f.model else []


def _build_filter(f: SearchFilter, with_model: bool = True) -> dict:
    flt: dict[str, list[str]] = {}

    # Значения внутри группы объединяются по ИЛИ — так и работает
    # «CAMRY или ACCORD или SONATA» одним фильтром
    brands = selected_brands(f)
    if brands:
        variants = [v for b in brands for v in _make_values(b)]
        flt["MAKE"] = [f'lot_make_desc:"{v}"' for v in dict.fromkeys(variants)]

    models = selected_models(f)
    if with_model and models:
        flt["MODL"] = [f'lot_model_desc:"{m}"' for m in dict.fromkeys(models)]

    if f.year_from or f.year_to:
        flt["YEAR"] = [f"lot_year:{_solr_range(f.year_from, f.year_to)}"]

    # В отдельном фильтре Copart пробег вводится сразу в милях,
    # в общем — в километрах, поэтому там переводим
    if f.mileage_from or f.mileage_to:
        lo, hi = f.mileage_from, f.mileage_to
        if not _is_native(f):
            lo, hi = _km_to_miles(lo), _km_to_miles(hi)
        flt["ODM"] = [f"odometer_reading_received:{_solr_range(lo, hi)}"]

    date_expr = _date_range(f)
    if date_expr:
        flt["SDAT"] = [date_expr]

    # Тип документа: чистый / salvage / не восстановить
    groups = [TITLE_GROUPS[c][0] for c in (f.title_groups or []) if c in TITLE_GROUPS]
    if groups:
        flt["TITL"] = [f"title_group_code:{g}" for g in groups]

    # Площадки: храним двухбуквенные коды штатов, ищем по префиксу имени площадки
    states = [s.strip().upper() for s in (f.yards or []) if s.strip().upper() in YARD_STATES]
    if states:
        flt["LOC"] = [f"yard_name:{s}*" for s in states]

    # «На ходу» и «купить сразу» живут в одной группе FETI,
    # а внутри группы условия объединяются по ИЛИ — поэтому вместе их не ставим:
    # приоритет у «на ходу», Buy It Now отбираем уже у себя.
    feti = []
    if f.run_and_drive:
        feti.append("lot_condition_code:CERT-D")
    elif f.buy_now_only:
        feti.append("buy_it_now_code:B1")
    if feti:
        flt["FETI"] = feti

    # Исключение нежелательных повреждений — отрицанием
    excluded = [DAMAGE_CODES[c][0] for c in (f.damage_exclude or []) if c in DAMAGE_CODES]
    if excluded:
        flt["PRID"] = [f"-damage_type_code:({' OR '.join(excluded)})"]

    return flt


def _build_payload(f: SearchFilter, page: int, with_model: bool = True) -> dict:
    return {
        "query":                ["*"],
        "filter":               _build_filter(f, with_model=with_model),
        "sort":                 ["auction_date_type asc"],   # ближайшие торги первыми
        "page":                 page,
        "size":                 PAGE_SIZE,
        "start":                page * PAGE_SIZE,
        "watchListOnly":        False,
        "freeFormSearch":       False,
        "hideImages":           True,
        "defaultSort":          False,
        "specificRowProvided":  False,
        "displayName":          "",
        "searchName":           "",
        "backPage":             "search",
    }


# ── Разбор ответа ─────────────────────────────────────────────────────────────

def _to_int(value) -> Optional[int]:
    """Copart отдаёт -1.0 и 0.0 для «не указано»."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    return int(round(num))


def _to_datetime(epoch_ms) -> Optional[datetime.datetime]:
    try:
        return datetime.datetime.fromtimestamp(
            int(epoch_ms) / 1000, tz=datetime.timezone.utc
        )
    except (TypeError, ValueError, OSError):
        return None


def _image_url(raw: dict) -> Optional[str]:
    """Из превью-ссылки делаем ссылку нужного размера."""
    url = (raw.get("tims") or "").strip()
    if not url:
        return None
    return url.replace("_thb.jpg", f"{IMAGE_SIZE}.jpg")


def _specs(raw: dict) -> Optional[str]:
    """Строка характеристик: двигатель · привод · топливо · цвет."""
    parts = []

    engine = " ".join((raw.get("egn") or "").split())   # «2.4L  4» → «2.4L 4»
    if engine:
        parts.append(engine)

    drive = (raw.get("drv") or "").strip().upper()
    if drive:
        parts.append(DRIVE_RU.get(drive, drive.title()))

    fuel = (raw.get("ft") or "").strip().upper()
    if fuel:
        parts.append(FUEL_RU.get(fuel, fuel.title()))

    color = (raw.get("clr") or "").strip()
    if color:
        parts.append(color.title())

    return "  ·  ".join(parts) or None


def _parse_lot(raw: dict, filter_name: str) -> Optional[Listing]:
    lot_id = str(raw.get("lotNumberStr") or raw.get("ln") or "").strip()
    if not lot_id:
        return None

    title = (raw.get("ld") or "").strip()
    if not title:
        title = " ".join(filter(None, [
            str(raw.get("lcy") or ""), raw.get("mkn") or "", raw.get("lm") or "",
        ])).strip() or f"Лот {lot_id}"

    return Listing(
        source="copart",
        external_id=lot_id,
        url=LOT_URL.format(lot=lot_id),
        title=title,
        price=_to_int(raw.get("la")),          # оценочная стоимость в валюте лота
        year=_to_int(raw.get("lcy")),
        mileage=_to_int(raw.get("orr")),
        city=(raw.get("yn") or "").strip() or None,   # площадка, напр. «FL - MIAMI»
        transmission=(raw.get("tsmn") or "").strip().upper() or None,
        filter_name=filter_name,
        damage_description=(raw.get("dd") or "").strip() or None,
        auction_date=_to_datetime(raw.get("ad")),
        currency=(raw.get("cuc") or "USD").strip().upper(),
        image_url=_image_url(raw),
        title_group=(raw.get("tgd") or "").strip().upper() or None,
        has_keys=(raw.get("hk") or "").strip().upper() or None,
        run_and_drive=(raw.get("lcc") or "").strip().upper() == "CERT-D",
        buy_now_price=_to_int(raw.get("bnp")),
        repair_cost=_to_int(raw.get("rc")),
        odometer_brand=(raw.get("ord") or "").strip().upper() or None,
        vin=(raw.get("fv") or "").strip() or None,
        specs=_specs(raw),
    )


# ── Клиентская фильтрация ─────────────────────────────────────────────────────

def _price_bounds_usd(f: SearchFilter) -> tuple[Optional[int], Optional[int]]:
    """
    В отдельном фильтре Copart цена сразу в долларах — берём как есть.
    В общем фильтре она в рублях, переводим по курсу USD_RUB_RATE.
    """
    if _is_native(f):
        return f.price_from, f.price_to
    rate = cached_rate() or USD_RUB_RATE or 1
    lo = int(f.price_from / rate) if f.price_from else None
    hi = int(f.price_to   / rate) if f.price_to   else None
    return lo, hi


def _matches(listing: Listing, f: SearchFilter, model_client_side: bool) -> bool:
    lo, hi = _price_bounds_usd(f)
    # У лотов «купить сразу» оценочной стоимости часто нет — тогда судим по ней
    effective = listing.price or listing.buy_now_price
    if lo and (effective is None or effective < lo):
        return False
    if hi and (effective is None or effective > hi):
        return False

    # Модель не прошла точным facet-фильтром — ищем подстроку в заголовке.
    # Моделей может быть несколько, достаточно совпадения с любой.
    models = selected_models(f)
    if model_client_side and models:
        haystack = listing.title.upper().replace("-", " ")
        if not any(m.replace("-", " ") in haystack for m in models):
            return False

    # Buy It Now отсеиваем здесь, если группу FETI занял фильтр «на ходу»
    if f.buy_now_only and f.run_and_drive and not listing.buy_now_price:
        return False

    return True


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _looks_like_challenge(body: str) -> bool:
    """Признаки страницы защиты вместо JSON."""
    head = body[:2000].lower()
    return any(marker in head for marker in
               ("_incapsula_", "incapsula", "captcha", "<html", "distil",
                "access denied", "request unsuccessful"))


@asynccontextmanager
async def _copart_session():
    """
    Сессия с cookie-jar и прогревом.

    С домашнего IP endpoint отвечает и без этого, но с IP дата-центра
    (Render) Incapsula возвращает HTML-заглушку со статусом 200.
    Обычный заход на страницу поиска выдаёт нужные cookie, после чего
    тот же запрос проходит.
    """
    session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())
    try:
        try:
            async with session.get(WARMUP_URL, headers=PAGE_HEADERS,
                                   timeout=TIMEOUT) as resp:
                await resp.read()
                logger.debug(f"copart: прогрев {resp.status}, "
                             f"cookie: {len(session.cookie_jar)}")
        except Exception as e:
            logger.warning(f"copart: прогрев не удался: {e}")
        yield session
    finally:
        await session.close()


async def _post_via_scraperapi(payload: dict) -> Optional[str]:
    """Запасной путь: тот же POST, но чужими руками и с другого IP."""
    if not SCRAPER_API_KEY:
        return None
    url = (f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}"
           f"&url={quote(SEARCH_URL, safe='')}&keep_headers=true")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=HEADERS,
                                    timeout=aiohttp.ClientTimeout(total=90)) as resp:
                body = await resp.text()
                if resp.status != 200:
                    logger.warning(f"copart: ScraperAPI HTTP {resp.status}")
                    return None
                return body
    except Exception as e:
        logger.error(f"copart: ScraperAPI ошибка: {e}")
        return None


async def _post(session: aiohttp.ClientSession, payload: dict) -> Optional[dict]:
    body: Optional[str] = None

    try:
        async with session.post(SEARCH_URL, json=payload, headers=HEADERS,
                                timeout=TIMEOUT) as resp:
            body = await resp.text()
            status, ctype = resp.status, resp.headers.get("Content-Type", "")
    except Exception as e:
        logger.error(f"copart: сеть недоступна: {e}")
        return None

    if status != 200 or _looks_like_challenge(body):
        # Раньше здесь падал JSONDecodeError без единой подробности,
        # и по логам нельзя было понять, что вместо JSON приходит HTML
        logger.warning(
            f"copart: вместо JSON пришло HTTP {status}, "
            f"Content-Type={ctype!r}, {len(body)} байт. "
            f"Начало: {body[:160]!r}"
        )
        body = await _post_via_scraperapi(payload)
        if not body:
            logger.error("copart: ни прямой запрос, ни ScraperAPI не дали JSON")
            return None
        logger.info("copart: получено через ScraperAPI")

    try:
        data = json.loads(body)
    except Exception as e:
        logger.error(f"copart: ответ не разобрался как JSON: {e}. "
                     f"Начало: {body[:160]!r}")
        return None

    if data.get("returnCode") != 1:
        logger.warning(f"copart: returnCode={data.get('returnCode')} "
                       f"({data.get('returnCodeDesc')})")
        return None

    return (data.get("data") or {}).get("results") or {}


async def _fetch_pages(session, f: SearchFilter, with_model: bool) -> list[dict]:
    """Постранично забираем лоты, пока не кончатся или не упрёмся в MAX_PAGES."""
    raw_lots: list[dict] = []
    total = 0

    for page in range(MAX_PAGES):
        results = await _post(session, _build_payload(f, page, with_model))
        if not results:
            break

        content = results.get("content") or []
        raw_lots.extend(content)

        if page == 0:
            total = results.get("totalElements", 0)
            logger.info(f"copart: найдено по фильтру «{f.name}»: {total} лотов")

        if len(content) < PAGE_SIZE:
            break
        await asyncio.sleep(0.4)   # не долбим API

    # Не молчим об обрезке: иначе неполная выдача выглядит как «больше ничего нет»
    if total > len(raw_lots):
        logger.warning(
            f"copart: фильтр «{f.name}» — забрали {len(raw_lots)} из {total} лотов "
            f"(лимит {MAX_PAGES} стр. по {PAGE_SIZE}). "
            f"Сузь фильтр, если нужны все"
        )

    return raw_lots


# ── Справочники марок и моделей ───────────────────────────────────────────────
#
# Copart возвращает facetFields в каждом ответе поиска, причём группа MODL
# сужается под выбранную марку. Значит, список можно не угадывать и не хранить
# захардкоженным, а брать прямо у аукциона — с числом лотов по каждой позиции.
# Facet отдаёт максимум 400 значений, этого хватает с большим запасом.

_FACET_TTL = 6 * 3600           # справочник меняется медленно
_facet_cache: dict[str, tuple[float, list[tuple[str, int]]]] = {}


def _facet_values(results: dict, code: str) -> list[tuple[str, int]]:
    """Из facetFields достаём пары (значение, количество лотов)."""
    groups = [g for g in (results.get("facetFields") or [])
              if g.get("quickPickCode") == code]
    if not groups:
        return []
    out = []
    for fc in groups[0].get("facetCounts", []):
        query = fc.get("query", "")
        if ":" not in query:
            continue
        value = query.split(":", 1)[1].strip('"').strip()
        if value:
            out.append((value, fc.get("count", 0)))
    # Самые ходовые — наверх, по ним и выбирают
    return sorted(out, key=lambda x: -x[1])


async def _fetch_facet(cache_key: str, code: str, flt: dict) -> list[tuple[str, int]]:
    now = asyncio.get_event_loop().time()
    cached = _facet_cache.get(cache_key)
    if cached and now - cached[0] < _FACET_TTL:
        return cached[1]

    payload = {
        "query": ["*"], "filter": flt, "sort": ["auction_date_type asc"],
        "page": 0, "size": 1, "start": 0, "watchListOnly": False,
        "freeFormSearch": False, "hideImages": True, "defaultSort": False,
        "specificRowProvided": False, "displayName": "", "searchName": "",
        "backPage": "search",
    }
    async with _copart_session() as session:
        results = await _post(session, payload)
    if not results:
        return cached[1] if cached else []

    values = _facet_values(results, code)
    if values:
        _facet_cache[cache_key] = (now, values)
    return values


async def fetch_makes() -> list[tuple[str, int]]:
    """Марки, реально представленные на аукционе, с количеством лотов."""
    return await _fetch_facet("MAKE", "MAKE", {})


async def fetch_models(make: str) -> list[tuple[str, int]]:
    """Модели выбранной марки — список сужается фильтром MAKE."""
    make = (make or "").strip().upper()
    if not make:
        return []
    values = [f'lot_make_desc:"{v}"' for v in _make_values(make)]
    return await _fetch_facet(f"MODL:{make}", "MODL", {"MAKE": values})


# ── Парсер ────────────────────────────────────────────────────────────────────

class CopartParser(BaseParser):
    SOURCE = "copart"

    async def search(self, f: SearchFilter) -> list[Listing]:
        # Отдельный фильтр Copart работает всегда; общий — только если
        # аукцион явно выбран в источниках
        if not _is_native(f) and "copart" not in f.sources:
            return []

        brands = selected_brands(f)
        if brands and all(b in MAKES_NOT_ON_COPART for b in brands):
            logger.info(f"copart: марок {brands} на аукционе нет, "
                        f"фильтр «{f.name}» пропущен")
            return []

        async with _copart_session() as session:
            # Сначала пробуем точный facet по модели
            raw_lots = await _fetch_pages(session, f, with_model=True)
            model_client_side = False

            # Названия моделей у Copart свои (CRUZE, но «LAND CRUISER» → «LANDCRUISER»
            # и т.п.) — если точное совпадение ничего не дало, ищем по марке
            # и отсеиваем по заголовку уже у себя
            if selected_models(f) and not raw_lots:
                logger.info(f"copart: модели {selected_models(f)} нет в справочнике, "
                            f"фильтруем по заголовку")
                raw_lots = await _fetch_pages(session, f, with_model=False)
                model_client_side = True

        listings: list[Listing] = []
        seen_ids: set[str] = set()

        for raw in raw_lots:
            try:
                listing = _parse_lot(raw, f.name)
            except Exception as e:
                logger.warning(f"copart: ошибка разбора лота: {e}")
                continue

            if not listing or listing.external_id in seen_ids:
                continue
            if not _matches(listing, f, model_client_side):
                continue

            seen_ids.add(listing.external_id)
            listings.append(listing)

        logger.info(f"copart: фильтр «{f.name}» → {len(listings)} лотов")
        return listings

    async def preview(self, f: SearchFilter, sample_size: int = 3) -> dict:
        """
        Быстрая прикидка «что найдётся» — один запрос, без сохранения.
        Нужна, чтобы не ждать обхода, чтобы понять: фильтр пустой или слишком широкий.

        total   — сколько лотов у аукциона по запросу
        matched — сколько из первой сотни прошло наши фильтры цены и модели
        sample  — примеры лотов
        """
        brands = selected_brands(f)
        if brands and all(b in MAKES_NOT_ON_COPART for b in brands):
            names = ", ".join(brands)
            return {"total": 0, "matched": 0, "checked": 0, "sample": [],
                    "note": f"Марок {names} на аукционе нет — это рынок США"}

        async with _copart_session() as session:
            results = await _post(session, _build_payload(f, 0, with_model=True))
            model_client_side = False

            # Точного совпадения по модели нет — пробуем по марке с отбором у себя
            if selected_models(f) and results is not None and not (results.get("content") or []):
                results = await _post(session, _build_payload(f, 0, with_model=False))
                model_client_side = True

        if results is None:
            return {"total": 0, "matched": 0, "sample": [],
                    "note": "Аукцион не ответил, попробуй ещё раз"}

        total = results.get("totalElements", 0)
        checked = 0
        matched: list[Listing] = []

        for raw in (results.get("content") or []):
            checked += 1
            try:
                lot = _parse_lot(raw, f.name)
            except Exception:
                continue
            if lot and _matches(lot, f, model_client_side):
                matched.append(lot)

        return {
            "total":   total,
            "checked": checked,
            "matched": len(matched),
            "sample":  matched[:sample_size],
            "note":    "",
        }
