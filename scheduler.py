import logging
from aiogram import Bot
from fastapi import APIRouter, Header, HTTPException

from config import WEBHOOK_SECRET, SOLD_CLEAN_DAYS
from rates import usd_rub
from db.repository import (
    get_all_active_filters,
    cleanup_old_listings,
    cleanup_sold_lots,
    record_source_result,
    should_alert,
    get_admin_ids,
)
from parsers.base import SearchFilter
from parsers.copart import CopartParser
from notifier import (process_listings, notify_upcoming_auctions,
                      notify_cleaned)

logger = logging.getLogger(__name__)

# Сколько обходов подряд источник может вернуть пусто, прежде чем бить тревогу.
# Один-два пустых прогона — норма (узкий фильтр, ночь), три подряд — подозрительно.
ZERO_RUNS_ALERT = 3

SOURCE_NAME = "🟡 Copart"


async def check_source_health(bot: Bot, found: int):
    """Написать владельцу, если аукцион несколько обходов подряд пуст."""
    zero_runs = await record_source_result("copart", found)
    if found:
        return
    if not await should_alert("copart", ZERO_RUNS_ALERT):
        return
    logger.error(f"scheduler: copart пуст {zero_runs} обходов подряд")
    text = (
        f"⚠️ <b>Источник {SOURCE_NAME} молчит</b>\n\n"
        f"Уже {zero_runs} обхода подряд возвращает ноль лотов "
        f"по всем активным фильтрам.\n\n"
        f"Обычно это значит одно из двух: фильтры стали слишком узкими "
        f"либо аукцион изменил формат ответа и парсер нужно поправить.\n\n"
        f"<i>Повторю это сообщение, только когда источник снова оживёт "
        f"и опять замолчит.</i>"
    )
    for admin in await get_admin_ids():
        try:
            await bot.send_message(chat_id=admin, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"scheduler: алерт не отправлен {admin}: {e}")


copart_parser = CopartParser()


async def run_parsers(bot: Bot) -> dict:
    records = await get_all_active_filters()

    if not records:
        logger.info("scheduler: нет активных фильтров")
        return {"status": "ok", "filters": 0, "new_listings": 0}

    filters = [SearchFilter.from_record(r) for r in records]
    logger.info(f"scheduler: фильтров: {len(filters)}")

    # Курс тянем раз за обход — им пользуется калькулятор «под ключ»
    try:
        await usd_rub()
    except Exception as e:
        logger.warning(f"scheduler: курс не обновлён: {e}")

    total_new = 0
    found_total = 0        # сколько лотов дал аукцион за весь обход

    for f in filters:
        try:
            logger.info(f"scheduler: 🟡 фильтр «{f.name}»")
            listings = await copart_parser.search(f)
            found_total += len(listings)

            if listings:
                new_count = await process_listings(
                    bot=bot,
                    listings=listings,
                    chat_id=f.user_id,
                )
                total_new += new_count
                logger.info(
                    f"scheduler: «{f.name}» — "
                    f"всего {len(listings)}, новых: {new_count}"
                )

        except Exception as e:
            logger.error(f"scheduler: ошибка фильтра «{f.name}»: {e}")
            continue

    # Следим за здоровьем источника: если аукцион сменит формат ответа,
    # это иначе видно только по отсутствию уведомлений
    try:
        await check_source_health(bot, found_total)
    except Exception as e:
        logger.warning(f"scheduler: ошибка проверки источника: {e}")

    # Напоминаем о торгах, которые вот-вот начнутся
    reminders = 0
    try:
        reminders = await notify_upcoming_auctions(bot)
    except Exception as e:
        logger.warning(f"scheduler: ошибка напоминаний: {e}")

    try:
        await cleanup_old_listings(days=30)
    except Exception as e:
        logger.warning(f"scheduler: ошибка очистки: {e}")

    # Убираем отторгованные лоты у тех, кто включил автоочистку,
    # и молча отчитываемся каждому — сообщение уходит без звука
    try:
        removed = await cleanup_sold_lots(SOLD_CLEAN_DAYS)
        if removed:
            logger.info(f"scheduler: убрано отторгованных лотов: "
                        f"{sum(removed.values())} у {len(removed)} польз.")
            await notify_cleaned(bot, removed)
    except Exception as e:
        logger.warning(f"scheduler: ошибка очистки отторгованных: {e}")

    logger.info(f"scheduler: завершён, новых: {total_new}, напоминаний: {reminders}")
    return {
        "status": "ok",
        "filters": len(filters),
        "new_listings": total_new,
        "reminders": reminders,
    }


def create_scheduler_router(bot: Bot) -> APIRouter:
    router = APIRouter()

    @router.post("/run")
    async def run_endpoint(x_secret: str = Header(default="")):
        """
        Endpoint для cron-job.org.
        Заголовок: X-Secret: <WEBHOOK_SECRET>
        """
        if x_secret != WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Forbidden")
        return await run_parsers(bot)

    @router.get("/")
    @router.head("/")
    async def root():
        return {"status": "ok"}

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    @router.get("/run_now")
    async def run_now():
        """Запустить обход вручную через браузер (без авторизации — только для тестов)."""
        result = await run_parsers(bot)
        return result

    @router.get("/debug/filters")
    async def debug_filters():
        """
        Что видит каждый активный фильтр прямо сейчас.

        Отвечает на вопрос «почему тишина»: фильтр слишком узкий, лоты
        не проходят по цене, или они найдены, но уже отправлялись раньше.
        """
        from db.repository import get_all_active_filters, get_pool
        from parsers.copart import selected_brands, selected_models, _build_filter

        pool = await get_pool()
        records = await get_all_active_filters()
        out = []

        for rec in records:
            f = SearchFilter.from_record(rec)
            item = {
                "id": f.id, "имя": f.name, "владелец": f.user_id,
                "марки": selected_brands(f), "модели": selected_models(f),
                "год": [f.year_from, f.year_to],
                "цена": [f.price_from, f.price_to],
                "пробег_до": f.mileage_to,
                "документ": f.title_groups, "площадки": f.yards,
                "исключено": f.damage_exclude,
                "на_ходу": f.run_and_drive, "купить_сразу": f.buy_now_only,
            }
            item["запрос_к_аукциону"] = _build_filter(f)
            try:
                p = await copart_parser.preview(f)
                item["нашлось_на_аукционе"] = p.get("total")
                item["прошло_фильтры"] = p.get("matched")
                item["проверено"] = p.get("checked")
                item["примечание"] = p.get("note") or ""
                item["примеры"] = [
                    {"лот": l.external_id, "название": l.title,
                     "цена": l.price, "купить_сразу": l.buy_now_price}
                    for l in p.get("sample", [])[:3]
                ]
            except Exception as e:
                item["ошибка_предпросмотра"] = f"{type(e).__name__}: {e}"

            # Сколько уже отправлено этому владельцу
            item["уже_отправлено_владельцу"] = await pool.fetchval(
                "SELECT COUNT(*) FROM user_seen WHERE user_id = $1", f.user_id)
            out.append(item)

        # Кто чем владеет — по этому видно, почему бот показывает пустой
        # список, а уведомления при этом приходят
        owners = await pool.fetch(
            """SELECT f.user_id,
                      COUNT(*) AS фильтров,
                      (SELECT COUNT(*) FROM user_seen us
                       WHERE us.user_id = f.user_id) AS отправлено
               FROM filters f GROUP BY f.user_id ORDER BY f.user_id"""
        )
        totals = {
            "активных_фильтров": len(records),
            "всего_фильтров": await pool.fetchval("SELECT COUNT(*) FROM filters"),
            "лотов_в_каталоге": await pool.fetchval(
                "SELECT COUNT(*) FROM seen_listings WHERE source = 'copart'"),
            "пользователей": await pool.fetchval("SELECT COUNT(*) FROM users"),
            "владельцы": [dict(o) for o in owners],
        }
        return {"итого": totals, "фильтры": out}

    @router.get("/debug/dbcheck")
    async def debug_dbcheck():
        """
        Прогнать все запросы к БД по разу и показать, какие падают.

        Локально настоящего Postgres нет, поэтому ошибки вроде несводимых
        типов параметров вылезают только на проде. Этот эндпоинт находит
        их все за один заход, а не по одной на деплой.

        Пишущие запросы выполняются от лица служебного пользователя
        с отрицательным id и убираются за собой.
        """
        import datetime
        from db import repository as r

        PROBE = -1        # служебный id, с настоящими не пересекается
        results: dict[str, str] = {}

        async def check(name, coro):
            try:
                await coro
                results[name] = "ok"
            except Exception as e:
                results[name] = f"{type(e).__name__}: {e}"

        # Чтение
        await check("get_all_active_filters", r.get_all_active_filters())
        await check("get_active_filters", r.get_active_filters(PROBE))
        await check("get_users", r.get_users())
        await check("get_admin_ids", r.get_admin_ids())
        await check("is_user_allowed", r.is_user_allowed(PROBE))
        await check("get_notification_settings", r.get_notification_settings(PROBE))
        await check("find_lots", r.find_lots(PROBE, "TEST"))
        await check("get_user_lots", r.get_user_lots(PROBE, 5, 0))
        await check("count_relists", r.count_relists("TESTVIN", "0"))
        await check("get_relist_history", r.get_relist_history("TESTVIN"))
        await check("get_price_history", r.get_price_history("copart", "0"))
        await check("get_lots_to_remind", r.get_lots_to_remind(1, 24))
        for group in ("model", "year", "damage", "title_group", "state"):
            await check(f"copart_price_stats:{group}",
                        r.copart_price_stats(PROBE, group))

        # Запись — от служебного пользователя
        await check("register_user", r.register_user(PROBE, "probe", "Проверка"))
        await check("set_user_active", r.set_user_active(PROBE, True))
        await check("save_notification_settings",
                    r.save_notification_settings(PROBE, {}))
        await check("record_source_result", r.record_source_result("__probe__", 1))
        await check("should_alert", r.should_alert("__probe__", 99))
        await check("set_notify_stage", r.set_notify_stage(PROBE, "0", 0))
        await check("add_favorite_from_seen",
                    r.add_favorite_from_seen(PROBE, "copart", "0"))
        await check("is_favorite", r.is_favorite(PROBE, "copart", "0"))
        await check("cleanup_old_listings", r.cleanup_old_listings(days=3650))
        await check("cleanup_sold_lots", r.cleanup_sold_lots(SOLD_CLEAN_DAYS))

        # Полный цикл фильтра: создать → скопировать → изменить → выключить
        try:
            f = await r.create_filter(user_id=PROBE, name="проверка",
                                      brands=["TOYOTA"], models=["CAMRY"])
            results["create_filter"] = "ok"
        except Exception as e:
            results["create_filter"] = f"{type(e).__name__}: {e}"
            f = None

        if f:
            await check("duplicate_filter", r.duplicate_filter(f["id"], PROBE))
            await check("update_filter_field",
                        r.update_filter_field(f["id"], PROBE, "name", "проверка2"))
            await check("get_filter_by_id", r.get_filter_by_id(f["id"], PROBE))
            await check("toggle_filter", r.toggle_filter(f["id"], PROBE, False))
            await check("delete_filter", r.delete_filter(f["id"], PROBE))

        # Уборка за собой
        pool = await r.get_pool()
        for sql in ("DELETE FROM filters WHERE user_id = $1",
                    "DELETE FROM favorites WHERE user_id = $1",
                    "DELETE FROM user_seen WHERE user_id = $1",
                    "DELETE FROM notification_settings WHERE user_id = $1",
                    "DELETE FROM users WHERE user_id = $1"):
            try:
                await pool.execute(sql, PROBE)
            except Exception as e:
                results["cleanup"] = f"{type(e).__name__}: {e}"
        try:
            await pool.execute("DELETE FROM source_health WHERE source = '__probe__'")
        except Exception:
            pass

        broken = {k: v for k, v in results.items() if v != "ok"}
        return {
            "проверено": len(results),
            "сломано": len(broken),
            "ошибки": broken or "нет",
            "время": str(datetime.datetime.now(datetime.timezone.utc)),
        }

    @router.get("/debug/copart")
    async def debug_copart():
        """
        Что реально отдаёт Copart с этого сервера.
        Нужен потому, что с домашнего IP endpoint отвечает JSON,
        а с IP дата-центра может прилетать страница защиты.
        """
        import aiohttp
        from parsers.copart import (
            SEARCH_URL, WARMUP_URL, HEADERS, PAGE_HEADERS, TIMEOUT,
            _looks_like_challenge, _build_payload,
        )
        from parsers.base import SearchFilter

        out = {}
        payload = _build_payload(
            SearchFilter(id=0, user_id=0, name="debug", brand="CHEVROLET",
                         sources=["copart"]), 0)
        payload["size"] = 1

        # 1. Прямой POST без прогрева
        async with aiohttp.ClientSession() as s:
            try:
                async with s.post(SEARCH_URL, json=payload, headers=HEADERS,
                                  timeout=TIMEOUT) as r:
                    body = await r.text()
                out["без_прогрева"] = {
                    "status": r.status,
                    "content_type": r.headers.get("Content-Type"),
                    "bytes": len(body),
                    "похоже_на_защиту": _looks_like_challenge(body),
                    "начало": body[:300],
                }
            except Exception as e:
                out["без_прогрева"] = {"ошибка": str(e)}

        # 2. С прогревом и cookie
        jar = aiohttp.CookieJar()
        async with aiohttp.ClientSession(cookie_jar=jar) as s:
            try:
                async with s.get(WARMUP_URL, headers=PAGE_HEADERS,
                                 timeout=TIMEOUT) as r:
                    warm = await r.text()
                out["прогрев"] = {
                    "status": r.status, "bytes": len(warm),
                    "cookie": [c.key for c in jar],
                }
            except Exception as e:
                out["прогрев"] = {"ошибка": str(e)}

            try:
                async with s.post(SEARCH_URL, json=payload, headers=HEADERS,
                                  timeout=TIMEOUT) as r:
                    body = await r.text()
                out["после_прогрева"] = {
                    "status": r.status,
                    "content_type": r.headers.get("Content-Type"),
                    "bytes": len(body),
                    "похоже_на_защиту": _looks_like_challenge(body),
                    "начало": body[:300],
                }
            except Exception as e:
                out["после_прогрева"] = {"ошибка": str(e)}

        from config import SCRAPER_API_KEY
        out["scraperapi_ключ_задан"] = bool(SCRAPER_API_KEY)
        return out

    return router
