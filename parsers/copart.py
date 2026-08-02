# parsers/copart.py — парсер аукциона Copart через его публичный JSON API
#
# Endpoint найден через DevTools (Network → Fetch/XHR) на странице поиска
# https://www.copart.com/lotSearchResults:
#
#   POST https://www.copart.com/public/lots/search-results
#   Content-Type: application/json
#
# Это тот же самый запрос, который делает сам сайт при поиске авто.
# ScraperAPI не нужен — endpoint отвечает напрямую, без авторизации и капчи.
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
#   ODM   odometer_reading_received:[0 TO 150000]
#   SDAT  auction_date_utc:[NOW TO NOW+7DAY]
#   LOC   yard_name:"FL - MIAMI"
#   TMTP  transmission_type:"AUTOMATIC"
#   BODY  body_style:"4DR SPOR"
#
# Поля лота (сокращения Copart):
#   ln / lotNumberStr — номер лота        ld  — заголовок «2016 CHEVROLET CRUZE LT»
#   lcy — год                             mkn — марка          lm — модель
#   dd  — описание повреждения            td  — тип документа  tgd — группа документа
#   orr — пробег (одометр)                ord — ACTUAL / NOT ACTUAL
#   la  — оценочная стоимость             cuc — валюта (USD/CAD)
#   ad  — дата аукциона (epoch ms)        at — время, tz — таймзона
#   yn  — площадка «FL - MIAMI»           locCity / locState / locCountry
#   ldu — slug для ссылки                 tims — превью-картинка
#   tsmn — КПП                            ft — топливо, drv — привод, clr — цвет

import asyncio
import datetime
import logging
from typing import Optional

import aiohttp

from config import USD_RUB_RATE
from parsers.base import BaseParser, Listing, SearchFilter

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.copart.com/public/lots/search-results"
LOT_URL    = "https://www.copart.com/ru/lot/{lot}"

PAGE_SIZE = 100     # максимум, который отдаёт endpoint за один запрос
MAX_PAGES = 3       # 300 лотов на фильтр — дальше уже неактуальные торги
TIMEOUT   = aiohttp.ClientTimeout(total=45)

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

# Русские названия повреждений для карточек и уведомлений
DAMAGE_RU: dict[str, str] = {
    "FRONT END":            "Перед",
    "REAR END":             "Зад",
    "SIDE":                 "Бок",
    "ALL OVER":             "Круговые",
    "MINOR DENT/SCRATCHES": "Мелкие вмятины/царапины",
    "NORMAL WEAR":          "Обычный износ",
    "UNDERCARRIAGE":        "Днище",
    "ROLLOVER":             "Переворот",
    "TOP/ROOF":             "Крыша",
    "WATER/FLOOD":          "Вода/потоп",
    "BURN":                 "Пожар",
    "BURN - ENGINE":        "Пожар (двигатель)",
    "BURN - INTERIOR":      "Пожар (салон)",
    "VANDALISM":            "Вандализм",
    "HAIL":                 "Град",
    "MECHANICAL":           "Механическая неисправность",
    "ELECTRICAL":           "Электрика",
    "SUSPENSION":           "Подвеска",
    "STRIPPED":             "Разукомплектован",
    "PARTIAL REPAIR":       "Частично отремонтирован",
    "REPLACED VIN":         "Заменён VIN",
    "DAMAGE HISTORY":       "История повреждений",
    "BIOHAZARD/CHEMICAL":   "Биоопасность/химия",
    "FRAME DAMAGE":         "Повреждение рамы",
}

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Origin": "https://www.copart.com",
    "Referer": "https://www.copart.com/lotSearchResults",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def damage_ru(value: Optional[str]) -> str:
    """«FRONT END» → «Перед». Незнакомые коды возвращаем как есть."""
    if not value:
        return ""
    return DAMAGE_RU.get(value.strip().upper(), value.strip().title())


# ── Сборка запроса ────────────────────────────────────────────────────────────

def _make_values(brand: str) -> list[str]:
    """Варианты написания марки у Copart."""
    key = brand.strip().upper()
    return MAKE_MAP.get(key, [key])


KM_IN_MILE = 1.60934


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


def _build_filter(f: SearchFilter, with_model: bool = True) -> dict:
    flt: dict[str, list[str]] = {}

    if f.brand:
        flt["MAKE"] = [f'lot_make_desc:"{v}"' for v in _make_values(f.brand)]

    if with_model and f.model:
        flt["MODL"] = [f'lot_model_desc:"{f.model.strip().upper()}"']

    if f.year_from or f.year_to:
        flt["YEAR"] = [f"lot_year:{_solr_range(f.year_from, f.year_to)}"]

    # Пробег в фильтре задаётся в километрах, одометр Copart — в милях
    if f.mileage_from or f.mileage_to:
        flt["ODM"] = [
            f"odometer_reading_received:"
            f"{_solr_range(_km_to_miles(f.mileage_from), _km_to_miles(f.mileage_to))}"
        ]

    date_expr = _date_range(f)
    if date_expr:
        flt["SDAT"] = [date_expr]

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
    )


# ── Клиентская фильтрация ─────────────────────────────────────────────────────

def _price_bounds_usd(f: SearchFilter) -> tuple[Optional[int], Optional[int]]:
    """
    Цена в фильтрах задаётся в рублях (как для Auto.ru/Авито),
    а лоты Copart — в долларах. Переводим границы по курсу USD_RUB_RATE.
    """
    rate = USD_RUB_RATE or 1
    lo = int(f.price_from / rate) if f.price_from else None
    hi = int(f.price_to   / rate) if f.price_to   else None
    return lo, hi


def _matches(listing: Listing, f: SearchFilter, model_client_side: bool) -> bool:
    lo, hi = _price_bounds_usd(f)
    if lo and (listing.price is None or listing.price < lo):
        return False
    if hi and (listing.price is None or listing.price > hi):
        return False

    # Модель не прошла точным facet-фильтром — ищем подстроку в заголовке
    if model_client_side and f.model:
        needle = f.model.strip().upper().replace("-", " ")
        haystack = listing.title.upper().replace("-", " ")
        if needle not in haystack:
            return False

    return True


# ── HTTP ──────────────────────────────────────────────────────────────────────

async def _post(session: aiohttp.ClientSession, payload: dict) -> Optional[dict]:
    try:
        async with session.post(SEARCH_URL, json=payload, headers=HEADERS,
                                timeout=TIMEOUT) as resp:
            if resp.status != 200:
                logger.warning(f"copart: HTTP {resp.status} от search-results")
                return None
            data = await resp.json(content_type=None)
    except Exception as e:
        logger.error(f"copart: ошибка запроса: {e}")
        return None

    if data.get("returnCode") != 1:
        logger.warning(f"copart: returnCode={data.get('returnCode')} "
                       f"({data.get('returnCodeDesc')})")
        return None

    return (data.get("data") or {}).get("results") or {}


async def _fetch_pages(session, f: SearchFilter, with_model: bool) -> list[dict]:
    """Постранично забираем лоты, пока не кончатся или не упрёмся в MAX_PAGES."""
    raw_lots: list[dict] = []

    for page in range(MAX_PAGES):
        results = await _post(session, _build_payload(f, page, with_model))
        if not results:
            break

        content = results.get("content") or []
        raw_lots.extend(content)

        if page == 0:
            logger.info(f"copart: найдено по фильтру «{f.name}»: "
                        f"{results.get('totalElements', 0)} лотов")

        if len(content) < PAGE_SIZE:
            break
        await asyncio.sleep(0.4)   # не долбим API

    return raw_lots


# ── Парсер ────────────────────────────────────────────────────────────────────

class CopartParser(BaseParser):
    SOURCE = "copart"

    async def search(self, f: SearchFilter) -> list[Listing]:
        if "copart" not in f.sources:
            return []

        if f.brand and f.brand.strip().upper() in MAKES_NOT_ON_COPART:
            logger.info(f"copart: марка «{f.brand}» на аукционе не представлена, "
                        f"фильтр «{f.name}» пропущен")
            return []

        async with aiohttp.ClientSession() as session:
            # Сначала пробуем точный facet по модели
            raw_lots = await _fetch_pages(session, f, with_model=True)
            model_client_side = False

            # Названия моделей у Copart свои (CRUZE, но «LAND CRUISER» → «LANDCRUISER»
            # и т.п.) — если точное совпадение ничего не дало, ищем по марке
            # и отсеиваем по заголовку уже у себя
            if f.model and not raw_lots:
                logger.info(f"copart: модель «{f.model}» не найдена в справочнике, "
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
