import datetime
import logging
import asyncpg
from typing import Optional
from config import DATABASE_URL, OWNER_ID

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
    # На боевой базе таблица создавалась до появления этой колонки,
    # а CREATE TABLE IF NOT EXISTS её не добавляет
    "ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS transmission TEXT",
    "ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS filter_name TEXT",
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
    # ── Многопользовательский режим ───────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS users (
           user_id      BIGINT PRIMARY KEY,
           username     TEXT,
           first_name   TEXT,
           is_active    BOOLEAN DEFAULT TRUE,
           is_admin     BOOLEAN DEFAULT FALSE,
           created_at   TIMESTAMPTZ DEFAULT NOW(),
           last_seen_at TIMESTAMPTZ DEFAULT NOW()
       )""",
    """CREATE TABLE IF NOT EXISTS user_seen (
           user_id     BIGINT NOT NULL,
           source      TEXT   NOT NULL,
           external_id TEXT   NOT NULL,
           filter_name TEXT,
           auction_notify_stage SMALLINT DEFAULT 0,
           created_at  TIMESTAMPTZ DEFAULT NOW(),
           PRIMARY KEY (user_id, source, external_id)
       )""",
    "CREATE INDEX IF NOT EXISTS idx_user_seen_created "
    "ON user_seen (user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_user_seen_lot "
    "ON user_seen (source, external_id)",
)

# Разовый перенос данных при переходе на многопользовательский режим.
# Всё, что бот уже присылал, закрепляем за владельцем — иначе после
# обновления ему прилетит вся история заново.
_BACKFILL = (
    """INSERT INTO users (user_id, is_admin)
       VALUES ($1, TRUE)
       ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE""",
    """INSERT INTO users (user_id)
       SELECT DISTINCT user_id FROM filters
       ON CONFLICT (user_id) DO NOTHING""",
    """INSERT INTO user_seen (user_id, source, external_id,
                              auction_notify_stage, created_at)
       SELECT $1, s.source, s.external_id,
              COALESCE(s.auction_notify_stage, 0), s.created_at
       FROM seen_listings s
       ON CONFLICT (user_id, source, external_id) DO NOTHING""",
)


async def _apply_migrations(pool: asyncpg.Pool):
    for sql in _MIGRATIONS:
        try:
            await pool.execute(sql)
        except Exception as e:
            logger.warning(f"миграция не применена ({sql[:60]}…): {e}")

    # Перенос истории владельцу — выполняется один раз, дальше вхолостую
    for sql in _BACKFILL:
        try:
            # Не во всех запросах есть параметр, лишний asyncpg не примет
            args = (OWNER_ID,) if "$1" in sql else ()
            await pool.execute(sql, *args)
        except Exception as e:
            logger.warning(f"перенос данных не выполнен ({sql[:50]}…): {e}")


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


# ── Пользователи ──────────────────────────────────────────────────────────────

async def register_user(user_id: int, username: Optional[str] = None,
                        first_name: Optional[str] = None) -> tuple[bool, bool]:
    """
    Завести или обновить пользователя.
    Возвращает (доступ_разрешён, это_новый_пользователь).
    """
    pool = await get_pool()
    # Типы параметров указаны явно: $1 попадает и в bigint-колонку,
    # и в сравнение — без приведения Postgres не может вывести тип
    row = await pool.fetchrow(
        """
        INSERT INTO users (user_id, username, first_name, is_admin)
        VALUES ($1::BIGINT, $2::TEXT, $3::TEXT, $1::BIGINT = $4::BIGINT)
        ON CONFLICT (user_id) DO UPDATE SET
            username     = COALESCE(EXCLUDED.username, users.username),
            first_name   = COALESCE(EXCLUDED.first_name, users.first_name),
            last_seen_at = NOW()
        RETURNING is_active, (xmax = 0) AS is_new
        """,
        user_id, username, first_name, OWNER_ID,
    )
    return (bool(row["is_active"]), bool(row["is_new"])) if row else (False, False)


async def is_user_allowed(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    pool = await get_pool()
    return bool(await pool.fetchval(
        "SELECT is_active FROM users WHERE user_id = $1", user_id))


async def get_users() -> list[asyncpg.Record]:
    pool = await get_pool()
    return await pool.fetch(
        """SELECT u.*,
                  (SELECT COUNT(*) FROM filters f WHERE f.user_id = u.user_id) AS filters
           FROM users u ORDER BY created_at"""
    )


async def set_user_active(user_id: int, active: bool) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE users SET is_active = $1 WHERE user_id = $2", active, user_id)
    return result == "UPDATE 1"


async def get_admin_ids() -> list[int]:
    """Кому слать технические алерты. Владелец в списке всегда."""
    pool = await get_pool()
    rows = await pool.fetch("SELECT user_id FROM users WHERE is_admin IS TRUE")
    ids = {r["user_id"] for r in rows}
    ids.add(OWNER_ID)
    return sorted(ids)


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


async def mark_seen(listing, user_id: int) -> bool:
    """
    Записать объявление и отметить, что его показали этому пользователю.
    True — для него это новое.

    seen_listings — общий каталог лотов: один лот там лежит в одном экземпляре,
    сколько бы пользователей его ни нашли. Факт показа хранится в user_seen,
    поэтому лот, найденный одним пользователем, не пропадает у остальных.
    """
    pool = await get_pool()
    source = listing.source

    # Каталог: добавляем лот, если его там ещё нет
    values      = [getattr(listing, col, None) for col in SEEN_COLUMNS]
    columns_sql = ", ".join(SEEN_COLUMNS)
    params_sql  = ", ".join(f"${i}" for i in range(1, len(SEEN_COLUMNS) + 1))
    await pool.execute(
        f"""INSERT INTO seen_listings ({columns_sql})
            VALUES ({params_sql})
            ON CONFLICT (source, external_id) DO NOTHING""",
        *values,
    )

    # Авито меняет URL у одного и того же объявления, поэтому для него
    # дополнительно проверяем совпадение по заголовку и цене
    if source == "avito" and listing.title and listing.price:
        twin = await pool.fetchval(
            """SELECT 1 FROM user_seen us
               JOIN seen_listings s
                 ON s.source = us.source AND s.external_id = us.external_id
               WHERE us.user_id = $1 AND s.source = 'avito'
                 AND s.title = $2 AND s.price = $3
                 AND s.external_id <> $4""",
            user_id, listing.title, listing.price, listing.external_id,
        )
        if twin:
            return False

    result = await pool.execute(
        """INSERT INTO user_seen (user_id, source, external_id, filter_name)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (user_id, source, external_id) DO NOTHING""",
        user_id, source, listing.external_id, listing.filter_name,
    )
    return result == "INSERT 0 1"


# Лоты, которые присылали конкретному пользователю. Пишется в каждый
# запрос, чтобы один пользователь не видел находки другого.
USER_LOTS_JOIN = """
    FROM user_seen us
    JOIN seen_listings s
      ON s.source = us.source AND s.external_id = us.external_id
    WHERE us.user_id = $1 AND s.source = 'copart'
"""


async def find_lots(user_id: int, query: str, limit: int = 10) -> list[asyncpg.Record]:
    """
    Поиск среди лотов Copart, которые присылали этому пользователю —
    по номеру лота, VIN или названию.
    VIN у Copart частично замаскирован (2GNFLNEK9C6******), поэтому
    сравниваем по началу строки.
    """
    q = query.strip().upper()
    if not q:
        return []
    pool = await get_pool()
    return await pool.fetch(
        f"""
        SELECT s.* {USER_LOTS_JOIN}
          AND (s.external_id = $2 OR s.vin LIKE $3 OR UPPER(s.title) LIKE $4)
        ORDER BY us.created_at DESC
        LIMIT $5
        """,
        user_id, q, q + "%", "%" + q + "%", limit,
    )


async def get_user_lots(user_id: int, limit: int, offset: int,
                        order: str = "us.created_at DESC",
                        extra_where: str = "", extra_args: tuple = ()) -> tuple:
    """Лоты пользователя постранично. Возвращает (строки, всего)."""
    pool = await get_pool()
    where = USER_LOTS_JOIN + extra_where
    total = await pool.fetchval(
        f"SELECT COUNT(*) {where}", user_id, *extra_args)
    n = len(extra_args)
    rows = await pool.fetch(
        f"""SELECT s.* {where} ORDER BY {order}
            LIMIT ${n + 2} OFFSET ${n + 3}""",
        user_id, *extra_args, limit, offset,
    )
    return rows, total


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


async def copart_price_stats(user_id: int, group_by: str = "model",
                             limit: int = 15) -> list[asyncpg.Record]:
    """
    Разброс оценочных стоимостей по лотам, которые видел этот пользователь.
    Цену продажи Copart не отдаёт, поэтому считаем по оценкам (`la`):
    сколько таких машин видели и в какую вилку они укладываются.
    """
    expr = {
        # Марка и модель из заголовка вида «2016 CHEVROLET CRUZE LT»
        "model":  "SPLIT_PART(s.title, ' ', 2) || ' ' || SPLIT_PART(s.title, ' ', 3)",
        "year":   "s.year::TEXT",
        "damage": "COALESCE(s.damage_description, 'НЕ УКАЗАНО')",
        "title_group": "COALESCE(s.title_group, 'НЕ УКАЗАН')",
        "state":  "SPLIT_PART(s.city, ' - ', 1)",
    }.get(group_by)
    if not expr:
        return []

    pool = await get_pool()
    return await pool.fetch(
        f"""
        SELECT {expr} AS bucket,
               COUNT(*)                        AS cnt,
               MIN(s.price)                    AS min_price,
               ROUND(AVG(s.price))::INT        AS avg_price,
               MAX(s.price)                    AS max_price,
               ROUND(AVG(s.repair_cost))::INT  AS avg_repair,
               ROUND(AVG(s.mileage))::INT      AS avg_mileage
        {USER_LOTS_JOIN}
          AND s.price IS NOT NULL AND s.price > 0
        GROUP BY bucket
        HAVING COUNT(*) >= 2
        ORDER BY cnt DESC
        LIMIT $2
        """,
        user_id, limit,
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
    Лоты Copart, у которых торги вот-вот начнутся, вместе с получателем.
    Напоминаем только тем, кому этот лот присылали, и только раз на стадию.
    """
    pool = await get_pool()
    return await pool.fetch(
        """
        SELECT us.user_id, s.*
        FROM user_seen us
        JOIN seen_listings s
          ON s.source = us.source AND s.external_id = us.external_id
        WHERE s.source = 'copart'
          AND s.auction_date IS NOT NULL
          AND s.auction_date > NOW()
          AND s.auction_date <= NOW() + ($1 || ' hours')::INTERVAL
          AND COALESCE(us.auction_notify_stage, 0) < $2
        ORDER BY s.auction_date
        """,
        str(within_hours), stage,
    )


async def set_notify_stage(user_id: int, external_id: str, stage: int):
    pool = await get_pool()
    await pool.execute(
        """UPDATE user_seen SET auction_notify_stage = $1
           WHERE user_id = $2 AND source = 'copart' AND external_id = $3""",
        stage, user_id, external_id,
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
    # Осиротевшие записи — самого объявления уже нет
    orphans = await pool.execute(
        """DELETE FROM price_history ph
           WHERE NOT EXISTS (
               SELECT 1 FROM seen_listings s
               WHERE s.source = ph.source AND s.external_id = ph.external_id
           )"""
    )
    user_orphans = await pool.execute(
        """DELETE FROM user_seen us
           WHERE NOT EXISTS (
               SELECT 1 FROM seen_listings s
               WHERE s.source = us.source AND s.external_id = us.external_id
           )"""
    )
    return {"listings": listings, "price_history": orphans,
            "user_seen": user_orphans}


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
        VALUES ($1::TEXT, CASE WHEN $2::INT > 0 THEN 0 ELSE 1 END,
                          CASE WHEN $2::INT > 0 THEN NOW() ELSE NULL END, FALSE)
        ON CONFLICT (source) DO UPDATE SET
            zero_runs = CASE WHEN $2::INT > 0 THEN 0
                             ELSE source_health.zero_runs + 1 END,
            last_ok   = CASE WHEN $2::INT > 0 THEN NOW()
                             ELSE source_health.last_ok END,
            alerted   = CASE WHEN $2::INT > 0 THEN FALSE
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
