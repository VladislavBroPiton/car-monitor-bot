import asyncio
import logging
import re
import json
from typing import Optional
import aiohttp
from bs4 import BeautifulSoup

from parsers.base import BaseParser, Listing, SearchFilter

logger = logging.getLogger(__name__)

# Маппинг городов Авито (slug в URL)
# Формат для городов области: volgogradskaya_oblast_<город>
CITY_SLUG = {
    "Волгоград":            "volgograd",
    "Волжский":             "volgogradskaya_oblast_volzhskiy",
    "Камышин":              "volgogradskaya_oblast_kamyshin",
    "Михайловка":           "volgogradskaya_oblast_mihaylovka",
    "Урюпинск":             "volgogradskaya_oblast_urupinsk",
    "Фролово":              "volgogradskaya_oblast_frolovo",
    "Калач-на-Дону":        "volgogradskaya_oblast_kalach-na-donu",
    "Николаевск":           "volgogradskaya_oblast_nikolaevsk",
    "Москва":               "moskva",
    "Санкт-Петербург":      "sankt-peterburg",
    "Краснодар":            "krasnodar",
    "Екатеринбург":         "ekaterinburg",
    "Новосибирск":          "novosibirsk",
    "Казань":               "kazan",
    "Нижний Новгород":      "nizhniy_novgorod",
    "Челябинск":            "chelyabinsk",
    "Самара":               "samara",
    "Уфа":                  "ufa",
    "Ростов-на-Дону":       "rostov-na-donu",
    "Пермь":                "perm",
    "Воронеж":              "voronezh",
    "Саратов":              "saratov",
    "Тольятти":             "tolyatti",
    "Красноярск":           "krasnoyarsk",
    "Омск":                 "omsk",
    "Тюмень":               "tyumen",
    "Ставрополь":           "stavropol",
    "Иркутск":              "irkutsk",
    "Владивосток":          "vladivostok",
    "Барнаул":              "barnaul",
    "Ярославль":            "yaroslavl",
    "Белгород":             "belgorod",
    "Калининград":          "kaliningrad",
    "Тверь":                "tver",
}

# Маппинг марок для URL Авито
BRAND_SLUG = {
    "CHEVROLET":  "chevrolet",
    "SKODA":      "skoda",
    "TOYOTA":     "toyota",
    "BMW":        "bmw",
    "KIA":        "kia",
    "HYUNDAI":    "hyundai",
    "VOLKSWAGEN": "volkswagen",
    "MERCEDES":   "mercedes",
    "AUDI":       "audi",
    "NISSAN":     "nissan",
    "RENAULT":    "renault",
    "LADA":       "lada_vaz",
    "MAZDA":      "mazda",
    "MITSUBISHI": "mitsubishi",
    "FORD":       "ford",
    "HONDA":      "honda",
    "SUBARU":     "subaru",
    "LEXUS":      "lexus",
    "GEELY":      "geely",
    "CHERY":      "chery",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

import random

def _get_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _build_url(f: SearchFilter, city_slug: str, use_radius: bool = False) -> str:
    """
    Формат: https://www.avito.ru/volgograd/avtomobili?pmin=500000&radius=200&s=104
    Марка и модель фильтруются по заголовку после получения результатов.
    radius=200 покрывает всю область одним запросом.
    """
    base = f"https://www.avito.ru/{city_slug}/avtomobili"

    params: list[str] = []
    if f.price_from:
        params.append(f"pmin={f.price_from}")
    if f.price_to:
        params.append(f"pmax={f.price_to}")
    if f.year_from:
        params.append(f"year_from={f.year_from}")
    if f.year_to:
        params.append(f"year_to={f.year_to}")
    if f.mileage_to:
        params.append(f"km_do={f.mileage_to}")
    if f.mileage_from:
        params.append(f"km_ot={f.mileage_from}")

    if use_radius:
        params.append("radius=200")
        params.append("searchRadius=200")

    # Сортировка по дате — новые сначала
    params.append("s=104")

    if params:
        base += "?" + "&".join(params)

    return base


def _parse_price(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_cards(html: str, filter_name: str, city: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []

    # Авито использует data-marker для карточек
    cards = soup.select("[data-marker='item']")
    if not cards:
        # Запасной селектор
        cards = soup.select("div[class*='iva-item-root']")

    logger.info(f"avito: найдено карточек на странице: {len(cards)}")

    for card in cards:
        try:
            # Ссылка
            link = card.select_one("a[data-marker='item-title']") or card.select_one("a[itemprop='url']")
            if not link:
                continue

            href = link.get("href", "")
            if not href.startswith("http"):
                href = "https://www.avito.ru" + href

            # ID из URL
            match = re.search(r"_(\d+)$", href)
            external_id = match.group(1) if match else href.split("/")[-1]

            # Заголовок
            title = link.get_text(strip=True)

            # Цена
            price = None
            price_tag = card.select_one("[data-marker='item-price']") or card.select_one("meta[itemprop='price']")
            if price_tag:
                if price_tag.name == "meta":
                    price = int(price_tag.get("content", 0) or 0) or None
                else:
                    price = _parse_price(price_tag.get_text())

            # Год и пробег из описания
            year = None
            mileage = None
            desc = card.select_one("[data-marker='item-specific-params']") or card.select_one("p[class*='params']")
            if desc:
                text = desc.get_text(" ", strip=True)
                year_m = re.search(r"\b(19|20)\d{2}\b", text)
                if year_m:
                    year = int(year_m.group(0))
                km_m = re.search(r"([\d\s]+)\s*км", text)
                if km_m:
                    mileage = int(re.sub(r"\s", "", km_m.group(1)))

            listings.append(Listing(
                source="avito",
                external_id=external_id,
                url=href,
                title=title,
                price=price,
                year=year,
                mileage=mileage,
                city=city,
                filter_name=filter_name,
            ))

        except Exception as e:
            logger.warning(f"avito: ошибка парсинга карточки: {e}")
            continue

    return listings


def _matches_filter(title: str, brand: Optional[str], model: Optional[str]) -> bool:
    """Проверяем что заголовок содержит нужную марку и модель."""
    if not brand:
        return True
    title_lower = title.lower()
    brand_lower = brand.lower()
    # Маппинг для кириллических названий
    brand_aliases = {
        "lada": ["lada", "ваз", "лада"],
        "chevrolet": ["chevrolet", "шевроле"],
        "skoda": ["skoda", "шкода"],
        "volkswagen": ["volkswagen", "vw", "фольксваген"],
        "mercedes": ["mercedes", "мерседес"],
        "bmw": ["bmw", "бмв"],
    }
    aliases = brand_aliases.get(brand_lower, [brand_lower])
    if not any(a in title_lower for a in aliases):
        return False
    if model:
        model_lower = model.lower().replace("_", " ")
        if model_lower not in title_lower:
            return False
    return True


async def _fetch(url: str) -> Optional[str]:
    await asyncio.sleep(random.uniform(1.5, 3.5))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=_get_headers(),
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
            ) as resp:
                if resp.status == 429:
                    logger.warning(f"avito: 429 rate limit для {url}")
                    return None
                if resp.status != 200:
                    logger.warning(f"avito: статус {resp.status} для {url}")
                    return None
                return await resp.text()
    except Exception as e:
        logger.error(f"avito: ошибка запроса {url}: {e}")
        return None


class AvitoParser(BaseParser):
    SOURCE = "avito"

    async def search(self, f: SearchFilter) -> list[Listing]:
        if "avito" not in f.sources:
            return []

        # Определяем главный город для запроса
        # Авито поддерживает radius=200 — покрывает всю область одним запросом
        cities = list(f.cities or [])
        
        # Берём первый крупный город из списка как базовый для запроса
        base_city = "Волгоград"  # дефолт
        priority = ["Волгоград", "Волжский", "Камышин"]
        for p in priority:
            if p in cities:
                base_city = p
                break
        else:
            base_city = cities[0]

        city_slug = CITY_SLUG.get(base_city, "volgograd")
        url = _build_url(f, city_slug, use_radius=True)
        logger.info(f"avito: запрос для «{f.name}» (база: {base_city}, радиус 200км): {url}")

        html = await _fetch(url)
        if not html:
            logger.info(f"avito: фильтр «{f.name}» → 0 объявлений")
            return []

        listings = _parse_cards(html, f.name, base_city)

        # Фильтруем по марке/модели из заголовка
        if f.brand:
            before = len(listings)
            listings = [l for l in listings if _matches_filter(l.title, f.brand, f.model)]
            logger.info(f"avito: после фильтра марки/модели: {len(listings)} из {before}")

        logger.info(f"avito: фильтр «{f.name}» → {len(listings)} объявлений")
        return listings
