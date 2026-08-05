# rates.py — курс доллара с сайта ЦБ РФ
#
# Раньше курс задавался переменной USD_RUB_RATE и тихо устаревал: при движении
# курса рублёвые границы цены в фильтрах начинали означать не то, что задумано.
# Теперь он подтягивается автоматически, а значение из окружения остаётся
# запасным — на случай, если ЦБ недоступен.

import asyncio
import logging
import xml.etree.ElementTree as ET
from typing import Optional

import aiohttp

from config import USD_RUB_RATE

logger = logging.getLogger(__name__)

CBR_XML  = "https://www.cbr.ru/scripts/XML_daily.asp"      # официальный источник
CBR_JSON = "https://www.cbr-xml-daily.ru/daily_json.js"    # зеркало, если XML лёг

TTL = 6 * 3600          # курс ЦБ меняется раз в сутки, чаще ходить незачем
TIMEOUT = aiohttp.ClientTimeout(total=15)

_cache: dict = {"rate": None, "at": 0.0, "source": "env"}


def _parse_xml(text: str) -> Optional[float]:
    """ЦБ отдаёт «80,9293» с запятой и отдельным номиналом."""
    root = ET.fromstring(text)
    for valute in root.findall("Valute"):
        if valute.findtext("CharCode") != "USD":
            continue
        value = (valute.findtext("Value") or "").replace(",", ".")
        nominal = (valute.findtext("Nominal") or "1").replace(",", ".")
        try:
            return float(value) / float(nominal)
        except (ValueError, ZeroDivisionError):
            return None
    return None


async def _fetch() -> tuple[Optional[float], str]:
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        try:
            async with session.get(CBR_XML) as resp:
                if resp.status == 200:
                    rate = _parse_xml(await resp.text(encoding="windows-1251"))
                    if rate:
                        return rate, "ЦБ РФ"
        except Exception as e:
            logger.warning(f"курс: XML ЦБ недоступен ({e}), пробую зеркало")

        try:
            async with session.get(CBR_JSON) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    rate = float(data["Valute"]["USD"]["Value"])
                    if rate:
                        return rate, "зеркало ЦБ"
        except Exception as e:
            logger.warning(f"курс: зеркало недоступно ({e})")

    return None, ""


async def usd_rub(force: bool = False) -> float:
    """Текущий курс доллара. При любой неудаче — значение из окружения."""
    now = asyncio.get_event_loop().time()
    if not force and _cache["rate"] and now - _cache["at"] < TTL:
        return _cache["rate"]

    rate, source = await _fetch()
    if rate:
        _cache.update(rate=rate, at=now, source=source)
        logger.info(f"курс: 1 USD = {rate:.2f} ₽ ({source})")
        return rate

    if _cache["rate"]:
        return _cache["rate"]      # держим последний удачный, он лучше дефолта

    logger.warning(f"курс: беру запасной из настроек — {USD_RUB_RATE}")
    return USD_RUB_RATE


def cached_rate() -> float:
    """Курс без похода в сеть — для синхронного кода."""
    return _cache["rate"] or USD_RUB_RATE


def rate_source() -> str:
    return _cache["source"] if _cache["rate"] else "настройки"
