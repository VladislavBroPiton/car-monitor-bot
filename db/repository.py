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
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS kind TEXT DEFAULT 'ru'",
    "UPDATE filters SET kind = 'ru' WHERE kind IS NULL",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS title_groups TEXT[]",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS damage_exclude TEXT[]",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS yards TEXT[]",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS run_and_drive BOOLEAN",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS buy_now_only BOOLEAN",
    "CREATE INDEX IF NOT EXISTS idx_seen_listings_auction_notify "
    "ON seen_listings (source, auction_date, auction_notify_stage)",
    # Поиск повторных выставлений по VIN и поиск лота вручную
    "CREATE INDEX IF NOT EXISTS idx_seen_listings_vin ON seen_listings (vin)",
    # Несколько марок и моделей в одном фильтре
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS brands TEXT[]",
    "ALTER TABLE filters ADD COLUMN IF NOT EXISTS models TEXT[]",
    # Здоровье источников
    """CREATE TABLE IF NOT EXISTS source_health (
           source    TEXT PRIMARY KEY,
           zero_runs INTEGER DEFAULT 0,
           last_ok   TIMESTAMPTZ,
           alerted   BOOLEAN DEFAULT FALSE
       )""",
    "CREATE INDEX IF NOT EXISTS idx_price_history_ext "
    "ON price_history (source, external_id)",
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
    kind: str = "ru",
    brands: Optional[list[str]] = None,
    models: Optional[list[str]] = None,
    title_groups: Optional[list[str]] = None,
    damage_exclude: Optional[list[str]] = None,
    yards: Optional[list[str]] = None,
    run_and_drive: Optional[bool] = None,
    buy_now_only: Optional[bool] = None,
) -> asyncpg.Record:
    if sources is None:
        sources = ["copart"] if kind == "copart" else ["autoru", "drom", "avito"]
    pool = await get_pool()
    return await pool.fetchrow(
        """
        INSERT INTO filters
            (user_id, name, kind, brand, model, year_from, year_to,
             price_from, price_to, mileage_from, mileage_to,
             cities, transmission, body_type, sources,
             auction_date_from, auction_date_to,
             title_groups, damage_exclude, yards, run_and_drive, buy_now_only,
             brands, models)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,
                $18,$19,$20,$21,$22,$23,$24)
        RETURNING *
        """,
        user_id, name, kind, brand, model, year_from, year_to,
        price_from, price_to, mileage_from, mileage_to,
        cities, transmission, body_type, sources,
        auction_date_from, auction_date_to,
        title_groups, damage_exclude, yards, run_and_drive, buy_now_only,
        brands, models,
    )


# Колонки, которые копируются при дублировании фильтра
FILTER_COPY_COLUMNS = (
    "user_id", "kind", "brand", "model", "brands", "models",
    "year_from", "year_to", "price_from", "price_to",
    "mileage_from", "mileage_to", "cities", "transmission", "body_type",
    "sources", "auction_date_from", "auction_date_to",
    "title_groups", "damage_exclude", "yards", "run_and_drive", "buy_now_only",
)


async def duplicate_filter(filter_id: int, user_id: int) -> Optional[asyncpg.Record]:
    """Скопировать фильтр целиком — чтобы не проходить мастер заново."""
    pool = await get_pool()
    cols = ", ".join(FILTER_COPY_COLUMNS)
    return await pool.fetchrow(
        f"""
        INSERT INTO filters (name, {cols})
        SELECT LEFT(name || ' (копия)', 64), {cols}
        FROM filters WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        filter_id, user_id,
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


async def find_lots(query: str, limit: int = 10) -> list[asyncpg.Record]:
    """
    Поиск сохранённых лотов Copart по номеру лота, VIN или названию.
    VIN у Copart частично замаскирован (2GNFLNEK9C6******), поэтому
    сравниваем по началу строки.
    """
    q = query.strip().upper()
    if not q:
        return []
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT * FROM seen_listings
        WHERE source = 'copart'
          AND (external_id = $1 OR vin LIKE $2 OR UPPER(title) LIKE $3)
        ORDER BY created_at DESC
        LIMIT $4
        """,
        q, q + "%", "%" + q + "%", limit,
    )


async def count_relists(vin: Optional[str], external_id: str) -> int:
    """
    Сколько раз этот же автомобиль уже был на торгах.
    Copart перевыставляет непроданные лоты под новым номером, но VIN тот же —
    по нему и считаем. Возвращает число прошлых появлений (0 — впервые).
    """
    if not vin or "*" not in vin and len(vin) < 6:
        return 0
    pool = await get_pool()
    return await pool.fetchval(
        """SELECT COUNT(*) FROM seen_listings
           WHERE source = 'copart' AND vin = $1 AND external_id <> $2""",
        vin, external_id,
    ) or 0


async def copart_price_stats(group_by: str = "model", limit: int = 15) -> list[asyncpg.Record]:
    """
    Разброс оценочных стоимостей по накопленным лотам.
    Цену продажи Copart не отдаёт, поэтому считаем по оценкам (`la`):
    сколько таких машин видели и в какую вилку они укладываются.
    """
    expr = {
        # Марка и модель из заголовка вида «2016 CHEVROLET CRUZE LT»
        "model":  "SPLIT_PART(title, ' ', 2) || ' ' || SPLIT_PART(title, ' ', 3)",
        "year":   "year::TEXT",
        "damage": "COALESCE(damage_description, 'НЕ УКАЗАНО')",
        "title_group": "COALESCE(title_group, 'НЕ УКАЗАН')",
        "state":  "SPLIT_PART(city, ' - ', 1)",
    }.get(group_by)
    if not expr:
        return []

    pool = await get_pool()
    return await pool.fetch(
        f"""
        SELECT {expr} AS bucket,
               COUNT(*)                      AS cnt,
               MIN(price)                    AS min_price,
               ROUND(AVG(price))::INT        AS avg_price,
               MAX(price)                    AS max_price,
               ROUND(AVG(repair_cost))::INT  AS avg_repair,
               ROUND(AVG(mileage))::INT      AS avg_mileage
        FROM seen_listings
        WHERE source = 'copart' AND price IS NOT NULL AND price > 0
        GROUP BY bucket
        HAVING COUNT(*) >= 2
        ORDER BY cnt DESC
        LIMIT $1
        """,
        limit,
    )


async def get_relist_history(vin: str) -> list[asyncpg.Record]:
    """Все появления автомобиля на торгах — от старых к новым."""
    pool = await get_pool()
    return await pool.fetch(
        """SELECT external_id, url, price, auction_date, created_at, city
           FROM seen_listings
           WHERE source = 'copart' AND vin = $1
           ORDER BY created_at""",
        vin,
    )


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


async def cleanup_old_listings(days: int = 30) -> dict:
    """
    Чистим не только объявления, но и их историю цен: раньше price_history
    рос бесконечно, потому что удаление seen_listings его не касалось.
    """
    pool = await get_pool()
    listings = await pool.execute(
        f"DELETE FROM seen_listings WHERE created_at < NOW() - INTERVAL '{days} days'"
    )
    # Осиротевшие записи истории — объявления уже нет
    orphans = await pool.execute(
        """DELETE FROM price_history ph
           WHERE NOT EXISTS (
               SELECT 1 FROM seen_listings s
               WHERE s.source = ph.source AND s.external_id = ph.external_id
           )"""
    )
    return {"listings": listings, "price_history": orphans}


# ── Здоровье источников ───────────────────────────────────────────────────────

async def record_source_result(source: str, found: int) -> int:
    """
    Запомнить итог обхода. Возвращает число подряд идущих пустых прогонов —
    по нему решаем, не сломался ли парсер после смены формата у площадки.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO source_health (source, zero_runs, last_ok, alerted)
        VALUES ($1, CASE WHEN $2 > 0 THEN 0 ELSE 1 END,
                    CASE WHEN $2 > 0 THEN NOW() ELSE NULL END, FALSE)
        ON CONFLICT (source) DO UPDATE SET
            zero_runs = CASE WHEN $2 > 0 THEN 0
                             ELSE source_health.zero_runs + 1 END,
            last_ok   = CASE WHEN $2 > 0 THEN NOW()
                             ELSE source_health.last_ok END,
            alerted   = CASE WHEN $2 > 0 THEN FALSE
                             ELSE source_health.alerted END
        RETURNING zero_runs
        """,
        source, found,
    )
    return row["zero_runs"] if row else 0


async def should_alert(source: str, threshold: int) -> bool:
    """Пора ли писать владельцу — и не писали ли уже."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT zero_runs, alerted FROM source_health WHERE source = $1", source
    )
    if not row or row["alerted"] or row["zero_runs"] < threshold:
        return False
    await pool.execute(
        "UPDATE source_health SET alerted = TRUE WHERE source = $1", source
    )
    return True


async def get_source_health() -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch("SELECT * FROM source_health ORDER BY source")


# ── Favorites ─────────────────────────────────────────────────────────────────

# Колонки, которые переносим из seen_listings в favorites
FAV_COPY_COLUMNS = (
    "source", "external_id", "url", "title", "price", "year", "mileage",
    "city", "transmission", "damage_description", "auction_date",
    "currency", "image_url",
)


async def add_favorite_from_seen(user_id: int, source: str, external_id: str) -> bool:
    """
    Скопировать объявление в избранное прямо из seen_listings.
    Раньше данные вытаскивались из текста сообщения — у карточек с фото
    текста нет вовсе, поэтому в избранное попадал мусор без ссылки.
    """
    pool = await get_pool()
    cols = ", ".join(FAV_COPY_COLUMNS)
    result = await pool.execute(
        f"""
        INSERT INTO favorites (user_id, {cols})
        SELECT $1, {cols} FROM seen_listings
        WHERE source = $2 AND external_id = $3
        ON CONFLICT (user_id, source, external_id) DO NOTHING
        """,
        user_id, source, external_id,
    )
    return result == "INSERT 0 1"


async def is_favorite(user_id: int, source: str, external_id: str) -> bool:
    pool = await get_pool()
    return bool(await pool.fetchval(
        "SELECT 1 FROM favorites WHERE user_id=$1 AND source=$2 AND external_id=$3",
        user_id, source, external_id,
    ))


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
