CREATE TABLE IF NOT EXISTS filters (
    id           SERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    name         TEXT NOT NULL,
    -- 'ru' — российские площадки, 'copart' — аукцион (своя семантика полей:
    -- цена в долларах, пробег в милях, города не применяются)
    kind         TEXT DEFAULT 'ru',
    brand        TEXT,
    model        TEXT,
    year_from    INTEGER,
    year_to      INTEGER,
    price_from   INTEGER,
    price_to     INTEGER,
    mileage_from INTEGER,
    mileage_to   INTEGER,
    city         TEXT,
    cities       TEXT[],
    transmission TEXT,
    body_type    TEXT,
    sources      TEXT[] DEFAULT ARRAY['autoru', 'drom'],
    -- Поля, которые использует только источник copart
    auction_date_from DATE,
    auction_date_to   DATE,
    brands         TEXT[],   -- несколько марок в одном фильтре
    models         TEXT[],   -- несколько моделей
    title_groups   TEXT[],   -- C / S / J — тип документа
    damage_exclude TEXT[],   -- коды исключаемых повреждений: BN, WA, BC…
    yards          TEXT[],   -- штаты площадок: FL, TX…
    run_and_drive  BOOLEAN,
    buy_now_only   BOOLEAN,
    is_active    BOOLEAN DEFAULT TRUE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS seen_listings (
    id          SERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT,
    price       INTEGER,
    year        INTEGER,
    mileage     INTEGER,
    city        TEXT,
    transmission TEXT,
    -- Поля аукционов (copart)
    damage_description TEXT,
    auction_date       TIMESTAMPTZ,
    currency           TEXT,
    image_url          TEXT,
    title_group        TEXT,
    has_keys           TEXT,
    run_and_drive      BOOLEAN,
    buy_now_price      INTEGER,
    repair_cost        INTEGER,
    odometer_brand     TEXT,
    vin                TEXT,
    specs              TEXT,
    -- 0 — не напоминали, 1 — отправлено за сутки, 2 — отправлено за час
    auction_notify_stage SMALLINT DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_filters_user_active
    ON filters (user_id, is_active);

CREATE INDEX IF NOT EXISTS idx_seen_listings_source_ext
    ON seen_listings (source, external_id);

CREATE INDEX IF NOT EXISTS idx_seen_listings_created
    ON seen_listings (created_at);

CREATE INDEX IF NOT EXISTS idx_seen_listings_url
    ON seen_listings (url);

CREATE TABLE IF NOT EXISTS favorites (
    id          SERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT,
    price       INTEGER,
    year        INTEGER,
    mileage     INTEGER,
    city        TEXT,
    transmission TEXT,
    filter_name TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (user_id, source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_favorites_user
    ON favorites (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS price_history (
    id          SERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    price       INTEGER NOT NULL,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_price_history_listing
    ON price_history (source, external_id, recorded_at DESC);

-- ── Миграции для уже созданных БД ────────────────────────────────────────────
-- CREATE TABLE IF NOT EXISTS выше не добавляет колонки в существующие таблицы,
-- поэтому дублируем их отдельно. Те же запросы выполняются автоматически
-- при старте приложения (db/repository.py → _apply_migrations).
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS damage_description TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS auction_date       TIMESTAMPTZ;
ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS damage_description TEXT;
ALTER TABLE favorites     ADD COLUMN IF NOT EXISTS auction_date       TIMESTAMPTZ;
ALTER TABLE filters       ADD COLUMN IF NOT EXISTS auction_date_from  DATE;
ALTER TABLE filters       ADD COLUMN IF NOT EXISTS auction_date_to    DATE;

ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS currency        TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS image_url       TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS title_group     TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS has_keys        TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS run_and_drive   BOOLEAN;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS buy_now_price   INTEGER;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS repair_cost     INTEGER;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS odometer_brand  TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS vin             TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS specs           TEXT;
ALTER TABLE seen_listings ADD COLUMN IF NOT EXISTS auction_notify_stage SMALLINT DEFAULT 0;

ALTER TABLE filters ADD COLUMN IF NOT EXISTS kind           TEXT DEFAULT 'ru';
ALTER TABLE filters ADD COLUMN IF NOT EXISTS title_groups   TEXT[];
ALTER TABLE filters ADD COLUMN IF NOT EXISTS damage_exclude TEXT[];
ALTER TABLE filters ADD COLUMN IF NOT EXISTS yards          TEXT[];
ALTER TABLE filters ADD COLUMN IF NOT EXISTS run_and_drive  BOOLEAN;
ALTER TABLE filters ADD COLUMN IF NOT EXISTS buy_now_only   BOOLEAN;

CREATE INDEX IF NOT EXISTS idx_seen_listings_auction_date
    ON seen_listings (auction_date);

-- Для выборки лотов, которым пора напомнить о торгах
CREATE INDEX IF NOT EXISTS idx_seen_listings_auction_notify
    ON seen_listings (source, auction_date, auction_notify_stage);

-- Здоровье источников: сколько обходов подряд площадка вернула пусто.
-- Нужно, чтобы смена формата у площадки не проходила незамеченной.
CREATE TABLE IF NOT EXISTS source_health (
    source     TEXT PRIMARY KEY,
    zero_runs  INTEGER DEFAULT 0,
    last_ok    TIMESTAMPTZ,
    alerted    BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS notification_settings (
    user_id         BIGINT PRIMARY KEY,
    price_threshold INTEGER DEFAULT NULL,
    quiet_from      INTEGER DEFAULT 23,
    quiet_to        INTEGER DEFAULT 8,
    notify_price_drop BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
