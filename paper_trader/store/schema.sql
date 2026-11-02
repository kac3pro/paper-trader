-- Warehouse schema. Every table is derived from the raw archive by
-- paper_trader.store.db.build and rebuilt from scratch each time; never edit rows by hand.
-- All timestamps are UTC (TIMESTAMPTZ). target_date / local_date are Warsaw local dates.

-- One row per daily event, from its most recent Gamma response.
CREATE TABLE events (
    event_id          VARCHAR PRIMARY KEY,
    slug              VARCHAR NOT NULL,
    target_date       DATE NOT NULL,
    title             VARCHAR,
    neg_risk          BOOLEAN,
    start_date        TIMESTAMPTZ,
    end_date          TIMESTAMPTZ,
    resolution_source VARCHAR,       -- the URL Polymarket resolves from
    description_hash  VARCHAR,       -- changes if the rules text is ever edited
    closed            BOOLEAN NOT NULL,
    closed_time       TIMESTAMPTZ,      -- when Polymarket closed it (= resolution, normally)
    winner_market_id  VARCHAR,          -- official winning bracket; NULL until resolved
    first_seen_at     TIMESTAMPTZ NOT NULL,
    last_seen_at      TIMESTAMPTZ NOT NULL
);

-- One row per bracket (YES/NO market) of an event.
CREATE TABLE brackets (
    market_id    VARCHAR PRIMARY KEY,
    condition_id VARCHAR NOT NULL,
    event_id     VARCHAR NOT NULL,
    label        VARCHAR NOT NULL,   -- e.g. '11°C or higher'
    lo_c         INTEGER,            -- inclusive, NULL = open below
    hi_c         INTEGER,            -- inclusive, NULL = open above
    yes_token_id VARCHAR NOT NULL,
    no_token_id  VARCHAR NOT NULL
);

-- Gamma's view of each bracket, every time we fetched the event (~15 min).
CREATE TABLE market_status (
    fetched_at       TIMESTAMPTZ NOT NULL,
    market_id        VARCHAR NOT NULL,
    active           BOOLEAN,
    closed           BOOLEAN,
    accepting_orders BOOLEAN,
    tick_size        DECIMAL(6, 4),
    min_size         DECIMAL(18, 6),
    best_bid         DECIMAL(6, 4),
    best_ask         DECIMAL(6, 4),
    last_trade_price DECIMAL(6, 4),
    outcome_prices   VARCHAR,        -- raw JSON string, e.g. '["0.575", "0.425"]' (Yes, No)
    volume           DECIMAL(18, 6),
    liquidity        DECIMAL(18, 6),
    fee_rate         DECIMAL(8, 6),
    fee_exponent     DECIMAL(8, 4),
    fee_taker_only   BOOLEAN,
    fee_rebate_rate  DECIMAL(8, 6)
);

-- Order book of each bracket's YES token, every poll (~2 min). NO = mirror image.
CREATE TABLE book_snapshots (
    fetched_at       TIMESTAMPTZ NOT NULL,
    server_ts        TIMESTAMPTZ,       -- CLOB's own timestamp for the book
    token_id         VARCHAR NOT NULL,
    condition_id     VARCHAR,
    book_hash        VARCHAR,
    bids             STRUCT(price DECIMAL(6, 4), size DECIMAL(18, 6))[],  -- best (highest) first
    asks             STRUCT(price DECIMAL(6, 4), size DECIMAL(18, 6))[],  -- best (lowest) first
    best_bid         DECIMAL(6, 4),
    best_ask         DECIMAL(6, 4),
    mid              DECIMAL(7, 5),
    tick_size        DECIMAL(6, 4),
    min_order_size   DECIMAL(18, 6),
    last_trade_price DECIMAL(6, 4)
);

-- One row per distinct METAR report version (a correction is a new version).
CREATE TABLE metar_obs (
    station       VARCHAR NOT NULL,
    obs_time      TIMESTAMPTZ NOT NULL,
    local_date    DATE NOT NULL,
    report_type   VARCHAR,           -- 'METAR', 'SPECI', or NULL if unlabelled (counts as routine)
    is_correction BOOLEAN NOT NULL,
    temp_body_c   INTEGER,           -- whole degrees from the body, e.g. '08/03' -> 8
    temp_tgroup_c DECIMAL(4, 1),     -- tenths from the T-group remark; EPWA doesn't send it
    raw           VARCHAR NOT NULL,
    raw_hash      VARCHAR PRIMARY KEY,
    first_seen_at TIMESTAMPTZ NOT NULL
);


-- Best bid/ask per bracket per poll, with the bracket and date attached.
CREATE VIEW book_top AS
SELECT
    s.fetched_at,
    e.target_date,
    b.label,
    b.lo_c,
    b.hi_c,
    s.best_bid,
    s.best_ask,
    s.mid,
    s.best_ask - s.best_bid AS spread,
    b.market_id,
    e.event_id
FROM book_snapshots s
JOIN brackets b ON b.yes_token_id = s.token_id
JOIN events e ON e.event_id = b.event_id;

-- Market-implied probability per bracket per poll: mids normalized to sum to 1.
-- Brackets without a two-sided book (no mid) are left out of that poll.
CREATE VIEW market_implied AS
SELECT
    fetched_at,
    target_date,
    label,
    lo_c,
    hi_c,
    mid,
    mid / sum(mid) OVER (PARTITION BY target_date, fetched_at) AS implied_prob
FROM book_top
WHERE mid IS NOT NULL;

-- Polymarket's official result per event, once resolved.
CREATE VIEW official_results AS
SELECT
    e.target_date,
    e.event_id,
    b.label AS winner_label,
    b.lo_c,
    b.hi_c,
    e.closed_time
FROM events e
JOIN brackets b ON b.market_id = e.winner_market_id;

-- PRELIMINARY daily max from METARs, for viewing only. The real resolution function
-- (with Polymarket's exact rules and cut-off) is a later step.
-- Routine reports only (SPECI excluded); for each observation time, the latest version.
CREATE VIEW metar_daily AS
WITH latest AS (
    SELECT *
    FROM metar_obs
    WHERE report_type IS DISTINCT FROM 'SPECI'
    QUALIFY row_number() OVER (PARTITION BY station, obs_time ORDER BY first_seen_at DESC) = 1
)
SELECT
    station,
    local_date,
    max(coalesce(floor(temp_tgroup_c + 0.5)::INTEGER, temp_body_c)) AS max_temp_c,
    count(*) AS n_reports,
    min(obs_time) AS first_obs,
    max(obs_time) AS last_obs
FROM latest
GROUP BY station, local_date;
