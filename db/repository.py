import datetime
import logging
import asyncpg
from typing import Optional
from config import DATABASE_URL

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

# Колонки, добавленные после первого релиза. Выполняем при старте, чтобы
# не заходить руками в SQL-редактор Neon после каждого деплоя.
_MIGRATIONS = (
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS damage_description TEXT",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS auction_date TIMESTAMPTZ",
    "ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS damage_description TEXT",
    "ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS auction_date TIMESTAMPTZ",
    "ALTER TABLE filters       ADD COLUMN IF NOT EXISTS auction_date_from DATE",
    "ALTER TABLE filters       ADD COLUMN IF NOT EXISTS auction_date_to DATE",
    "CREATE INDEX IF NOT EXISTS idx_seen_listings_auction_date ON seen_listings (auction_date)",
    # Расширенные поля лота Copart
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS currency TEXT",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS image_url TEXT",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS title_group TEXT",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS has_keys TEXT",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS run_and_drive BOOLEAN",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS buy_now_price INTEGER",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS repair_cost INTEGER",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS odometer_brand TEXT",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS vin TEXT",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS specs TEXT",
    "ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS auction_notify_stage SMALLINT DEFAULT 0",
    "ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS currency TEXT",
    "ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS image_url TEXT",
    # Расширенные фильтры Copart
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS title_groups TEXT[]",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS damage_exclude TEXT[]",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS yards TEXT[]",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS run_and_drive BOOLEAN",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS buy_now_only BOOLEAN",
    "CREATE INDEX IF NOT EXISTS idx_seen_listings_auction_notify "
    "ON seen_listings (source, auction_date, auction_notify_stage)",
)


async def _apply_migrations(pool: asyncpg.Pool):
    for sql in _MIGRATIONS:
        try:
            await pool.execute(sql)
        except Exception as e:
            logger.warning(f"миграция не применена ({sql[:60]}…): {e}")


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await _apply_migrations(_pool)
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
    auction_date_from: Optional[datetime.date] = None,
    auction_date_to: Optional[datetime.date] = None,
) -> asyncpg.Record:
    if sources is None:
        sources = ["autoru", "drom", "avito"]
    pool = await get_pool()
    return await pool.fetchrow(
        """
        INSERT INTO filters
            (user_id, name, brand, model, year_from, year_to,
             price_from, price_to, mileage_from, mileage_to,
             cities, transmission, body_type, sources,
             auction_date_from, auction_date_to)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
        RETURNING *
        """,
        user_id, name, brand, model, year_from, year_to,
        price_from, price_to, mileage_from, mileage_to,
        cities, transmission, body_type, sources,
        auction_date_from, auction_date_to,
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
        "auction_date_from", "auction_date_to",
        "title_groups", "damage_exclude", "yards", "run_and_drive", "buy_now_only",
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

# Колонки seen_listings, которые заполняются из одноимённых атрибутов Listing.
# Чтобы добавить поле, достаточно дописать его сюда и в миграции.
SEEN_COLUMNS = (
    "source", "external_id", "url", "title", "price", "year", "mileage",
    "city", "transmission",
    # Copart
    "damage_description", "auction_date", "currency", "image_url", "title_group",
    "has_keys", "run_and_drive", "buy_now_price", "repair_cost",
    "odometer_brand", "vin", "specs",
)


async def mark_seen(listing) -> bool:
    """Записать объявление, если оно ещё не попадалось. True — новое."""
    pool = await get_pool()
    source = listing.source
    url    = listing.url
    title  = listing.title
    price  = listing.price

    # Дедупликация по URL
    if url:
        exists = await pool.fetchval(
            "SELECT 1 FROM seen_listings WHERE url = $1", url
        )
        if exists:
            return False
    # Дедупликация по заголовку + цене (для Авито с нестабильными URL)
    if source == "avito" and title and price:
        exists = await pool.fetchval(
            "SELECT 1 FROM seen_listings WHERE source = $1 AND title = $2 AND price = $3",
            source, title, price
        )
        if exists:
            return False

    values      = [getattr(listing, col, None) for col in SEEN_COLUMNS]
    columns_sql = ", ".join(SEEN_COLUMNS)
    params_sql  = ", ".join(f"${i}" for i in range(1, len(SEEN_COLUMNS) + 1))

    result = await pool.execute(
        f"""
        INSERT INTO seen_listings ({columns_sql})
        VALUES ({params_sql})
        ON CONFLICT (source, external_id) DO NOTHING
        """,
        *values,
    )
    return result == "INSERT 0 1"


async def get_lots_to_remind(stage: int, within_hours: int) -> list[asyncpg.Record]:
    """
    Лоты Copart, у которых торги начнутся в ближайшие within_hours
    и которым ещё не отправляли напоминание этой стадии.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT * FROM seen_listings
        WHERE source = 'copart'
          AND auction_date IS NOT NULL
          AND auction_date > NOW()
          AND auction_date <= NOW() + ($1 || ' hours')::INTERVAL
          AND COALESCE(auction_notify_stage, 0) < $2
        ORDER BY auction_date
        """,
        str(within_hours), stage,
    )


async def set_notify_stage(external_id: str, stage: int):
    pool = await get_pool()
    await pool.execute(
        """UPDATE seen_listings SET auction_notify_stage = $1
           WHERE source = 'copart' AND external_id = $2""",
        stage, external_id,
    )


async def cleanup_old_listings(days: int = 30):
    pool = await get_pool()
    return await pool.execute(
        f"DELETE FROM seen_listings WHERE created_at < NOW() - INTERVAL '{days} days'"
    )


# ── Price history ─────────────────────────────────────────────────────────────

async def record_price(source: str, external_id: str, price: int):
    """Записываем цену если изменилась."""
    pool = await get_pool()
    last = await pool.fetchval(
        "SELECT price FROM price_history WHERE source=$1 AND external_id=$2 ORDER BY recorded_at DESC LIMIT 1",
        source, external_id
    )
    if last != price:
        await pool.execute(
            "INSERT INTO price_history (source, external_id, price) VALUES ($1,$2,$3)",
            source, external_id, price
        )
        return last  # возвращает старую цену (или None если первая запись)
    return None


async def get_price_history(source: str, external_id: str) -> list:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT price, recorded_at FROM price_history WHERE source=$1 AND external_id=$2 ORDER BY recorded_at",
        source, external_id
    )
    return [dict(r) for r in rows]


# ── Notification settings ─────────────────────────────────────────────────────

async def get_notification_settings(user_id: int) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM notification_settings WHERE user_id=$1", user_id
    )
    if row:
        return dict(row)
    return {
        "user_id": user_id,
        "price_threshold": None,
        "quiet_from": 23,
        "quiet_to": 8,
        "notify_price_drop": True,
    }


async def save_notification_settings(user_id: int, settings: dict):
    pool = await get_pool()
    await pool.execute(
        """
        INSERT INTO notification_settings
            (user_id, price_threshold, quiet_from, quiet_to, notify_price_drop)
        VALUES ($1,$2,$3,$4,$5)
        ON CONFLICT (user_id) DO UPDATE SET
            price_threshold   = EXCLUDED.price_threshold,
            quiet_from        = EXCLUDED.quiet_from,
            quiet_to          = EXCLUDED.quiet_to,
            notify_price_drop = EXCLUDED.notify_price_drop
        """,
        user_id,
        settings.get("price_threshold"),
        settings.get("quiet_from", 23),
        settings.get("quiet_to", 8),
        settings.get("notify_price_drop", True),
    )
