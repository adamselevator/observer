# Changelog

## 2026-02-19

### Added: Analysis tooling (EDA + backtest pipeline)

- **Data loader** (`analysis/data_loader.py`) — Loads snapshot CSVs and interval JSONLs into pandas DataFrames. Joins summary + resolution records, adds formula-derived features (delta, window fraction, fees).
- **Backtest engine** (`analysis/backtest.py`) — Replays the sigmoid confidence formula second-by-second against historical snapshots. Simulates entries, flips, and settlement. Computes PnL after taker fees. Supports parameter grid search and gate configuration.
- **EDA notebook** (`analysis/eda.ipynb`) — Data quality, delta distributions, realized vol, token pricing evolution, fee kill zone, book depth/spread, cross-asset correlation, formula preview with calibration curve.
- **Backtest results notebook** (`analysis/backtest_results.ipynb`) — Default param results, per-asset breakdown, PnL analysis, parameter sensitivity grid search, gate sensitivity, key takeaways.
- **Analysis dependencies** (`requirements-analysis.txt`) — pandas, numpy, matplotlib, scipy, jupyter. Installed into `.venv/`.

**Files**: `analysis/__init__.py`, `analysis/data_loader.py`, `analysis/backtest.py`, `analysis/eda.ipynb`, `analysis/backtest_results.ipynb`, `requirements-analysis.txt`

### Changed: 5-minute markets for all assets

- **Added ETH, SOL, XRP to 5m timeframe** — Polymarket now offers 5-minute markets for all four assets (previously BTC only). Updated `available_timeframes` from `["15m"]` to `["5m", "15m"]`.

**Files**: `config.py`

### Fixed: CLOB order book data (bid/ask now reflects real market prices)

- **Handle `price_change` events** — The CLOB WebSocket sends ~75% of messages as `price_change` (incremental level updates) which were being silently ignored. Only `book` (full snapshot) events were processed. Now both are handled.
- **Fix `price_change` message parsing** — The `price_change` event contains a `price_changes` array with per-token changes, not top-level fields. Parser now iterates the array correctly.
- **Fix book sort order** — Polymarket sends bids sorted ascending (lowest first) but `best_bid` read `bids[0]`, always returning 0.01. Bids are now sorted descending after parsing so `bids[0]` is the highest (best) bid.
- **Add incremental book merge** — `TokenBook` now supports `apply_level()` for merging single-level changes into the running order book, with sorted insert and size-0 removal.
- **Reset books on token ID change** — `AssetState.set_token_ids()` clears stale book data when tokens rotate between intervals.

**Files**: `connections/clob_ws.py`, `state/market_state.py`, `main.py`

### Fixed: Gamma market discovery (tag-based → slug-based)

- **Replaced tag-based API queries** — `GET /events?tag=crypto&tag=up-or-down` returned no results for up-down markets. Now constructs expected slugs directly (`{asset}-updown-{tf}-{ts}`) and queries `GET /events?slug=...`.
- **Fixed `clobTokenIds` JSON parsing** — API returns JSON-encoded strings (`"[\"123...\"]"`), not native lists. Added `json.loads()` deserialization.

**Files**: `connections/gamma_poller.py`

### Fixed: Token ID overwrite (0% book data on first interval)

- **Deferred token ID application** — When GammaPoller discovers the next interval's market early, token IDs were immediately set on `AssetState`, overwriting the current interval's tokens and breaking book routing. Now stores markets in `_discovered_markets` dict and only applies tokens for the current interval.
- **Added `_sync_active_tokens()`** — Runs every second to apply stored tokens to newly-active intervals that lack token IDs.

**Files**: `main.py`

### Fixed: Resolution detection log spam

- **Adjusted price snap threshold** — Changed from `<= 0.01` to `< 0.01`. The value 0.01 is the normal minimum bid, not a resolution indicator.
- **Simplified `_on_price_snap`** — Only resolves pending intervals (not active ones). Active intervals end at their time boundary regardless of price.

**Files**: `connections/clob_ws.py`, `main.py`

### Fixed: CLOB WebSocket crash on list messages

- **Added `isinstance(msg, dict)` guard** — Some CLOB messages are arrays, not dicts. Guard prevents `AttributeError` on `.get()`.

**Files**: `connections/clob_ws.py`

### Fixed: Chainlink "Invalid JSON" on connect

- **Skip empty messages** — First WebSocket message after connect is sometimes empty/whitespace. Added guard before `json.loads()`.

**Files**: `connections/chainlink_ws.py`

### Fixed: SSL certificate errors on macOS

- **Added `certifi` dependency** — macOS Homebrew Python lacks CA certificates. Created shared `SSL_CONTEXT` using certifi's bundle, passed to all connections (WebSocket + aiohttp).

**Files**: `config.py`, `connections/base_ws.py`, `connections/gamma_poller.py`, `connections/binance_backfill.py`, `requirements.txt`

### Changed: Lower reconnect delay

- **`WS_RECONNECT_MAX_S`**: 30 → 10 seconds.

**Files**: `config.py`
