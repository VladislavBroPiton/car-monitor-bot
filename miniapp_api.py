# miniapp_api.py — REST API endpoints для Mini App
import logging
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from db.repository import get_pool, get_active_filters, delete_filter, toggle_filter
from config import OWNER_ID

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


@router.get("/stats")
async def api_stats():
    """Статистика для дашборда."""
    pool = await get_pool()
    seen_total = await pool.fetchval("SELECT COUNT(*) FROM seen_listings")
    seen_24h   = await pool.fetchval("SELECT COUNT(*) FROM seen_listings WHERE created_at > NOW() - INTERVAL '24 hours'")
    seen_1h    = await pool.fetchval("SELECT COUNT(*) FROM seen_listings WHERE created_at > NOW() - INTERVAL '1 hour'")
    active_filters = await pool.fetchval("SELECT COUNT(*) FROM filters WHERE user_id=$1 AND is_active=TRUE", OWNER_ID)

    recent = await pool.fetch(
        """SELECT source, title, price, city, created_at
           FROM seen_listings
           ORDER BY created_at DESC LIMIT 10"""
    )

    return {
        "seen_total":     seen_total,
        "seen_24h":       seen_24h,
        "seen_1h":        seen_1h,
        "active_filters": active_filters,
        "recent_listings": [dict(r) for r in recent],
    }


@router.get("/filters")
async def api_filters():
    """Список фильтров."""
    filters = await get_active_filters(OWNER_ID)
    result = []
    for f in filters:
        result.append({
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
        })
    return result


@router.post("/filters/{filter_id}/toggle")
async def api_toggle_filter(filter_id: int, body: dict):
    """Пауза/возобновление фильтра."""
    active = body.get("active", True)
    ok = await toggle_filter(filter_id, OWNER_ID, active)
    return {"ok": ok}


@router.delete("/filters/{filter_id}")
async def api_delete_filter(filter_id: int):
    """Удаление фильтра."""
    ok = await delete_filter(filter_id, OWNER_ID)
    return {"ok": ok}


@router.post("/seen/clear")
async def api_clear_seen():
    """Очистить seen_listings."""
    pool = await get_pool()
    await pool.execute("DELETE FROM seen_listings")
    return {"ok": True}
