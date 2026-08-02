import datetime
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Listing:
    source: str
    external_id: str
    url: str
    title: str
    price: Optional[int] = None
    year: Optional[int] = None
    mileage: Optional[int] = None
    city: Optional[str] = None
    transmission: Optional[str] = None
    body_type: Optional[str] = None
    filter_name: Optional[str] = None
    # ── Поля аукционов (Copart) ───────────────────────────────────────────────
    damage_description: Optional[str] = None            # «FRONT END», «REAR END», …
    auction_date: Optional[datetime.datetime] = None    # дата и время торгов (UTC)
    currency: Optional[str] = None                      # USD / CAD — валюта лота


@dataclass
class SearchFilter:
    id: int
    user_id: int
    name: str
    brand: Optional[str] = None
    model: Optional[str] = None
    year_from: Optional[int] = None
    year_to: Optional[int] = None
    price_from: Optional[int] = None
    price_to: Optional[int] = None
    mileage_from: Optional[int] = None
    mileage_to: Optional[int] = None
    city: Optional[str] = None   # устарело
    cities: list[str] = field(default_factory=list)  # список городов
    transmission: Optional[str] = None
    body_type: Optional[str] = None
    sources: list[str] = field(default_factory=lambda: ["autoru", "drom"])
    # Диапазон даты аукциона — используется только источником copart
    auction_date_from: Optional[datetime.date] = None
    auction_date_to: Optional[datetime.date] = None

    @classmethod
    def from_record(cls, record) -> "SearchFilter":
        # Колонки auction_date_* появились позже — на неразмигрированной БД их нет
        def opt(key):
            try:
                return record[key]
            except (KeyError, IndexError):
                return None

        return cls(
            id=record["id"],
            user_id=record["user_id"],
            name=record["name"],
            brand=record["brand"],
            model=record["model"],
            year_from=record["year_from"],
            year_to=record["year_to"],
            price_from=record["price_from"],
            price_to=record["price_to"],
            mileage_from=record["mileage_from"],
            mileage_to=record["mileage_to"],
            city=record["city"],
            cities=list(record["cities"] or []),
            transmission=record["transmission"],
            body_type=record["body_type"],
            sources=list(record["sources"] or ["autoru", "drom"]),
            auction_date_from=opt("auction_date_from"),
            auction_date_to=opt("auction_date_to"),
        )


class BaseParser(ABC):
    SOURCE: str = ""

    @abstractmethod
    async def search(self, f: SearchFilter) -> list[Listing]:
        ...
