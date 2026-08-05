# miniapp_api.py — REST API для Mini App
import logging
from typing import Optional
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel

from db.repository import get_pool, get_active_filters, delete_filter, toggle_filter
from config import OWNER_ID
from rates import usd_rub
from auth import current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def api_stats(user_id: int = Depends(current_user)):
    pool = await get_pool()
    # Считаем присланное этому пользователю, а не всю базу
    seen_total = await pool.fetchval(
        "SELECT COUNT(*) FROM user_seen WHERE user_id=$1", user_id)
    seen_24h = await pool.fetchval(
        "SELECT COUNT(*) FROM user_seen WHERE user_id=$1 "
        "AND created_at > NOW() - INTERVAL '24 hours'", user_id)
    seen_1h = await pool.fetchval(
        "SELECT COUNT(*) FROM user_seen WHERE user_id=$1 "
        "AND created_at > NOW() - INTERVAL '1 hour'", user_id)
    active_filters = await pool.fetchval("SELECT COUNT(*) FROM filters WHERE user_id=$1 AND is_active=TRUE", user_id)

    # Последние объявления — с валютой, иначе доллары подписываются рублями
    recent = await pool.fetch(
        """SELECT s.source, s.external_id, s.url, s.title, s.price, s.city,
                  us.created_at, s.currency, s.image_url
           FROM user_seen us JOIN seen_listings s ON s.source = us.source AND s.external_id = us.external_id WHERE us.user_id = $1
           ORDER BY us.created_at DESC LIMIT 20""",
        user_id,
    )

    # Топ дешёвых за 24 часа.
    # Лоты Copart в долларах, российские — в рублях; сортировать по сырому
    # числу нельзя, иначе аукцион вытеснит всё остальное. Приводим к рублям.
    rate = await usd_rub()
    top_deals = await pool.fetch(
        """SELECT s.source, s.external_id, s.url, s.title, s.price, s.city,
                  us.created_at, s.currency, s.image_url,
                  CASE WHEN s.source = 'copart' THEN ROUND(s.price * $2)::INT
                       ELSE s.price END AS price_rub
           FROM user_seen us JOIN seen_listings s ON s.source = us.source AND s.external_id = us.external_id WHERE us.user_id = $1
             AND us.created_at > NOW() - INTERVAL '24 hours'
             AND s.price IS NOT NULL AND s.price > 0
           ORDER BY price_rub ASC LIMIT 10""",
        user_id, rate,
    )

    # Активность по часам за последние 24ч (для графика)
    hourly = await pool.fetch(
        """
        SELECT DATE_TRUNC('hour', us.created_at) as hour, COUNT(*) as cnt
        FROM user_seen us
        WHERE us.user_id = $1 AND us.created_at > NOW() - INTERVAL '24 hours'
        GROUP BY hour ORDER BY hour
        """,
        user_id,
    )

    # Активность по дням за 7 дней
    daily = await pool.fetch(
        """
        SELECT DATE_TRUNC('day', us.created_at) as day, COUNT(*) as cnt
        FROM user_seen us
        WHERE us.user_id = $1 AND us.created_at > NOW() - INTERVAL '7 days'
        GROUP BY day ORDER BY day
        """,
        user_id,
    )

    return {
        "seen_total":      seen_total,
        "seen_24h":        seen_24h,
        "seen_1h":         seen_1h,
        "active_filters":  active_filters,
        "usd_rate":        round(rate, 2),
        "top_deals":       [dict(r) for r in top_deals],
        "recent_listings": [dict(r) for r in recent],
        "hourly": [{"hour": str(r["hour"]), "cnt": r["cnt"]} for r in hourly],
        "daily":  [{"day":  str(r["day"]),  "cnt": r["cnt"]} for r in daily],
    }


# ── Listings ──────────────────────────────────────────────────────────────────

@router.get("/listings")
async def api_listings(page: int = 1, source: str = "", limit: int = 20,
                       user_id: int = Depends(current_user)):
    pool = await get_pool()
    offset = (page - 1) * limit

    # Только объявления этого пользователя
    join = ("FROM user_seen us JOIN seen_listings s "
            "ON s.source = us.source AND s.external_id = us.external_id "
            "WHERE us.user_id = $1")
    args: list = [user_id]
    if source:
        args.append(source)
        join += f" AND s.source = ${len(args)}"

    try:
        total = await pool.fetchval(f"SELECT COUNT(*) {join}", *args)
        rows = await pool.fetch(
            f"""SELECT s.*, us.created_at {join}
                ORDER BY us.created_at DESC
                LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}""",
            *args, limit, offset,
        )

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
    user_id: int = Depends(current_user),
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
        "date":         "us.created_at DESC",
        "price_asc":    "s.price ASC NULLS LAST",
        "price_desc":   "s.price DESC NULLS LAST",
        "year_desc":    "s.year DESC NULLS LAST",
        "mileage_asc":  "s.mileage ASC NULLS LAST",
        "auction_soon": "s.auction_date ASC NULLS LAST",
    }.get(sort, "us.created_at DESC")

    # Показываем только те лоты, которые присылали этому пользователю
    where = ["us.user_id = $1", "s.source = 'copart'"]
    params: list = [user_id]

    if q.strip():
        params.append(f"%{q.strip().upper()}%")
        n = len(params)
        where.append(
            f"(UPPER(s.title) LIKE ${n} OR UPPER(s.city) LIKE ${n}"
            f" OR s.external_id LIKE ${n} OR s.vin LIKE ${n})"
        )
    if clean:
        where.append("UPPER(s.title_group) = 'CLEAN TITLE'")
    if rnd:
        where.append("s.run_and_drive IS TRUE")
    if buynow:
        where.append("s.buy_now_price IS NOT NULL AND s.buy_now_price > 0")
    if keys:
        where.append("(s.has_keys IS NULL OR UPPER(s.has_keys) <> 'NO')")

    join_sql = ("FROM user_seen us JOIN seen_listings s "
                "ON s.source = us.source AND s.external_id = us.external_id "
                "WHERE " + " AND ".join(where))

    try:
        total = await pool.fetchval(f"SELECT COUNT(*) {join_sql}", *params)
        rows = await pool.fetch(
            f"""SELECT s.* {join_sql}
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


@router.get("/copart/overview")
async def api_copart_overview(user_id: int = Depends(current_user)):
    """Сводка по аукциону для дашборда."""
    pool = await get_pool()
    rate = await usd_rub()

    row = await pool.fetchrow(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE us.created_at > NOW() - INTERVAL '24 hours') AS new_24h,
               COUNT(*) FILTER (WHERE s.run_and_drive IS TRUE) AS run_drive,
               COUNT(*) FILTER (WHERE UPPER(s.title_group) = 'CLEAN TITLE') AS clean,
               COUNT(*) FILTER (WHERE s.buy_now_price > 0) AS buy_now,
               ROUND(AVG(s.price))::INT AS avg_price,
               MIN(s.price) AS min_price
        FROM user_seen us JOIN seen_listings s
        ON s.source = us.source AND s.external_id = us.external_id
        WHERE us.user_id = $1 AND s.source = 'copart'
        """,
        user_id,
    )

    auctions = await pool.fetch(
        """
        SELECT COUNT(*) AS cnt,
               COUNT(*) FILTER (WHERE s.auction_date < NOW() + INTERVAL '24 hours') AS today,
               COUNT(*) FILTER (WHERE s.auction_date < NOW() + INTERVAL '7 days') AS week
        FROM user_seen us JOIN seen_listings s
        ON s.source = us.source AND s.external_id = us.external_id
        WHERE us.user_id = $1 AND s.source = 'copart' AND s.auction_date > NOW()
        """,
        user_id,
    )

    damages = await pool.fetch(
        """SELECT COALESCE(s.damage_description, 'НЕ УКАЗАНО') AS name, COUNT(*) AS cnt
        FROM user_seen us JOIN seen_listings s
        ON s.source = us.source AND s.external_id = us.external_id
        WHERE us.user_id = $1 AND s.source = 'copart'
           GROUP BY name ORDER BY cnt DESC LIMIT 6""",
        user_id,
    )

    return {
        **dict(row or {}),
        "auctions": dict(auctions[0]) if auctions else {},
        "damages":  [dict(d) for d in damages],
        "usd_rate": round(rate, 2),
    }


@router.get("/copart/stats")
async def api_copart_stats(group: str = "model",
                           user_id: int = Depends(current_user)):
    """Разброс оценочных стоимостей по накопленным лотам."""
    from db.repository import copart_price_stats
    rows = await copart_price_stats(user_id, group)
    return [dict(r) for r in rows]


@router.get("/copart/cost/{external_id}")
async def api_copart_cost(external_id: str,
                          user_id: int = Depends(current_user)):
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
async def api_filters(user_id: int = Depends(current_user)):
    filters = await get_active_filters(user_id)
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
async def api_toggle_filter(filter_id: int, body: ToggleBody,
                            user_id: int = Depends(current_user)):
    ok = await toggle_filter(filter_id, user_id, body.active)
    return {"ok": ok}


@router.delete("/filters/{filter_id}")
async def api_delete_filter(filter_id: int,
                            user_id: int = Depends(current_user)):
    ok = await delete_filter(filter_id, user_id)
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
async def api_get_favorites(user_id: int = Depends(current_user)):
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM favorites WHERE user_id=$1 ORDER BY created_at DESC",
        user_id
    )
    return [dict(r) for r in rows]


@router.post("/favorites")
async def api_add_favorite(item: FavItem,
                           user_id: int = Depends(current_user)):
    pool = await get_pool()
    try:
        await pool.execute(
            """
            INSERT INTO favorites
                (user_id, source, external_id, url, title, price, year, mileage, city, filter_name)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (user_id, source, external_id) DO NOTHING
            """,
            user_id, item.source, item.external_id, item.url,
            item.title, item.price, item.year, item.mileage, item.city, item.filter_name,
        )
        return {"ok": True}
    except Exception as e:
        logger.error(f"favorites add error: {e}")
        return {"ok": False}


@router.delete("/favorites/{source}/{external_id}")
async def api_remove_favorite(source: str, external_id: str,
                              user_id: int = Depends(current_user)):
    pool = await get_pool()
    await pool.execute(
        "DELETE FROM favorites WHERE user_id=$1 AND source=$2 AND external_id=$3",
        user_id, source, external_id
    )
    return {"ok": True}


# ── Notification settings ─────────────────────────────────────────────────────

class NotifSettings(BaseModel):
    price_threshold:    Optional[int]  = None
    quiet_from:         Optional[int]  = 23
    quiet_to:           Optional[int]  = 8
    notify_price_drop:  Optional[bool] = True


@router.get("/settings")
async def api_get_settings(user_id: int = Depends(current_user)):
    from db.repository import get_notification_settings
    return await get_notification_settings(user_id)


@router.post("/settings")
async def api_save_settings(body: NotifSettings,
                            user_id: int = Depends(current_user)):
    from db.repository import save_notification_settings
    await save_notification_settings(user_id, body.dict())
    return {"ok": True}


# ── Price history ─────────────────────────────────────────────────────────────

@router.get("/price_history/{source}/{external_id}")
async def api_price_history(source: str, external_id: str,
                            user_id: int = Depends(current_user)):
    from db.repository import get_price_history
    return await get_price_history(source, external_id)


# ── Seen ──────────────────────────────────────────────────────────────────────

@router.post("/seen/clear")
async def api_clear_seen(user_id: int = Depends(current_user)):
    pool = await get_pool()
    # Удаляем только свою историю: каталог лотов общий и нужен остальным
    await pool.execute("DELETE FROM user_seen WHERE user_id = $1", user_id)
    return {"ok": True}
