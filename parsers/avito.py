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
CITY_SLUG = {
    "Волгоград":            "volgograd",
    "Волжский":             "volzhskiy",
    "Камышин":              "kamyshin",
    "Михайловка":           "mihaylovka",
    "Урюпинск":             "urupinsk",
    "Фролово":              "frolovo",
    "Калач-на-Дону":        "kalach-na-donu",
    "Николаевск":           "nikolaevsk",
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


def _build_url(f: SearchFilter, city_slug: str) -> str:
    """
    Формат: https://www.avito.ru/volgograd/avtomobili/chevrolet_cruze
    Параметры: pmin, pmax, year_from, year_to, km_ot, km_do, transmission
    """
    base = f"https://www.avito.ru/{city_slug}/avtomobili"

    if f.brand:
        brand_slug = BRAND_SLUG.get(f.brand.upper(), f.brand.lower())
        model_slug = f.model.lower().replace(" ", "_") if f.model else ""
        if model_slug:
            base += f"/{brand_slug}_{model_slug}"
        else:
            base += f"/{brand_slug}"

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
    if f.transmission:
        tr_map = {"AUTO": "2", "MECHANICAL": "1"}
        tr = tr_map.get(f.transmission.upper())
        if tr:
            params.append(f"transmission={tr}")

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

        # Определяем города для поиска
        cities = list(f.cities or [])
        if not cities:
            cities = ["Волгоград"]  # дефолт если не указано

        all_listings: list[Listing] = []
        seen_ids: set[str] = set()

        for city in cities:
            city_slug = CITY_SLUG.get(city)
            if not city_slug:
                logger.warning(f"avito: нет slug для города «{city}»")
                continue

            url = _build_url(f, city_slug)
            logger.info(f"avito: запрос для «{f.name}» / {city}: {url}")

            html = await _fetch(url)
            if not html:
                continue

            listings = _parse_cards(html, f.name, city)

            for listing in listings:
                if listing.external_id not in seen_ids:
                    seen_ids.add(listing.external_id)
                    all_listings.append(listing)

        logger.info(f"avito: фильтр «{f.name}» → {len(all_listings)} объявлений")
        return all_listings
