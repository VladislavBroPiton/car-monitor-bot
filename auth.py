# auth.py — кто именно открыл Mini App
#
# Раньше API отвечал данными владельца всем подряд: user_id был захардкожен
# в OWNER_ID. В многопользовательском режиме так нельзя — нужно понимать,
# от чьего имени пришёл запрос.
#
# Telegram передаёт в Mini App строку initData, подписанную ключом,
# производным от токена бота. Подпись проверяется на сервере, подделать
# её без токена невозможно. Схема описана в документации Telegram
# («Validating data received via the Mini App»).

import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

from config import BOT_TOKEN

logger = logging.getLogger(__name__)

# Сколько initData считается свежей. Telegram кладёт в неё auth_date;
# просроченную строку не принимаем, чтобы перехваченную ссылку нельзя было
# переиспользовать бесконечно.
MAX_AGE = 24 * 3600


def _secret_key() -> bytes:
    return hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()


def parse_init_data(init_data: str) -> Optional[dict]:
    """
    Проверить подпись и вернуть данные пользователя.
    None — подпись не сошлась, строка просрочена или испорчена.
    """
    if not init_data:
        return None

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    # Строка для подписи: пары key=value, отсортированные по ключу
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    expected = hmac.new(_secret_key(), check_string.encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", 0))
    except ValueError:
        return None
    if auth_date and time.time() - auth_date > MAX_AGE:
        logger.info("initData просрочена")
        return None

    try:
        return json.loads(pairs.get("user") or "{}")
    except json.JSONDecodeError:
        return None


async def current_user(
    x_telegram_init_data: str = Header(default=""),
) -> int:
    """
    Зависимость FastAPI: id пользователя, открывшего Mini App.

    Mini App шлёт initData заголовком X-Telegram-Init-Data.
    Без валидной подписи запрос отклоняется — иначе любой смог бы
    прочитать чужие фильтры, просто дёрнув API напрямую.
    """
    user = parse_init_data(x_telegram_init_data)
    if not user or not user.get("id"):
        raise HTTPException(status_code=401, detail="Требуется вход через Telegram")
    return int(user["id"])
