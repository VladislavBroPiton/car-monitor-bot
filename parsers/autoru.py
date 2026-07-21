import asyncio
import logging
from typing import Optional
import aiohttp

from config import AUTORU_SESSION_ID, AUTORU_CSRF_TOKEN
from parsers.base import BaseParser, Listing, SearchFilter

# Маппинг городов → geo_id Auto.ru
# Источник: внутренний API auto.ru/geo/suggest/
CITY_GEO_ID: dict[str, int] = {
    "Москва":               213,
    "Санкт-Петербург":      2,
    "Екатеринбург":         54,
    "Новосибирск":          65,
    "Казань":               43,
    "Нижний Новгород":      47,
    "Челябинск":            56,
    "Самара":               51,
    "Уфа":                  172,
    "Ростов-на-Дону":       39,
    "Краснодар":            35,
    "Пермь":                50,
    "Воронеж":              193,
    "Волгоград":            38,
    "Волжский":             10950,
    "Камышин":              11115,
    "Михайловка":           11120,
    "Урюпинск":             11116,
    "Фролово":              11118,
    "Калач-на-Дону":        11113,
    "Николаевск":           11119,
    "Саратов":              194,
    "Тольятти":             38,
    "Красноярск":           62,
    "Иркутск":              63,
    "Омск":                 66,
    "Тюмень":               55,
    "Кемерово":             81,
    "Томск":                67,
    "Барнаул":              197,
    "Владивосток":          75,
    "Ижевск":               44,
    "Хабаровск":            76,
    "Ярославль":            16,
    "Оренбург":             48,
    "Рязань":               10,
    "Пенза":                49,
    "Тверь":                14,
    "Липецк":               9,
    "Белгород":             4,
    "Тула":                 15,
    "Калининград":          22,
    "Балашиха":             10740,
    "Подольск":             10747,
    "Химки":                10758,
    "Мытищи":               10741,
    "Люберцы":              10744,
    "Королёв":              10748,
    "Красногорск":          10742,
    "Одинцово":             10746,
    "Гатчина":              10838,
    "Выборг":               10842,
    "Таганрог":             11010,
    "Шахты":                11012,
    "Новочеркасск":         11011,
    "Батайск":              11013,
    "Набережные Челны":     10293,
    "Нижнекамск":           10294,
    "Альметьевск":          10296,
    "Стерлитамак":          10904,
    "Салават":              10906,
    "Тобольск":             10926,
    "Магнитогорск":         10754,
    "Миасс":                10755,
    "Сочи":                 239,
    "Новороссийск":         971,
    "Армавир":              976,
    "Анапа":                11252,
    "Дзержинск":            10786,
    "Арзамас":              10789,
    "Первоуральск":         10943,
    "Нижний Тагил":         236,
    "Ачинск":               10185,
    "Братск":               10191,
    "Ангарск":              10192,
    "Прокопьевск":          10100,
    "Новокузнецк":          237,
    "Ставрополь":           36,
    "Пятигорск":            11204,
    "Кисловодск":           11205,
}

logger = logging.getLogger(__name__)

SEARCH_URL = "https://auto.ru/-/ajax/desktop/listing/"

TRANSMISSION_MAP = {
    "AUTO": "AUTOMATIC",
    "MECHANICAL": "MECHANICAL",
    "ROBOT": "ROBOT",
    "VARIATOR": "VARIATOR",
}

BODY_TYPE_MAP = {
    "SEDAN": "SEDAN",
    "SUV": "ALLROAD_5_DOORS",
    "HATCHBACK": "HATCHBACK",
    "WAGON": "WAGON",
    "COUPE": "COUPE",
    "MINIVAN": "MINIVAN",
    "PICKUP": "PICKUP",
    "VAN": "VAN",
}


def _build_headers() -> dict:
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://auto.ru",
        "referer": "https://auto.ru/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "x-client-app": "autoru-frontend-desktop",
        "x-requested-with": "fetch",
    }
    if AUTORU_CSRF_TOKEN:
        headers["x-csrf-token"] = AUTORU_CSRF_TOKEN
    if AUTORU_SESSION_ID:
        # Передаём полные cookies для обхода капчи
        headers["cookie"] = AUTORU_SESSION_ID
    return headers


def _build_payload(f: SearchFilter, page: int = 1) -> dict:
    payload: dict = {
        "category": "cars",
        "section": "used",
        "sort": "fresh_relevance_1-desc",
        "page": page,
        "page_size": 30,
        "output_type": "list",
    }

    if f.brand:
        payload["catalog_filter"] = [{"mark": f.brand.upper()}]
        if f.model:
            payload["catalog_filter"][0]["model"] = f.model.upper()

    if f.year_from:
        payload["year_from"] = f.year_from
    if f.year_to:
        payload["year_to"] = f.year_to
    if f.price_from:
        payload["price_from"] = f.price_from
    if f.price_to:
        payload["price_to"] = f.price_to
    if f.mileage_from:
        payload["km_age_from"] = f.mileage_from
    if f.mileage_to:
        payload["km_age_to"] = f.mileage_to
    if f.transmission and f.transmission.upper() in TRANSMISSION_MAP:
        payload["transmission"] = [TRANSMISSION_MAP[f.transmission.upper()]]
    if f.body_type and f.body_type.upper() in BODY_TYPE_MAP:
        payload["body_type_group"] = [BODY_TYPE_MAP[f.body_type.upper()]]

    # Фильтрация по городам через geo_id
    if f.cities:
        geo_ids = [CITY_GEO_ID[c] for c in f.cities if c in CITY_GEO_ID]
        if geo_ids:
            # Auto.ru принимает geo_id как список в поле geo_id
            payload["geo_id"] = geo_ids
            # Радиус поиска 0 = только выбранный город (без окрестностей)
            payload["geo_radius"] = 0
            logger.info(f"autoru: geo_id={geo_ids} для городов {f.cities}")
        else:
            logger.warning(f"autoru: не найден geo_id для городов {f.cities}")

    return payload


def _parse_listing(item: dict, filter_name: str) -> Optional[Listing]:
    try:
        offer_id = item.get("id") or item.get("saleId")
        if not offer_id:
            return None

        vehicle = item.get("vehicle_info", {})
        mark_info = vehicle.get("mark_info", {})
        model_info = vehicle.get("model_info", {})
        tech = vehicle.get("tech_param", {})
        docs = item.get("documents", {})
        price_info = item.get("price_info", {})
        seller = item.get("seller", {})
        location = seller.get("location", {})

        mark = mark_info.get("name", "")
        model = model_info.get("name", "")
        year = docs.get("year")
        title = f"{mark} {model} {year or ''}".strip()

        price_rub = price_info.get("price")
        mileage = item.get("state", {}).get("mileage")
        city = (
            location.get("region_info", {}).get("name")
            or location.get("name", "")
        )

        transmission_raw = tech.get("transmission", "")
        body_raw = vehicle.get("body_type", "")
        url = f"https://auto.ru/cars/used/sale/{offer_id}/"

        return Listing(
            source="autoru",
            external_id=str(offer_id),
            url=url,
            title=title,
            price=int(price_rub) if price_rub else None,
            year=year,
            mileage=mileage,
            city=city,
            transmission=transmission_raw or None,
            body_type=body_raw or None,
            filter_name=filter_name,
        )
    except Exception as e:
        logger.warning(f"autoru: ошибка парсинга объявления: {e}")
        return None


class AutoRuParser(BaseParser):
    SOURCE = "autoru"

    async def search(self, f: SearchFilter) -> list[Listing]:
        if "autoru" not in f.sources:
            return []

        listings: list[Listing] = []
        headers = _build_headers()
        payload = _build_payload(f, page=1)

        try:
            logger.info(f"autoru: payload geo_id={payload.get('geo_id')}, geo_radius={payload.get('geo_radius')}")
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    SEARCH_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        logger.warning(
                            f"autoru: статус {resp.status} для фильтра «{f.name}»"
                        )
                        return []
                    data = await resp.json(content_type=None)

            for item in data.get("offers", []):
                listing = _parse_listing(item, f.name)
                if listing:
                    listings.append(listing)

            logger.info(f"autoru: фильтр «{f.name}» → {len(listings)} объявлений")

        except asyncio.TimeoutError:
            logger.error(f"autoru: timeout для фильтра «{f.name}»")
        except Exception as e:
            logger.error(f"autoru: ошибка для фильтра «{f.name}»: {e}")

        return listings
