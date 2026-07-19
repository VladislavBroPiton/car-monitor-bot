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
    city: Optional[str] = None
    transmission: Optional[str] = None
    body_type: Optional[str] = None
    sources: list[str] = field(default_factory=lambda: ["autoru", "drom"])

    @classmethod
    def from_record(cls, record) -> "SearchFilter":
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
            transmission=record["transmission"],
            body_type=record["body_type"],
            sources=list(record["sources"] or ["autoru", "drom"]),
        )


class BaseParser(ABC):
    SOURCE: str = ""

    @abstractmethod
    async def search(self, f: SearchFilter) -> list[Listing]:
        ...
