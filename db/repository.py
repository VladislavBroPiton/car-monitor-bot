import asyncpg
from typing import Optional
from config import DATABASE_URL

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Filters ───────────────────────────────────────────────────────────────────

async def get_active_filters(user_id: int) -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(
        "SELECT * FROM filters WHERE user_id = $1 ORDER BY created_at DESC",
        user_id,
    )


async def get_filter_by_id(filter_id: int, user_id: int) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM filters WHERE id = $1 AND user_id = $2",
        filter_id, user_id,
    )


async def get_all_active_filters() -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM filters WHERE is_active = TRUE")


async def create_filter(
    user_id: int,
    name: str,
    brand: Optional[str] = None,
    model: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    price_from: Optional[int] = None,
    price_to: Optional[int] = None,
    mileage_from: Optional[int] = None,
    mileage_to: Optional[int] = None,
    cities: Optional[list[str]] = None,
    transmission: Optional[str] = None,
    body_type: Optional[str] = None,
    sources: list[str] = None,
) -> asyncpg.Record:
    if sources is None:
        sources = ["autoru", "drom", "avito"]
    pool = await get_pool()
    return await pool.fetchrow(
        """
        INSERT INTO filters
            (user_id, name, brand, model, year_from, year_to,
             price_from, price_to, mileage_from, mileage_to,
             cities, transmission, body_type, sources)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
        RETURNING *
        """,
        user_id, name, brand, model, year_from, year_to,
        price_from, price_to, mileage_from, mileage_to,
        cities, transmission, body_type, sources,
    )


async def update_filter_field(
    filter_id: int,
    user_id: int,
    field: str,
    value,
) -> bool:
    """Обновить одно поле фильтра."""
    allowed = {
        "name", "brand", "model", "year_from", "year_to",
        "price_from", "price_to", "mileage_from", "mileage_to",
        "cities", "transmission", "body_type", "sources",
    }
    if field not in allowed:
        return False
    pool = await get_pool()
    result = await pool.execute(
        f"UPDATE filters SET {field} = $1 WHERE id = $2 AND user_id = $3",
        value, filter_id, user_id,
    )
    return result == "UPDATE 1"


async def delete_filter(filter_id: int, user_id: int) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "DELETE FROM filters WHERE id = $1 AND user_id = $2",
        filter_id, user_id,
    )
    return result == "DELETE 1"


async def toggle_filter(filter_id: int, user_id: int, active: bool) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE filters SET is_active = $1 WHERE id = $2 AND user_id = $3",
        active, filter_id, user_id,
    )
    return result == "UPDATE 1"


# ── Seen listings ─────────────────────────────────────────────────────────────

async def mark_seen(
    source: str,
    external_id: str,
    url: str,
    title: Optional[str] = None,
    price: Optional[int] = None,
    year: Optional[int] = None,
    mileage: Optional[int] = None,
    city: Optional[str] = None,
) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        """
        INSERT INTO seen_listings
            (source, external_id, url, title, price, year, mileage, city)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        ON CONFLICT (source, external_id) DO NOTHING
        """,
        source, external_id, url, title, price, year, mileage, city,
    )
    return result == "INSERT 0 1"


async def cleanup_old_listings(days: int = 30):
    pool = await get_pool()
    return await pool.execute(
        f"DELETE FROM seen_listings WHERE created_at < NOW() - INTERVAL '{days} days'"
    )
