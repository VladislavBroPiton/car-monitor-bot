# miniapp_api.py — REST API для Mini App
import logging
from typing import Optional
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel

from db.repository import get_pool, get_active_filters, delete_filter, toggle_filter
from config import OWNER_ID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def api_stats():
    pool = await get_pool()
    seen_total     = await pool.fetchval("SELECT COUNT(*) FROM seen_listings")
    seen_24h       = await pool.fetchval("SELECT COUNT(*) FROM seen_listings WHERE created_at > NOW() - INTERVAL '24 hours'")
    seen_1h        = await pool.fetchval("SELECT COUNT(*) FROM seen_listings WHERE created_at > NOW() - INTERVAL '1 hour'")
    active_filters = await pool.fetchval("SELECT COUNT(*) FROM filters WHERE user_id=$1 AND is_active=TRUE", OWNER_ID)

    # Последние 10 объявлений
    recent = await pool.fetch(
        "SELECT source, external_id, url, title, price, city, created_at FROM seen_listings ORDER BY created_at DESC LIMIT 20"
    )

    # Топ дешёвых за 24 часа
    top_deals = await pool.fetch(
        """SELECT source, external_id, url, title, price, city, created_at
           FROM seen_listings
           WHERE created_at > NOW() - INTERVAL '24 hours'
           AND price IS NOT NULL AND price > 0
           ORDER BY price ASC LIMIT 10"""
    )

    # Активность по часам за последние 24ч (для графика)
    hourly = await pool.fetch(
        """
        SELECT DATE_TRUNC('hour', created_at) as hour, COUNT(*) as cnt
        FROM seen_listings
        WHERE created_at > NOW() - INTERVAL '24 hours'
        GROUP BY hour ORDER BY hour
        """
    )

    # Активность по дням за 7 дней
    daily = await pool.fetch(
        """
        SELECT DATE_TRUNC('day', created_at) as day, COUNT(*) as cnt
        FROM seen_listings
        WHERE created_at > NOW() - INTERVAL '7 days'
        GROUP BY day ORDER BY day
        """
    )

    return {
        "seen_total":      seen_total,
        "seen_24h":        seen_24h,
        "seen_1h":         seen_1h,
        "active_filters":  active_filters,
        "recent_listings": [dict(r) for r in recent],
        "hourly": [{"hour": str(r["hour"]), "cnt": r["cnt"]} for r in hourly],
        "daily":  [{"day":  str(r["day"]),  "cnt": r["cnt"]} for r in daily],
    }


# ── Listings ──────────────────────────────────────────────────────────────────

@router.get("/listings")
async def api_listings(page: int = 1, source: str = "", limit: int = 20):
    pool = await get_pool()
    offset = (page - 1) * limit

    try:
        if source:
            rows = await pool.fetch(
                "SELECT * FROM seen_listings WHERE source = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3",
                source, limit, offset
            )
            total = await pool.fetchval(
                "SELECT COUNT(*) FROM seen_listings WHERE source = $1", source
            )
        else:
            rows = await pool.fetch(
                "SELECT * FROM seen_listings ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                limit, offset
            )
            total = await pool.fetchval("SELECT COUNT(*) FROM seen_listings")

        def row_to_dict(r):
            d = dict(r)
            # Конвертируем datetime в строку
            if 'created_at' in d and d['created_at']:
                d['created_at'] = str(d['created_at'])
            return d

        return {
            "items": [row_to_dict(r) for r in rows],
            "total": total,
            "page":  page,
            "pages": max(1, (total + limit - 1) // limit),
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"api_listings error: {e}")
        raise


# ── Copart ────────────────────────────────────────────────────────────────────

@router.get("/copart/listings")
async def api_copart_listings(
    page: int = 1,
    limit: int = 20,
    sort: str = "date",
    q: str = "",
    clean: bool = False,
    rnd: bool = False,
    buynow: bool = False,
    keys: bool = False,
):
    """
    Лоты аукциона Copart. Цены — в долларах (валюта торгов),
    пробег — в милях, auction_date — дата ближайших торгов.

    Фильтры применяются в SQL, а не по загруженной странице, иначе
    «на ходу» отбирал бы только среди 20 видимых лотов.
    """
    pool = await get_pool()
    offset = (page - 1) * limit

    order = {
        "date":         "created_at DESC",
        "price_asc":    "price ASC NULLS LAST",
        "price_desc":   "price DESC NULLS LAST",
        "year_desc":    "year DESC NULLS LAST",
        "mileage_asc":  "mileage ASC NULLS LAST",
        "auction_soon": "auction_date ASC NULLS LAST",
    }.get(sort, "created_at DESC")

    where = ["source = 'copart'"]
    params: list = []

    if q.strip():
        params.append(f"%{q.strip().upper()}%")
        where.append(
            f"(UPPER(title) LIKE ${len(params)} OR UPPER(city) LIKE ${len(params)}"
            f" OR external_id LIKE ${len(params)} OR vin LIKE ${len(params)})"
        )
    if clean:
        where.append("UPPER(title_group) = 'CLEAN TITLE'")
    if rnd:
        where.append("run_and_drive IS TRUE")
    if buynow:
        where.append("buy_now_price IS NOT NULL AND buy_now_price > 0")
    if keys:
        where.append("(has_keys IS NULL OR UPPER(has_keys) <> 'NO')")

    where_sql = " AND ".join(where)

    try:
        total = await pool.fetchval(
            f"SELECT COUNT(*) FROM seen_listings WHERE {where_sql}", *params
        )
        rows = await pool.fetch(
            f"""SELECT * FROM seen_listings
                WHERE {where_sql}
                ORDER BY {order}
                LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
            *params, limit, offset,
        )
    except Exception as e:
        logger.error(f"api_copart_listings error: {e}")
        raise

    def row_to_dict(r):
        d = dict(r)
        for key in ("created_at", "auction_date"):
            if d.get(key):
                d[key] = str(d[key])
        return d

    return {
        "items": [row_to_dict(r) for r in rows],
        "total": total,
        "page":  page,
        "pages": max(1, (total + limit - 1) // limit),
    }


@router.get("/copart/stats")
async def api_copart_stats(group: str = "model"):
    """Разброс оценочных стоимостей по накопленным лотам."""
    from db.repository import copart_price_stats
    rows = await copart_price_stats(group)
    return [dict(r) for r in rows]


@router.get("/copart/cost/{external_id}")
async def api_copart_cost(external_id: str):
    """Прикидка стоимости «под ключ» по сохранённому лоту."""
    from costs import estimate
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT title, price, buy_now_price FROM seen_listings
           WHERE source = 'copart' AND external_id = $1""",
        external_id,
    )
    if not row:
        return {"ok": False, "error": "not_found"}
    breakdown = estimate(row["buy_now_price"] or row["price"])
    if not breakdown:
        return {"ok": False, "error": "no_price"}
    return {"ok": True, "title": row["title"], **breakdown.as_dict()}


# ── Filters ───────────────────────────────────────────────────────────────────

@router.get("/filters")
async def api_filters():
    filters = await get_active_filters(OWNER_ID)
    return [{
        "id":           f["id"],
        "name":         f["name"],
        "kind":         f["kind"] or "ru",
        "brand":        f["brand"],
        "model":        f["model"],
        "year_from":    f["year_from"],
        "year_to":      f["year_to"],
        "price_from":   f["price_from"],
        "price_to":     f["price_to"],
        "mileage_from": f["mileage_from"],
        "mileage_to":   f["mileage_to"],
        "cities":       list(f["cities"] or []),
        "transmission": f["transmission"],
        "body_type":    f["body_type"],
        "sources":      list(f["sources"] or []),
        "auction_date_from": str(f["auction_date_from"]) if f["auction_date_from"] else None,
        "auction_date_to":   str(f["auction_date_to"])   if f["auction_date_to"]   else None,
        "title_groups":   list(f["title_groups"]   or []),
        "damage_exclude": list(f["damage_exclude"] or []),
        "yards":          list(f["yards"]          or []),
        "run_and_drive":  f["run_and_drive"],
        "buy_now_only":   f["buy_now_only"],
        "is_active":    f["is_active"],
    } for f in filters]


class ToggleBody(BaseModel):
    active: bool

@router.post("/filters/{filter_id}/toggle")
async def api_toggle_filter(filter_id: int, body: ToggleBody):
    ok = await toggle_filter(filter_id, OWNER_ID, body.active)
    return {"ok": ok}


@router.delete("/filters/{filter_id}")
async def api_delete_filter(filter_id: int):
    ok = await delete_filter(filter_id, OWNER_ID)
    return {"ok": ok}


# ── Favorites ─────────────────────────────────────────────────────────────────

class FavItem(BaseModel):
    source:      str
    external_id: str
    url:         str
    title:       Optional[str] = None
    price:       Optional[int] = None
    year:        Optional[int] = None
    mileage:     Optional[int] = None
    city:        Optional[str] = None
    filter_name: Optional[str] = None


@router.get("/favorites")
async def api_get_favorites():
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM favorites WHERE user_id=$1 ORDER BY created_at DESC",
        OWNER_ID
    )
    return [dict(r) for r in rows]


@router.post("/favorites")
async def api_add_favorite(item: FavItem):
    pool = await get_pool()
    try:
        await pool.execute(
            """
            INSERT INTO favorites
                (user_id, source, external_id, url, title, price, year, mileage, city, filter_name)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (user_id, source, external_id) DO NOTHING
            """,
            OWNER_ID, item.source, item.external_id, item.url,
            item.title, item.price, item.year, item.mileage, item.city, item.filter_name,
        )
        return {"ok": True}
    except Exception as e:
        logger.error(f"favorites add error: {e}")
        return {"ok": False}


@router.delete("/favorites/{source}/{external_id}")
async def api_remove_favorite(source: str, external_id: str):
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM favorites WHERE user_id=$1 AND source=$2 AND external_id=$3",
        OWNER_ID, source, external_id
    )
    return {"ok": True}


# ── Notification settings ─────────────────────────────────────────────────────

class NotifSettings(BaseModel):
    price_threshold:    Optional[int]  = None
    quiet_from:         Optional[int]  = 23
    quiet_to:           Optional[int]  = 8
    notify_price_drop:  Optional[bool] = True


@router.get("/settings")
async def api_get_settings():
    from db.repository import get_notification_settings
    return await get_notification_settings(OWNER_ID)


@router.post("/settings")
async def api_save_settings(body: NotifSettings):
    from db.repository import save_notification_settings
    await save_notification_settings(OWNER_ID, body.dict())
    return {"ok": True}


# ── Price history ─────────────────────────────────────────────────────────────

@router.get("/price_history/{source}/{external_id}")
async def api_price_history(source: str, external_id: str):
    from db.repository import get_price_history
    return await get_price_history(source, external_id)


# ── Seen ──────────────────────────────────────────────────────────────────────

@router.post("/seen/clear")
async def api_clear_seen():
    pool = await get_pool()
    await pool.execute("DELETE FROM seen_listings")
    return {"ok": True}
