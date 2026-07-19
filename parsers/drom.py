import asyncio
import logging
import re
from typing import Optional
import aiohttp
from bs4 import BeautifulSoup

from parsers.base import BaseParser, Listing, SearchFilter

logger = logging.getLogger(__name__)

BASE_URL = "https://auto.drom.ru"

TRANSMISSION_MAP = {
    "AUTO": "2",
    "MECHANICAL": "1",
    "ROBOT": "6",
    "VARIATOR": "3",
}

BODY_TYPE_MAP = {
    "SEDAN": "1",
    "SUV": "7",
    "HATCHBACK": "2",
    "WAGON": "3",
    "COUPE": "4",
    "MINIVAN": "5",
    "PICKUP": "6",
    "VAN": "8",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _build_url(f: SearchFilter) -> str:
    parts = [BASE_URL]
    if f.brand:
        parts.append(f.brand.lower())
        if f.model:
            parts.append(f.model.lower())
    url = "/".join(parts) + "/"

    params: list[str] = []
    if f.year_from:
        params.append(f"minyear={f.year_from}")
    if f.year_to:
        params.append(f"maxyear={f.year_to}")
    if f.price_from:
        params.append(f"minprice={f.price_from}")
    if f.price_to:
        params.append(f"maxprice={f.price_to}")
    if f.mileage_from:
        params.append(f"minprobeg={f.mileage_from}")
    if f.mileage_to:
        params.append(f"maxprobeg={f.mileage_to}")
    if f.transmission and f.transmission.upper() in TRANSMISSION_MAP:
        params.append(f"transmission={TRANSMISSION_MAP[f.transmission.upper()]}")
    if f.body_type and f.body_type.upper() in BODY_TYPE_MAP:
        params.append(f"body={BODY_TYPE_MAP[f.body_type.upper()]}")
    if f.city:
        params.append(f"city={f.city}")
    params.append("order=date_add")

    if params:
        url += "?" + "&".join(params)
    return url


def _extract_id(url: str) -> Optional[str]:
    match = re.search(r"/(\d{6,})/", url)
    return match.group(1) if match else None


def _parse_price(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_mileage(text: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def _parse_year(text: str) -> Optional[int]:
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return int(match.group(0)) if match else None


def _parse_card(card, filter_name: str) -> Optional[Listing]:
    try:
        link_tag = (
            card.select_one("a[data-ftid='bulls-list_bull']")
            or card.select_one("a[href*='auto.drom.ru']")
            or card.select_one("h3 a")
        )
        if not link_tag:
            return None

        url = link_tag.get("href", "")
        if not url.startswith("http"):
            url = "https:" + url

        external_id = _extract_id(url)
        if not external_id:
            return None

        title_tag = card.select_one("[data-ftid='bulls-list_bull-title']")
        title = (
            title_tag.get_text(strip=True)
            if title_tag
            else link_tag.get_text(strip=True)
        )

        price = None
        price_tag = card.select_one("[data-ftid='bulls-list_bull-price']")
        if price_tag:
            price = _parse_price(price_tag.get_text())

        year = None
        mileage = None
        desc_tag = card.select_one("[data-ftid='bulls-list_bull-description']")
        if desc_tag:
            desc_text = desc_tag.get_text(" ", strip=True)
            year = _parse_year(desc_text)
            if "км" in desc_text:
                mileage = _parse_mileage(desc_text)

        if not year:
            year = _parse_year(title)

        city = None
        city_tag = card.select_one("[data-ftid='bulls-list_bull-location']")
        if city_tag:
            city = city_tag.get_text(strip=True)

        return Listing(
            source="drom",
            external_id=external_id,
            url=url,
            title=title,
            price=price,
            year=year,
            mileage=mileage,
            city=city,
            filter_name=filter_name,
        )
    except Exception as e:
        logger.warning(f"drom: ошибка парсинга карточки: {e}")
        return None


async def _fetch_page(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    try:
        async with session.get(
            url,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=25),
            allow_redirects=True,
        ) as resp:
            if resp.status != 200:
                logger.warning(f"drom: статус {resp.status} для {url}")
                return None
            return await resp.text()
    except asyncio.TimeoutError:
        logger.error(f"drom: timeout для {url}")
        return None
    except Exception as e:
        logger.error(f"drom: ошибка запроса {url}: {e}")
        return None


def _parse_html(html: str, filter_name: str) -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")

    cards = soup.select("[data-ftid='bulls-list_bull']")
    if not cards:
        cards = soup.select("div.bull-list-item-v2")
    if not cards:
        logger.warning("drom: не найдены карточки (возможно, изменилась разметка)")
        return []

    listings = []
    for card in cards:
        listing = _parse_card(card, filter_name)
        if listing:
            listings.append(listing)
    return listings


class DromParser(BaseParser):
    SOURCE = "drom"

    async def search(self, f: SearchFilter) -> list[Listing]:
        if "drom" not in f.sources:
            return []

        url = _build_url(f)
        logger.info(f"drom: запрос для фильтра «{f.name}»: {url}")

        async with aiohttp.ClientSession() as session:
            html = await _fetch_page(session, url)

        if not html:
            return []

        listings = _parse_html(html, f.name)
        logger.info(f"drom: фильтр «{f.name}» → {len(listings)} объявлений")
        return listings
