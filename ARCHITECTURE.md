# Polymarket Paper Trader: Architecture

Status: design only, no code yet.
Scope (v1): one market series, the daily **"Highest temperature in Warsaw on <date>?"**. Strategy: **day-ahead trading driven by an existing EMOS model**.
Deployment: a single always-on VPS. Storage: **DuckDB**.

---

## 1. Goals

1. **Paper-trade** the Warsaw max-temperature market with realistic fills and fees, and record every profit and loss.
2. **Collect data** (order books, forecasts, observations, results) so that later models and strategies can be **backtested on the same data the live trader saw**.
3. **Functional design:** a pure core (`state, event -> new state`) with side effects only at the edges. Live paper trading and backtesting share the same engine and differ only in where the events come from.

---

## 2. Market facts that drive the design

Checked against the live Gamma API on 2026-11-01, using event `highest-temperature-in-warsaw-on-november-2-2026`.

| Fact | Consequence |
|---|---|
| One **event per day** with about 11 **bracket markets** (`"1°C or below"`, `2°C` … `10°C`, `"11°C or higher"`), each a separate YES/NO market with its own CLOB token IDs. | Brackets change daily (centred on the forecast). Read and parse them from `groupItemTitle` each day; never hardcode them. |
| `negRisk: true`: exactly one bracket resolves YES. | Model output is a probability distribution over the brackets that sums to 1. Market prices won't sum exactly to 1, so normalize them when computing market-implied probabilities. |
| Resolves from **EPWA (Warsaw Chopin) routine METARs** via aviationweather.gov. SPECI reports are excluded. The hourly T-group remark (tenths of °C) is used if present, otherwise the whole-°C body temperature. Rounded half-up to a whole °C. A report counts if its timestamp falls on the date **in Warsaw local time** (DST applies). | Implement the resolution rule as a pure function and reconcile it daily against the official result. |
| **EPWA METARs carry no T-group**: the body shows e.g. `09/06`. Reports are issued every 30 minutes. | The real target is the **max of half-hourly whole-degree readings**. This is not true Tmax, which can be higher. The EMOS target must match it (see §11). |
| If no data is published by the deadline, the event resolves to the **lowest bracket**. | Edge case for the resolution function. |
| Fees: `feeSchedule {rate: 0.05, exponent: 1, takerOnly: true, rebateRate: 0.25}`, `feeType: weather_fees`. | Fees are a pure function of the market's fee schedule, price and size. **Check the exact formula against Polymarket's docs before implementing.** |
| `orderMinSize: 5`. Tick size is `0.01` or `0.001` (smaller ticks near 0 and 1). | The execution simulator must respect both. Store tick size per snapshot, because it changes with price. |
| Liquidity is thin: about $8.5k volume and $62k listed liquidity per event. | Fills walk the order book. Cap size to the depth visible in the book. Assume our paper orders don't move the market. |
| An event opens about **2 days early** (the Nov 2 event was created Oct 31, 05:57Z). | The D+1 market is already open on day D, so day-ahead trading is possible. |
| Three clocks are involved: METAR times are UTC, the market day is Europe/Warsaw (CET/CEST), and Polymarket deadlines are US Eastern. | Store everything as UTC `TIMESTAMPTZ`; `target_date` is a local Warsaw `DATE`. Convert only at the edges. |

---

## 3. High-level architecture

```
            ┌──────────────── IMPURE SHELL ─────────────────┐
 Gamma API ─┤                                               │
 CLOB books ┤  adapters (HTTP only) ──► raw archive (JSONL.gz, append-only)
 METAR ─────┤          │                                    │
 Open-Meteo ┘          ▼                                    │
                  normalizers (pure: raw JSON -> rows) ─────► DuckDB tables
                                                                 │
                          ┌──────────────────────────────────────┤
                          ▼                                      ▼
                   predictor job                          trader job
              (features -> EMOS -> dist)         (reads latest prediction
               writes `predictions`               + books, runs strategy)
                                                                 │
            ┌──────────────── PURE CORE ─────────────────────────▼─┐
            │ snapshot -> strategy -> orders -> fill simulator     │
            │ -> portfolio update -> settlement                    │
            │ run = reduce(step, events, initial_state)            │
            └──────────────────────────┬───────────────────────────┘
                                       ▼
                    runs / decisions / orders / fills / settlements
                                       ▼
                                evaluation reports
```

**Key rule:** the trader **never calls an external API**. It reads only from DuckDB, the same data a backtest replays. A live run is then just a backtest whose events arrive in real time.

---

## 4. Processes on the VPS

### DuckDB constraint
A DuckDB database file allows **either one read-write process, or many read-only processes, but not both at once**. Having a collector, predictor and trader as separate processes all writing to the same `.duckdb` file won't work.

### Choice: one long-running daemon that owns the database
- A single process (`paper-trader daemon`, run as a systemd service with `Restart=always`) holds the only DuckDB connection.
- An internal scheduler runs **jobs**: `collect_markets`, `collect_books`, `collect_metar`, `collect_forecasts`, `predict`, `trade`, `settle`, `snapshot_db`.
- Each job is a thin impure wrapper around pure functions. Each job is **isolated**: if one raises an exception, it is logged and the others keep running. A strategy bug must never stop data collection.
- Jobs run one at a time, so there are no write races and the order of events is deterministic.

### Analysis and backtests: work on a snapshot, never the live file
- Every night the daemon writes a consistent copy (`EXPORT DATABASE` to Parquet, or `ATTACH 'snapshot.duckdb'` + `COPY FROM DATABASE`). Copying the live file with `cp` while it's open is not safe.
- `rsync` the snapshot off the VPS to your laptop. Run backtests and notebooks there against the snapshot. This doubles as an off-site backup.

### Alternative, if separate processes are ever needed
Writers emit Parquet files (written to a temp file, then atomically renamed), and DuckDB queries them as a read-only "lake" (`read_parquet('data/books/date=*/*.parquet')`). This adds more moving parts (compacting many small files), so it's not used in v1.

---

## 5. Data sources and collection schedule

| Source | What | How often | Backfillable? |
|---|---|---|---|
| Gamma API (`/events`, `/public-search`) | Event and bracket discovery, labels, token IDs, fee schedule, `acceptingOrders`, `closed`, final `outcomePrices` | every 15 min; after the target date, until resolved | partly |
| CLOB `/books` (batch) | Full order book for every bracket token | every 2 min while the event is open (could be every 1 min in the trading window) | **No. Must be recorded from now on.** |
| CLOB / data-api trades | Executed trades (for future maker-fill simulation) | every 15 min | partly |
| aviationweather.gov METAR | EPWA reports | every 10 min | yes (Iowa Environmental Mesonet archive) |
| Open-Meteo ensemble API | Hourly 2m temperature for every ensemble member, for the model(s) the EMOS was trained on | poll hourly; store only when a **new run** appears | yes (Previous Runs / Historical Forecast APIs) |
| Open-Meteo model metadata (`.../static/meta.json`) | `last_run_initialisation_time`, `last_run_availability_time` | with each forecast poll | no; record it |

**Start the collector before anything else.** Order book history is the only data that can't be reconstructed later.

---

## 6. Storage

### 6.1 Raw archive (outside DuckDB)
- `data/raw/<source>/<YYYY-MM-DD>/<HH>.jsonl.gz`, one line per HTTP response: `{fetched_at, url, params, status, body}`.
- Append-only and never edited. If a normalizer has a bug, fix it and re-derive the tables from this archive.
- Cheap enough to keep forever (compressed text, a few MB per day).

### 6.2 DuckDB conventions
- All timestamps are `TIMESTAMPTZ` in UTC. `target_date` is a `DATE` in Europe/Warsaw.
- Prices are `DECIMAL(6,4)`; sizes and money are `DECIMAL(18,6)`. No floats in money paths.
- Every collected row has **`fetched_at`** (when we learned it). Forecasts also have **`available_at`**.
- **Point-in-time rule:** a decision at time `t` may only use rows with `fetched_at <= t` (forecasts: `available_at <= t`). Every backtest query goes through this filter.
- Collected tables are append-only. Status changes are stored as new snapshot rows, not updates.

### 6.3 Tables

#### A. Market reference

**`events`**: one row per daily event.

| column | type | notes |
|---|---|---|
| event_id | VARCHAR PK | Gamma id |
| slug | VARCHAR | |
| target_date | DATE | Warsaw local |
| title | VARCHAR | |
| neg_risk | BOOLEAN | |
| neg_risk_market_id | VARCHAR | |
| start_date, end_date | TIMESTAMPTZ | |
| resolution_source | VARCHAR | URL |
| description_hash | VARCHAR | detects changes to the rules text |
| first_seen_at | TIMESTAMPTZ | |

**`brackets`**: one row per bracket market.

| column | type | notes |
|---|---|---|
| market_id | VARCHAR PK | |
| condition_id | VARCHAR | |
| event_id | VARCHAR FK | |
| label | VARCHAR | raw `groupItemTitle` |
| lo_c, hi_c | INTEGER NULL | parsed: `"1°C or below"` → (NULL, 1); `"8°C"` → (8, 8); `"11°C or higher"` → (11, NULL) |
| yes_token_id, no_token_id | VARCHAR | |
| first_seen_at | TIMESTAMPTZ | |

**`market_status`**: snapshots of slowly changing fields, appended every 15 minutes.

| column | type |
|---|---|
| fetched_at | TIMESTAMPTZ |
| market_id | VARCHAR |
| active, closed, accepting_orders | BOOLEAN |
| tick_size, min_size | DECIMAL |
| fee_rate, fee_exponent, fee_taker_only, fee_rebate_rate | DECIMAL / BOOLEAN |
| outcome_prices | DECIMAL[] |
| volume, liquidity | DECIMAL |
| uma_status | VARCHAR |

#### B. Order books (the most valuable table)

**`book_snapshots`**: one row per token per poll. Bid and ask ladders are stored as nested lists (DuckDB handles these natively).

| column | type | notes |
|---|---|---|
| fetched_at | TIMESTAMPTZ | our clock |
| server_ts | TIMESTAMPTZ | CLOB's timestamp |
| market_id | VARCHAR | |
| token_id | VARCHAR | |
| outcome | VARCHAR | `YES` / `NO` |
| book_hash | VARCHAR | CLOB hash; shows whether the book changed between polls |
| bids | STRUCT(price DECIMAL(6,4), size DECIMAL(18,6))[] | best first |
| asks | STRUCT(price DECIMAL(6,4), size DECIMAL(18,6))[] | best first |
| best_bid, best_ask, mid | DECIMAL(6,4) | stored, not computed, for fast queries |
| tick_size | DECIMAL(6,4) | |

Volume: about 22 tokens × 720 polls/day ≈ 16k rows/day, i.e. tens of MB per month. Fine.
Note: in Polymarket's CLOB, the NO book should mirror the YES book (buying NO at p is equivalent to selling YES at 1−p). Store both at first; once the mirroring is confirmed, YES alone is enough.

**`trades`**: executed trades, kept for future maker-fill modelling.

| column | type |
|---|---|
| trade_id | VARCHAR PK (tx hash + log index) |
| ts | TIMESTAMPTZ |
| market_id, token_id | VARCHAR |
| side | VARCHAR |
| price, size | DECIMAL |
| fetched_at | TIMESTAMPTZ |

#### C. Weather

**`metar_obs`**: every distinct report version we have seen. Corrections count as separate versions.

| column | type | notes |
|---|---|---|
| station | VARCHAR | `EPWA` |
| obs_time | TIMESTAMPTZ | from the report's `DDHHMMZ` |
| report_type | VARCHAR | `METAR` / `SPECI` |
| is_correction | BOOLEAN | `COR` |
| raw | VARCHAR | full text |
| raw_hash | VARCHAR | PK is (station, obs_time, raw_hash) |
| temp_body_c | INTEGER NULL | `09/06` → 9, `M02/..` → −2 |
| temp_tgroup_c | DECIMAL(4,1) NULL | from `Tsnnnsnnn`; currently always NULL for EPWA |
| local_date | DATE | obs_time converted to Europe/Warsaw |
| first_seen_at | TIMESTAMPTZ | |

**`forecast_runs`**: one row per (provider model, initialization time).

| column | type | notes |
|---|---|---|
| model | VARCHAR | e.g. `ecmwf_ifs025_ensemble`. Must match what the EMOS was trained on |
| run_time | TIMESTAMPTZ | initialization time |
| available_at | TIMESTAMPTZ | from `meta.json`, or the first `fetched_at` that showed this run |
| n_members | INTEGER | |
| fetched_at | TIMESTAMPTZ | |

**`forecast_values`**: long format.

| column | type |
|---|---|
| model | VARCHAR |
| run_time | TIMESTAMPTZ |
| member | SMALLINT (0 = control) |
| valid_time | TIMESTAMPTZ |
| variable | VARCHAR (`temperature_2m`) |
| value | REAL (physical quantity, not money, so a float is fine) |

Volume: 51 members × about 72 h × up to 4 runs/day ≈ 15k rows/day per model.

**`forecast_features`**: derived (a view or a materialized table). The exact inputs the EMOS consumes, one row per (model, run_time, target_date).

| column | type | notes |
|---|---|---|
| model, run_time, target_date | | PK |
| available_at | TIMESTAMPTZ | inherited from the run |
| lead_days | INTEGER | |
| ens_mean_tmax, ens_sd_tmax | REAL | per-member max over the Warsaw-local day, then mean/sd across members. **Must use the same definition as in training** |
| member_tmax | REAL[] | kept for other models later |

#### D. Resolution

**`resolutions`**

| column | type | notes |
|---|---|---|
| event_id | VARCHAR PK | |
| target_date | DATE | |
| computed_max_c | INTEGER | from `metar_obs` via the pure resolution function |
| computed_market_id | VARCHAR | winning bracket per our rule |
| official_market_id | VARCHAR NULL | from Gamma once resolved |
| official_resolved_at | TIMESTAMPTZ NULL | |
| agrees | BOOLEAN | **alert if false** |

`computed_max_c` is **the training and scoring target** for models.

#### E. Models, predictions, trading (shared by live and backtest)

**`model_versions`**

| column | type | notes |
|---|---|---|
| model_id | VARCHAR | `emos_v1` |
| version | VARCHAR | content hash of the coefficients |
| family | VARCHAR | `normal`, `truncated_normal`, `logistic` … |
| params | JSON | EMOS coefficients (a, b, c, d …), per season/lead if applicable |
| feature_spec | JSON | which forecast model and features it expects |
| target_spec | VARCHAR | e.g. `epwa_metar_max_whole_c` |
| train_window | VARCHAR | |
| git_sha | VARCHAR | |
| created_at | TIMESTAMPTZ | |

**`predictions`**: one row per model output (see §7).

| column | type | notes |
|---|---|---|
| prediction_id | VARCHAR PK | deterministic hash of (model version, event, input run) |
| run_id | VARCHAR | live run or backtest run |
| model_id, version | VARCHAR | |
| event_id, target_date | | |
| issued_at | TIMESTAMPTZ | wall-clock time (live) or simulated time (backtest) |
| input_model, input_run_time | | which forecast run fed it |
| features | JSON | e.g. `{ens_mean: 8.4, ens_sd: 1.1}` |
| dist_family | VARCHAR | |
| dist_params | JSON | e.g. `{mu: 8.7, sigma: 1.3}` |

**`prediction_brackets`**

| column | type |
|---|---|
| prediction_id | VARCHAR |
| market_id | VARCHAR |
| prob | DOUBLE |

**`runs`**

| column | type | notes |
|---|---|---|
| run_id | VARCHAR PK | |
| mode | VARCHAR | `live` / `backtest` |
| strategy_id | VARCHAR | |
| strategy_params | JSON | thresholds, Kelly fraction, caps |
| model_id, model_version | VARCHAR | |
| data_from, data_to | DATE | for backtests |
| git_sha, config_hash | VARCHAR | |
| started_at | TIMESTAMPTZ | |
| bankroll_start | DECIMAL | |

**`decisions`**: one row per bracket per evaluation, **including "no trade"**.

| column | type |
|---|---|
| decision_id | VARCHAR PK |
| run_id, prediction_id | VARCHAR |
| t | TIMESTAMPTZ |
| market_id | VARCHAR |
| model_prob | DOUBLE |
| market_bid, market_ask, market_implied_prob | DECIMAL / DOUBLE |
| edge_yes, edge_no | DOUBLE (after fees) |
| action | VARCHAR (`buy_yes`, `buy_no`, `none`) |
| reason | VARCHAR |

**`orders`**: `order_id, decision_id, run_id, t, token_id, side, limit_price, size, order_type ('taker')`

**`fills`**: `fill_id, order_id, run_id, t, token_id, side, size, avg_price, fee, levels JSON (price/size per book level consumed), book_fetched_at` (which snapshot was used).

**`settlements`**: `run_id, event_id, market_id, token_id, qty, payout_per_share (0/1), cost_basis, fees, pnl, settled_at`.

**Views:** `positions` (net quantity per run and token), `pnl_daily`, `pnl_by_run`, `scores` (Brier score and log-loss per prediction, joined to `resolutions`).

---

## 7. How predictions get to the trader

**Answer: predictions are rows in DuckDB (`predictions` + `prediction_brackets`), written by a separate predictor job and read by the trader job. Not a file, and not a direct function call from the trader.**

Why:

1. **Separation:** the model and the strategy evaluate independently. You can score predictions (Brier score, log-loss, calibration) without trading. You can also run *any* strategy over *any* stored prediction set.
2. **Point-in-time audit:** every prediction records `issued_at` and the exact forecast run it used. A backtest takes the latest prediction with `issued_at <= t`, the same rule the live trader follows.
3. **Backtesting new models without changing the trader:** a model backtest is "run the predictor over history (point-in-time features) → write predictions under a new `run_id`/`version`". The strategy backtest then consumes them unchanged. Comparing EMOS v1 against a new model means comparing two prediction sets.
4. **Files would recreate a database badly:** you'd need naming conventions, atomic writes, versioning and joins against books and results, all of which DuckDB already provides. And the daemon already holds the only write connection.
5. **Brackets change daily**, so store **both** the distribution parameters (`mu`, `sigma`), which are model-native and bracket-independent, **and** the bracket probabilities. The probabilities are what the strategy uses and what you audit.

Bracket probabilities from a continuous EMOS distribution with CDF *F*:
the value resolves to whole degree *k* if `k − 0.5 ≤ x < k + 0.5`, so
`P(lo..hi) = F(hi + 0.5) − F(lo − 0.5)`, with `lo = −∞` for "or below" and `hi = +∞` for "or higher".
The probabilities sum to 1 across brackets by construction. This mapping is a pure function in `core/`.

The model itself is a pure function `features → distribution`. EMOS coefficients live in `model_versions.params`, not in code, so a retrained model is a new version rather than a code change.

---

## 8. The pure core

All types are immutable (frozen dataclasses / NamedTuples). All functions are pure and deterministic.

| Function | Signature (described) |
|---|---|
| `parse_bracket` | label → (lo, hi) |
| `metar_temp` | raw report → °C or None (T-group first, then body) |
| `resolve` | (list of reports, target_date, brackets) → winning bracket |
| `features` | forecast values for a run → `forecast_features` row |
| `emos_predict` | (params, features) → distribution |
| `bracket_probs` | (distribution, brackets) → probability per bracket |
| `market_implied` | books → normalized probability per bracket |
| `fee` | (fee schedule, price, size, is_taker) → fee |
| `simulate_fill` | (order, book snapshot, fee schedule) → fill or rejection (walks levels, respects tick size and minimum size) |
| `strategy` | (bracket probs, books, portfolio, params) → orders + decision records |
| `apply_fill` | (portfolio, fill) → portfolio |
| `settle` | (portfolio, resolution) → (portfolio, settlement records) |
| `step` | (state, event) → (state, records) |
| `run` | `reduce(step, events, initial_state)` |

The impure shell only (a) builds the event stream, live or replayed from DuckDB, and (b) writes the returned records to DuckDB.

---

## 9. Day-ahead daily timeline (live)

Times are approximate. Driven by forecast availability, not the wall clock.

1. **Collect continuously:** books every 2 min, METAR every 10 min, Gamma every 15 min, forecast metadata hourly.
2. **New forecast run available** (`forecast_runs.available_at`) for the model the EMOS uses → compute `forecast_features` for target **D+1**.
3. **`predict` job:** EMOS → `predictions` + `prediction_brackets` for the D+1 event.
4. **`trade` job:** triggered by a new prediction. Loads the latest D+1 books (each must be no more than N minutes old, otherwise skip and log), runs the strategy, and simulates taker fills against that snapshot. Writes `decisions`, `orders`, `fills`.
   - v1 policy: trade **once per new forecast run** (e.g. after the 00Z and 12Z runs), with a cap on total exposure per event. Positions are **held to resolution**: no intraday exits in v1.
5. **Day D+1 ends:** wait for the first METAR of D+2, then compute `resolutions.computed_*`.
6. **Official result appears in Gamma** → fill in `official_*`, check `agrees`, run `settle` → `settlements`.
7. **Nightly:** consistent snapshot → rsync off the VPS.

---

## 10. Evaluation

With one event per day, P&L stays noisy for months. Evaluate on two levels:

**Model quality (needs no prices; can use years of backfilled data):**
- Brier score and log-loss of the bracket distribution against `resolutions`.
- **The same scores for the market-implied distribution at the same time.** "Beats the market" means a lower log-loss than the market.
- Calibration (reliability diagram), PIT histogram for the EMOS distribution, scores by lead time.

**Strategy quality (needs recorded books):**
- P&L after fees, ROI per dollar risked, hit rate, max drawdown, P&L per run.
- **Closing-line value:** our entry price versus the market's last mid before trading closes. Measures edge faster than P&L does.
- Slippage: average fill price versus the mid at decision time.

Baselines to report next to EMOS: climatology, raw ensemble frequencies (no calibration), and the market itself.

---

## 11. Backtesting

1. **Model backtest (available now):** backfill Open-Meteo previous runs plus IEM METARs for EPWA → `forecast_features` and `resolutions` for past years → generate predictions point-in-time → score them. No trading.
2. **Strategy backtest (grows over time):** replay `book_snapshots` together with stored or regenerated predictions through `run(...)` with `mode='backtest'`. Results go into the same tables under a new `run_id`.
3. **Avoid overfitting:** decide a held-out period in advance. Never tune strategy parameters on the days used to report results.

---

## 12. Open questions about the EMOS model

These need answers before going live. They decide whether live features will match training.

1. **Inputs:** which ensemble (ECMWF IFS ENS? which resolution? from which provider?), which run (00Z/12Z), which lead time? Live features must be computed **exactly** as they were in training.
2. **Target:** was it trained on the **EPWA METAR max (whole degrees, half-hourly)**, or on SYNOP/IMGW Tmax, or ERA5? If not on the METAR max, expect a systematic warm bias against what the market settles on. Re-fit or add a correction.
3. **Day definition:** was Tmax computed over the Warsaw local day, or 00–24 UTC (or 06–18 UTC SYNOP windows)?
4. **Distribution family and parameterization:** Normal or logistic, and how σ depends on ensemble spread. Are the coefficients seasonal or fit over a rolling window?
5. **Storage format:** where do the coefficients live today? They will be loaded into `model_versions.params`.

---

## 13. Pitfalls

- **Look-ahead:** forecast `run_time` is not `available_at`. A 00Z run becomes usable hours later.
- **Stale books:** never fill against a snapshot older than N minutes.
- **Fees:** applied to every taker fill. Many apparent edges disappear after fees near p = 0.5.
- **Liquidity:** cap order size to a fraction of the visible depth, and cap per-trade and per-event exposure.
- **DST:** the local day shifts against UTC twice a year, which changes which METARs count.
- **Rule drift:** keep `description_hash` on events and alert if the resolution text changes.
- **Resolution mismatch:** alert on `resolutions.agrees = false`. It means either our rule or the target definition is wrong.

---

## 14. Code layout

```
paper_trader/
  domain/       immutable types, bracket parsing, time zone helpers
  core/         resolution, features, bracket_probs, fees, execution, portfolio, engine   (pure)
  models/       emos, climatology, market_implied                                        (pure)
  strategies/   edge_threshold (+ fractional Kelly sizing)                               (pure)
  adapters/     gamma, clob, metar, openmeteo                                            (HTTP only)
  store/        raw archive writer, normalizers, DuckDB schema/migrations, point-in-time queries
  apps/         daemon (scheduler + jobs), backfill, backtest, report
tests/          property tests for the pure core (fills, settlement, resolution rule)
```

---

## 15. Build order

1. **Collector and raw archive**, plus the DuckDB market and weather tables. Deploy to the VPS immediately so data collection starts.
2. **Resolution function**, reconciled against official results for a few days.
3. **Forecast features and EMOS predictor**, plus backfill. Score past predictions against past results.
4. **Fees, fill simulator and portfolio**, with tests.
5. **Trade and settle jobs** → live paper trading.
6. **Reports**, then strategy backtests once enough book history exists.
