# tests/test_copart.py — офлайн-проверки парсера Copart.
#
# Сеть не трогаем: разбор идёт по зафиксированной выдаче API
# (tests/fixtures/copart_search.json). Тесты ловят самое опасное —
# молчаливую поломку разбора, когда бот продолжает работать,
# но присылает карточки без цены, фото и повреждений.
#
# Запуск:  python -m pytest tests -q
#     или: python tests/test_copart.py

import datetime
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "1:test")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("DATABASE_URL", "postgres://test/test")
os.environ.setdefault("WEBHOOK_HOST", "https://example.com")
os.environ.setdefault("USD_RUB_RATE", "90")

from parsers.base import SearchFilter                      # noqa: E402
from parsers.copart import (                               # noqa: E402
    _parse_lot, _build_filter, _matches, _price_bounds_usd, _facet_values,
    damage_ru, title_ru, keys_ru, DAMAGE, DAMAGE_CODES, DAMAGE_RU,
    FETCH_LIMIT, PAGE_SIZE, MAX_PAGES,
)

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "copart_search.json").read_text(encoding="utf-8")
)
RAW_LOTS = FIXTURE["data"]["results"]["content"]


def _filter(**kw) -> SearchFilter:
    kw.setdefault("sources", ["copart"])
    return SearchFilter(id=1, user_id=1, name="тест", **kw)


# ── Разбор лота ───────────────────────────────────────────────────────────────

def test_parses_every_lot():
    for raw in RAW_LOTS:
        lot = _parse_lot(raw, "тест")
        assert lot is not None
        assert lot.source == "copart"
        assert lot.external_id
        assert lot.url.endswith(lot.external_id)


def test_key_fields_are_filled():
    """Поля, без которых карточка теряет смысл."""
    lot = _parse_lot(RAW_LOTS[0], "тест")
    assert lot.title
    assert lot.year and 1980 < lot.year < 2100
    assert lot.price and lot.price > 0
    assert lot.damage_description
    assert lot.city
    assert lot.currency in {"USD", "CAD"}
    assert lot.image_url and lot.image_url.endswith(".jpg")
    assert lot.vin


def test_image_url_upgraded_from_thumbnail():
    """В API приходит превью _thb — для карточек нужен размер побольше."""
    lot = _parse_lot(RAW_LOTS[0], "тест")
    assert "_thb.jpg" not in lot.image_url
    assert "_ful.jpg" in lot.image_url


def test_run_and_drive_flag():
    """CERT-D в поле lcc означает «заводится и едет»."""
    for raw in RAW_LOTS:
        lot = _parse_lot(raw, "тест")
        assert lot.run_and_drive == (raw.get("lcc") == "CERT-D")


def test_missing_values_become_none():
    """Copart отдаёт -1 и 0 вместо «не указано» — в модель это попасть не должно."""
    lot = _parse_lot({"lotNumberStr": "1", "ld": "TEST", "la": -1.0,
                      "orr": 0.0, "bnp": 0.0, "rc": -1.0}, "тест")
    assert lot.price is None
    assert lot.mileage is None
    assert lot.buy_now_price is None
    assert lot.repair_cost is None


def test_auction_date_parsed_as_utc():
    raw = next((r for r in RAW_LOTS if r.get("ad")), None)
    if raw is None:
        return  # у лотов со статусом Future даты торгов нет — это нормально
    lot = _parse_lot(raw, "тест")
    assert lot.auction_date.tzinfo == datetime.timezone.utc


# ── Сборка запроса ────────────────────────────────────────────────────────────

def test_year_and_make_filters():
    flt = _build_filter(_filter(brand="CHEVROLET", year_from=2015, year_to=2020))
    assert flt["MAKE"] == ['lot_make_desc:"CHEVROLET"']
    assert flt["YEAR"] == ["lot_year:[2015 TO 2020]"]


def test_open_ended_range():
    flt = _build_filter(_filter(brand="FORD", year_from=2018))
    assert flt["YEAR"] == ["lot_year:[2018 TO *]"]


def test_mileage_converted_for_shared_filter():
    """Общий фильтр задаёт километры, одометр Copart — в милях."""
    flt = _build_filter(_filter(brand="KIA", mileage_to=160934, kind="ru"))
    assert flt["ODM"] == ["odometer_reading_received:[* TO 100000]"]


def test_mileage_kept_for_dedicated_filter():
    """В отдельном фильтре Copart пробег вводится сразу в милях."""
    flt = _build_filter(_filter(brand="KIA", mileage_to=100000, kind="copart"))
    assert flt["ODM"] == ["odometer_reading_received:[* TO 100000]"]


def test_price_bounds_by_filter_kind():
    shared = _filter(price_from=270000, price_to=900000, kind="ru")
    assert _price_bounds_usd(shared) == (3000, 10000)      # пересчёт по курсу 90
    native = _filter(price_from=3000, price_to=10000, kind="copart")
    assert _price_bounds_usd(native) == (3000, 10000)      # доллары как есть


def test_damage_exclusion_is_negated():
    flt = _build_filter(_filter(brand="BMW", damage_exclude=["BN", "WA"]))
    assert flt["PRID"] == [
        "-damage_type_code:(DAMAGECODE_BN OR DAMAGECODE_WA)"
    ]


def test_yard_filter_uses_state_prefix():
    """yard_name:"FL - "* не работает, а yard_name:FL* — работает."""
    flt = _build_filter(_filter(brand="BMW", yards=["FL", "TX"]))
    assert flt["LOC"] == ["yard_name:FL*", "yard_name:TX*"]


def test_run_and_drive_wins_over_buy_now():
    """Обе отметки живут в группе FETI, а внутри группы условия — ИЛИ.
    Поэтому в запрос уходит «на ходу», Buy It Now отбираем у себя."""
    flt = _build_filter(_filter(brand="BMW", run_and_drive=True, buy_now_only=True))
    assert flt["FETI"] == ["lot_condition_code:CERT-D"]


def test_title_groups_are_ored():
    flt = _build_filter(_filter(brand="BMW", title_groups=["C", "S"]))
    assert flt["TITL"] == [
        "title_group_code:TITLEGROUP_C",
        "title_group_code:TITLEGROUP_S",
    ]


# ── Клиентская фильтрация ─────────────────────────────────────────────────────

def test_buy_now_price_used_when_estimate_missing():
    """У лотов «купить сразу» оценки часто нет — цену берём из Buy It Now."""
    lot = _parse_lot({"lotNumberStr": "2", "ld": "TEST", "la": -1.0,
                      "bnp": 4500.0}, "тест")
    assert _matches(lot, _filter(price_to=5000, kind="copart"), False)
    assert not _matches(lot, _filter(price_to=4000, kind="copart"), False)


def test_model_matched_by_title_when_facet_missed():
    lot = _parse_lot({"lotNumberStr": "3", "ld": "2005 TOYOTA LAND CRUISER BASE"}, "тест")
    assert _matches(lot, _filter(model="LAND CRUISER"), True)
    assert not _matches(lot, _filter(model="COROLLA"), True)


# ── Справочники ───────────────────────────────────────────────────────────────

def test_damage_dictionaries_stay_in_sync():
    """DAMAGE_RU и DAMAGE_CODES собираются из одной таблицы — проверяем это."""
    for code, (full, desc, ru) in DAMAGE.items():
        assert DAMAGE_CODES[code] == (full, ru)
        assert DAMAGE_RU[desc] == ru


def test_translations_have_fallback():
    assert damage_ru("FRONT END") == "Перед"
    assert damage_ru("НЕЧТО НЕВЕДОМОЕ")        # незнакомое не роняет, а возвращает
    assert damage_ru(None) == ""
    assert "Salvage" in title_ru("SALVAGE TITLE")
    assert keys_ru("YES") and keys_ru(None) == ""


# ── Справочники из facetFields ────────────────────────────────────────────────

def test_facet_values_parsed_and_sorted():
    """Марки и модели берём из facetFields, самые ходовые — первыми."""
    results = {"facetFields": [{
        "quickPickCode": "MAKE",
        "facetCounts": [
            {"query": 'lot_make_desc:"HONDA"',  "count": 100},
            {"query": 'lot_make_desc:"TOYOTA"', "count": 500},
            {"query": "мусор без двоеточия",     "count": 1},
            {"query": 'lot_make_desc:""',        "count": 0},
        ],
    }]}
    assert _facet_values(results, "MAKE") == [("TOYOTA", 500), ("HONDA", 100)]


def test_facet_values_missing_group():
    assert _facet_values({"facetFields": []}, "MODL") == []
    assert _facet_values({}, "MAKE") == []


def test_fetch_limit_matches_paging():
    """FETCH_LIMIT показывается пользователю в предпросмотре — не должен разъехаться."""
    assert FETCH_LIMIT == PAGE_SIZE * MAX_PAGES == 300


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
