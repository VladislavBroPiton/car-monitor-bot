CREATE TABLE IF NOT EXISTS filters (
    id           SERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL,
    name         TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS notification_settings (
    user_id         BIGINT PRIMARY KEY,
    price_threshold INTEGER DEFAULT NULL,
    quiet_from      INTEGER DEFAULT 23,
    quiet_to        INTEGER DEFAULT 8,
    notify_price_drop BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
