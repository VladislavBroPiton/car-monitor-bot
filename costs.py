# costs.py — прикидка итоговой стоимости лота Copart «под ключ»
#
# ВАЖНО про точность. Это оценка, а не расчёт по договору:
#   • Copart не отдаёт цену продажи, поэтому за базу берётся оценочная
#     стоимость лота (`la`) либо цена «купить сразу» (`bnp`).
#     Реальная цена на торгах может отличаться в обе стороны.
#   • Аукционный сбор у Copart прогрессивный и зависит от способа оплаты
#     и типа членства. Здесь он взят единым процентом.
#   • Доставка зависит от площадки и порта, пошлина — от возраста
#     и объёма двигателя.
# Все тарифы вынесены в переменные окружения (см. .env.example),
# чтобы подставить свои значения без правки кода.

from dataclasses import dataclass
from typing import Optional

from config import (
    COPART_AUCTION_FEE_PCT,
    COPART_BROKER_FEE,
    COPART_INLAND_USD,
    COPART_OCEAN_USD,
    COPART_CUSTOMS_PCT,
    USD_RUB_RATE,
)


@dataclass
class CostBreakdown:
    lot_price:   int    # цена лота (оценка либо «купить сразу»)
    auction_fee: int    # аукционный сбор
    broker_fee:  int    # брокер и оформление
    inland:      int    # доставка от площадки до порта США
    ocean:       int    # морской фрахт
    customs:     int    # пошлина и таможенные сборы
    total_usd:   int
    total_rub:   int

    def as_dict(self) -> dict:
        return {
            "lot_price":   self.lot_price,
            "auction_fee": self.auction_fee,
            "broker_fee":  self.broker_fee,
            "inland":      self.inland,
            "ocean":       self.ocean,
            "customs":     self.customs,
            "total_usd":   self.total_usd,
            "total_rub":   self.total_rub,
        }


def estimate(price_usd: Optional[int], rate: Optional[float] = None) -> Optional[CostBreakdown]:
    """
    Прикинуть стоимость «под ключ» по цене лота в долларах.
    Возвращает None, если цена неизвестна — врать нулями не будем.
    """
    if not price_usd or price_usd <= 0:
        return None

    rate = rate or USD_RUB_RATE or 1

    auction_fee = round(price_usd * COPART_AUCTION_FEE_PCT / 100)
    broker_fee  = round(COPART_BROKER_FEE)
    inland      = round(COPART_INLAND_USD)
    ocean       = round(COPART_OCEAN_USD)

    # Пошлина считается от стоимости авто вместе с доставкой
    customs_base = price_usd + auction_fee + inland + ocean
    customs = round(customs_base * COPART_CUSTOMS_PCT / 100)

    total_usd = price_usd + auction_fee + broker_fee + inland + ocean + customs

    return CostBreakdown(
        lot_price=price_usd,
        auction_fee=auction_fee,
        broker_fee=broker_fee,
        inland=inland,
        ocean=ocean,
        customs=customs,
        total_usd=total_usd,
        total_rub=round(total_usd * rate),
    )


def _usd(v: int) -> str:
    return "$" + f"{v:,}".replace(",", " ")


def _rub(v: int) -> str:
    return f"{v:,}".replace(",", " ") + " ₽"


def format_breakdown(b: CostBreakdown) -> str:
    """Расшифровка расчёта для сообщения в Telegram (HTML)."""
    return "\n".join([
        f"🧮 <b>Ориентировочно «под ключ»</b>",
        f"<code>{'─' * 26}</code>",
        f"Лот                {_usd(b.lot_price)}",
        f"Сбор аукциона      {_usd(b.auction_fee)}",
        f"Брокер             {_usd(b.broker_fee)}",
        f"До порта США       {_usd(b.inland)}",
        f"Морской фрахт      {_usd(b.ocean)}",
        f"Пошлина и сборы    {_usd(b.customs)}",
        f"<code>{'─' * 26}</code>",
        f"<b>Итого  {_usd(b.total_usd)}  ≈  {_rub(b.total_rub)}</b>",
        "",
        "<i>Оценка по вашим тарифам из настроек. Цена лота — оценочная, "
        "итог торгов может отличаться.</i>",
    ])
