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
        "SELECT source, title, price, city, created_at FROM seen_listings ORDER BY created_at DESC LIMIT 10"
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
    where = "WHERE source = $3" if source else ""
    args = [limit, offset]
    if source:
        args.append(source)

    rows = await pool.fetch(
        f"SELECT * FROM seen_listings {where} ORDER BY created_at DESC LIMIT $1 OFFSET $2",
        *args
    )
    total = await pool.fetchval(
        f"SELECT COUNT(*) FROM seen_listings {where}",
        *(args[2:] if source else [])
    )
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page":  page,
        "pages": (total + limit - 1) // limit,
    }


# ── Filters ───────────────────────────────────────────────────────────────────

@router.get("/filters")
async def api_filters():
    filters = await get_active_filters(OWNER_ID)
    return [{
        "id":           f["id"],
        "name":         f["name"],
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


# ── Seen ──────────────────────────────────────────────────────────────────────

@router.post("/seen/clear")
async def api_clear_seen():
    pool = await get_pool()
    await pool.execute("DELETE FROM seen_listings")
    return {"ok": True}
