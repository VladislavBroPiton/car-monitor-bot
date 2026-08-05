"""
Проверка подписи initData — того, что отделяет одного пользователя от другого.

Если эта проверка сломается, любой сможет прочитать чужие фильтры,
просто дёрнув API напрямую. Поэтому она покрыта отдельно.

Запуск:  python tests/test_auth.py
"""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKEN")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgres://test/test")
os.environ.setdefault("WEBHOOK_HOST", "https://example.com")

from auth import parse_init_data, _secret_key, MAX_AGE      # noqa: E402


def make_init_data(user_id: int = 777, age: int = 0,
                   valid_key: bool = True, extra: dict = None) -> str:
    """Собрать initData так же, как это делает Telegram."""
    pairs = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps({"id": user_id, "first_name": "Тест",
                            "username": "test"}, ensure_ascii=False),
    }
    pairs.update(extra or {})
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    key = _secret_key() if valid_key else b"someone-elses-key"
    pairs["hash"] = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


def test_valid_signature_returns_user():
    user = parse_init_data(make_init_data(777))
    assert user and user["id"] == 777


def test_foreign_key_rejected():
    """Подпись чужим ключом — главный сценарий подделки."""
    assert parse_init_data(make_init_data(777, valid_key=False)) is None


def test_tampered_payload_rejected():
    """Подменили user_id, не тронув подпись."""
    data = make_init_data(777).replace("777", "888")
    assert parse_init_data(data) is None


def test_expired_rejected():
    assert parse_init_data(make_init_data(777, age=MAX_AGE + 60)) is None


def test_fresh_accepted():
    assert parse_init_data(make_init_data(777, age=MAX_AGE - 60))


def test_missing_hash_rejected():
    assert parse_init_data("auth_date=1&user=%7B%22id%22%3A1%7D") is None


def test_garbage_rejected():
    for bad in ("", "   ", "hash=abc", "не строка вовсе", "a=1&a=2&hash=x"):
        assert parse_init_data(bad) is None, bad


def test_extra_fields_do_not_break_signature():
    """Telegram может добавлять новые поля — подпись должна сходиться."""
    data = make_init_data(777, extra={"chat_type": "private", "start_param": "x"})
    user = parse_init_data(data)
    assert user and user["id"] == 777


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  OK   {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e or 'assert'}")
        except Exception as e:
            failed += 1
            print(f"  ERR  {name}: {type(e).__name__}: {e}")
    print(f"\n{'ПРОВАЛЕНО: ' + str(failed) if failed else 'ВСЕ ТЕСТЫ ПРОШЛИ'}")
    sys.exit(1 if failed else 0)
