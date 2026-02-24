# Polymarket Observer — Complete Documentation

This document contains everything needed to understand, maintain, extend, and build on top of the Observer. It is written for a human or AI picking up the project cold.

---

## 1. Project Overview & Strategy

### What This Is

The Observer is the data collection component of an autonomous cryptocurrency prediction market trading system for Polymarket. It continuously captures price feeds, order book data, and market metadata for short-duration crypto prediction markets. The data it produces is used for backtesting, parameter calibration, and eventually feeds into an autonomous Trader component.

The full system pipeline is: **Observer → Backtest → Calibrate → Trader → AI Orchestrator**

Only the Observer exists today. Everything downstream depends on the data it collects.

### The Markets

Polymarket runs binary prediction markets on whether a crypto asset's price will be higher or lower at the end of a fixed time window compared to the start. These markets exist in two durations:

- **5-minute markets**: BTC, ETH, SOL, XRP. New market spawns every 5 minutes.
- **15-minute markets**: BTC, ETH, SOL, XRP. New market spawns every 15 minutes.

Each market has two outcome tokens: "Up" and "Down." Tokens pay $1.00 if correct, $0.00 if not. Token prices fluctuate between $0 and $1 based on market consensus. The Up and Down token prices always sum to approximately $1.00.

Resolution is determined by comparing the Chainlink BTC/USD (or ETH/USD, SOL/USD, XRP/USD) price at interval start versus interval end. If end ≥ start, "Up" wins. If end < start, "Down" wins. This is critical: Chainlink is the source of truth for 5-min and 15-min markets, not Binance. Daily markets use Binance, but we don't trade those.

### The Trading Strategy (Not Yet Implemented)

The planned strategy uses a sigmoid-based confidence formula evaluated every second during the trading window:

```
confidence = sigmoid(a × |delta|/vol + b × elapsed/window - offset)
```

Where:
- `delta = (chainlink_price_now - chainlink_open) / chainlink_open` — how far price has moved from interval start
- `vol` — realized volatility (standard deviation of last 20 interval deltas)
- `elapsed` — seconds since trading window opened
- `window` — trading window duration (240s for 5-min, 600s for 15-min)

Starting parameter values (require calibration via backtesting):
- `a = 5` (delta sensitivity)
- `b = 3` (time elapsed weight)
- `offset = 4` (sigmoid centering)
- `F = 0.6` (flip threshold)

Entry rule: `confidence > token_price + fee` (self-calibrating against market price and fee).
Flip rule: `confidence > F` when delta reverses sign after initial entry. One flip max per interval.

Gate conditions filter which intervals to trade:
- Token price between $0.65–$0.85 (avoids fee kill zone near $0.50, low upside above $0.85)
- Must be within trading window (last 4 min of 5-min market, last 10 min of 15-min market)
- Sufficient order book depth at best ask
- Minimum volume/activity threshold

### Fee Structure

Taker fees are enabled on all 5-min and 15-min crypto markets (since January 2026). The fee follows a parabolic curve:

```
fee_per_share = 0.0624 × price × (1 - price)
```

Key fee levels:
- At $0.50 (maximum): ~1.56% per share, ~3.12% of trade value
- At $0.80: ~1.0% per share, ~0.50% of trade value
- At $0.90: ~0.56% per share, ~0.25% of trade value

The fee peaks at 50/50 odds — specifically designed to kill latency arbitrage bots that were exploiting the Binance-to-Chainlink delay. The strategy avoids this zone by trading mid-to-late in the interval when tokens are already priced away from $0.50.

Maker orders (post-only, available since January 2026) pay zero fees and earn daily USDC rebates. The current strategy uses taker-only orders for guaranteed fills, accepting the fee at favorable price levels.

---

## 2. Architecture & Data Flow

### Single-Process Design

The Observer runs as one Python process with three WebSocket connections:

```
┌─────────────────────────────────────────────────┐
│                  Observer Process                │
│                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌────────┐ │
│  │ Chainlink WS │  │  Binance WS  │  │CLOB WS │ │
│  │ (price ticks)│  │(trade ticks) │  │ (books) │ │
│  └──────┬───────┘  └──────┬───────┘  └────┬────┘ │
│         │                 │               │      │
│         ▼                 ▼               ▼      │
│  ┌──────────────────────────────────────────┐    │
│  │            MarketState (in-memory)        │    │
│  │  Per-asset: chainlink tick, binance tick, │    │
│  │             up_book, down_book            │    │
│  └──────────────────┬───────────────────────┘    │
│                     │                            │
│         ┌───────────┼───────────┐                │
│         ▼           ▼           ▼                │
│  ┌────────────┐ ┌─────────┐ ┌──────────┐        │
│  │ Interval   │ │Snapshot │ │ Interval │        │
│  │ Tracker    │ │ Writer  │ │  Writer  │        │
│  │(lifecycle) │ │ (CSV)   │ │ (JSONL)  │        │
│  └────────────┘ └─────────┘ └──────────┘        │
│                                                  │
│  ┌──────────────┐  ┌──────────────────────────┐  │
│  │ Gamma Poller │  │  Binance Backfill (REST) │  │
│  │(market disco)│  │  (gap filling, cold vol) │  │
│  └──────────────┘  └──────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

### Data Flow Per Second

1. **Tick arrives** (Chainlink or Binance WebSocket) → updates `MarketState` in memory
2. **Main loop** fires once per second:
   - Calls `IntervalTracker.check_boundaries()` — detects if any interval ended or started
   - If boundary crossed: closes old interval (captures close price, computes basis, moves to pending resolution queue), starts new interval
   - Calls `SnapshotWriter.write_snapshot()` — reads current `MarketState` and `IntervalTracker`, writes one CSV row per active (asset, timeframe) pair
3. **Book update arrives** (CLOB WebSocket) → updates `MarketState.up_book` or `down_book` for the relevant asset. Full snapshots (`book` events) replace the entire book; incremental level changes (`price_change` events) merge into the existing book via sorted insert/update/remove

### Interval Lifecycle

```
┌─────────┐    ┌──────────┐    ┌───────────┐    ┌──────────┐
│  Start   │───▶│  Active   │───▶│  Pending   │───▶│ Resolved │
│(boundary │    │(receiving │    │(awaiting   │    │(up/down/ │
│ detected)│    │  ticks)   │    │ settlement)│    │unresolved│
└─────────┘    └──────────┘    └───────────┘    └──────────┘
```

**Start**: `IntervalTracker.check_boundaries()` detects that the current time has crossed an interval boundary. Creates new `IntervalRecord`, begins routing ticks to it.

**Active**: Every Chainlink tick updates open (first tick only), high, low, close (always latest), and tick count. Every Binance tick updates binance_open and binance_close for basis computation. The `IntervalRecord` also tracks gap count and max gap duration via tick timestamps.

**Pending Resolution**: When the boundary is crossed again, the interval is finalized (basis computed, delta stored in history for vol), written to JSONL with `"resolution": null`, and moved to the pending queue. The Observer does not block — it immediately starts the next interval.

**Resolution**: Two detection paths run in parallel:
- **Fast path**: CLOB WebSocket detects a token price snapping to $0.99+ or $0.01- (indicates settlement). This fires within seconds.
- **Fallback**: A background loop polls the Gamma API every 15 seconds to check if the market has closed and what the outcome was.
- **Timeout**: After 10 minutes, the interval is marked `"resolution": "unresolved"` and removed from the queue.

Resolution is written as a second JSONL record for the same interval_id:
```json
{"interval_id": "btc-updown-5m-1739836800", "type": "summary", "resolution": null, ...}
{"interval_id": "btc-updown-5m-1739836800", "type": "resolution", "resolution": "up", "resolved_at": 1739837145}
```

### Module Map

```
observer/
├── main.py                       # Entry point, orchestrates all components
├── config.py                     # All constants, CLI parsing, ObserverConfig
├── clock.py                      # Interval timing, boundary detection
├── health.py                     # Connection health event logging
├── requirements.txt              # websockets, aiohttp, certifi
├── connections/
│   ├── base_ws.py                # Abstract WebSocket with reconnect + staleness
│   ├── chainlink_ws.py           # Polymarket RTDS Chainlink stream
│   ├── binance_ws.py             # Binance combined @trade stream
│   ├── clob_ws.py                # CLOB order book + resolution detection
│   ├── gamma_poller.py           # Market discovery via REST polling
│   └── binance_backfill.py       # REST API for gap filling and cold-start vol
├── state/
│   ├── market_state.py           # In-memory price + book state per asset
│   └── interval_tracker.py       # Interval lifecycle, vol computation
└── writers/
    ├── snapshot_writer.py        # Per-second CSV rows
    └── interval_writer.py        # Per-interval JSONL records
```

---

## 3. Configuration & Usage

### CLI Flags

```bash
python3 main.py                              # All assets, all timeframes
python3 main.py --asset btc                  # BTC only (both 5m and 15m)
python3 main.py --asset btc,eth              # BTC and ETH
python3 main.py --timeframe 15m             # 15-minute markets only (all assets)
python3 main.py --asset btc --timeframe 5m  # BTC 5-minute only
python3 main.py --data-dir /path/to/data    # Custom output directory
```

The CLI validates combinations. If you specify `--asset eth --timeframe 5m`, it exits with an error because ETH doesn't have 5-minute markets.

### Asset Registry (config.py)

```python
ASSET_REGISTRY = {
    "btc": {
        "chainlink_symbol": "btc/usd",
        "binance_symbol": "btcusdt",
        "available_timeframes": ["5m", "15m"],
    },
    "eth": {
        "chainlink_symbol": "eth/usd",
        "binance_symbol": "ethusdt",
        "available_timeframes": ["5m", "15m"],
    },
    # ... sol, xrp similar (5m + 15m)
}
```

**To add a new asset**: Add an entry here. No code changes needed. The Observer will automatically subscribe to the new Chainlink and Binance symbols, discover markets via Gamma, and write separate data files.

**To add a new timeframe** (e.g., if Polymarket launches 1-hour markets): Add to `TIMEFRAME_SETTINGS` and update the relevant asset's `available_timeframes`. The clock module already handles arbitrary durations.

### Timeframe Settings

```python
TIMEFRAME_SETTINGS = {
    "5m":  {"duration_s": 300, "trading_window_s": 240},
    "15m": {"duration_s": 900, "trading_window_s": 600},
}
```

`duration_s` is the full interval length. `trading_window_s` is the last N seconds where the strategy would consider entering — the Observer captures the full interval regardless, enabling retroactive testing of different window sizes.

### Key Constants

| Constant | Value | Purpose |
|----------|-------|---------|
| `SNAPSHOT_INTERVAL_MS` | 1000 | One snapshot per second |
| `GAMMA_POLL_INTERVAL_S` | 30 | Market discovery polling frequency |
| `GAMMA_BOUNDARY_DELAY_S` | 5 | Seconds after boundary to query for new markets |
| `RESOLUTION_POLL_INTERVAL_S` | 15 | How often to check pending resolutions via Gamma |
| `RESOLUTION_TIMEOUT_S` | 600 | Mark unresolved after 10 minutes |
| `VOL_LOOKBACK_INTERVALS` | 20 | Number of historical deltas for realized vol |
| `WS_RECONNECT_BASE_S` | 1 | Initial reconnect backoff |
| `WS_RECONNECT_MAX_S` | 10 | Maximum reconnect backoff |
| `CHAINLINK_STALENESS_TIMEOUT_S` | 5 | Force reconnect if no Chainlink tick for 5s |
| `BINANCE_STALENESS_TIMEOUT_S` | 3 | Force reconnect if no Binance tick for 3s |
| `CLOB_STALENESS_TIMEOUT_S` | 10 | Force reconnect if no CLOB message for 10s |
| `BACKFILL_MAX_GAP_S` | 300 | Maximum gap size to attempt backfill (5 min) |

---

## 4. Data Schemas

### File Structure

```
data/
├── 5m/
│   ├── snapshots/
│   │   └── btc_2026-02-17.csv          # 1 row/second, ~86,400 rows/day
│   └── intervals/
│       └── btc_2026-02-17.jsonl        # 1-2 records per interval, ~288/day
├── 15m/
│   ├── snapshots/
│   │   ├── btc_2026-02-17.csv
│   │   ├── eth_2026-02-17.csv
│   │   ├── sol_2026-02-17.csv
│   │   └── xrp_2026-02-17.csv
│   └── intervals/
│       ├── btc_2026-02-17.jsonl
│       ├── eth_2026-02-17.jsonl
│       ├── sol_2026-02-17.jsonl
│       └── xrp_2026-02-17.jsonl
├── health/
│   └── 2026-02-17.jsonl                # Connection events
└── logs/
    └── observer_2026-02-17.log         # Application log
```

Files rotate daily at midnight UTC. Expected data volume: ~300–350 MB/day for all assets and timeframes at 1-second resolution. Approximately 2 GB/week, 8.5 GB/month uncompressed.

### Snapshot CSV

One row per second per (asset, timeframe) pair. This is the primary dataset for ML and backtesting.

| Column | Type | Description |
|--------|------|-------------|
| `timestamp` | int | Unix timestamp (seconds) |
| `timestamp_iso` | string | ISO 8601 UTC timestamp |
| `interval_id` | string | e.g., `btc-updown-5m-1739836800` |
| `seconds_into_interval` | int | 0 to 299 (5m) or 0 to 899 (15m) |
| `market_phase` | string | `early`, `mid`, `late` (interval split into thirds) |
| `chainlink_price` | float/empty | Chainlink BTC/USD price. Empty if no tick received yet. |
| `chainlink_tick_age_ms` | int | Milliseconds since last Chainlink tick was received. -1 if never received. High values (>2000) indicate a gap. |
| `chainlink_source` | string | `live` = from WebSocket, `backfill_binance` = substituted from Binance during gap, `missing` = no data |
| `binance_price` | float/empty | Binance BTCUSDT last trade price |
| `binance_tick_age_ms` | int | Milliseconds since last Binance tick. -1 if never received. |
| `up_token_bid` | float/empty | Best bid price for the Up token |
| `up_token_ask` | float/empty | Best ask price for the Up token |
| `up_depth_1` | float | Size at best ask level (level 1) |
| `up_depth_2` | float | Size at ask level 2 |
| `up_depth_3` | float | Size at ask level 3 |
| `down_token_bid` | float/empty | Best bid for Down token |
| `down_token_ask` | float/empty | Best ask for Down token |
| `down_depth_1` | float | Size at best ask level 1 |
| `down_depth_2` | float | Size at ask level 2 |
| `down_depth_3` | float | Size at ask level 3 |
| `spread_up` | float/empty | Ask - bid for Up token |
| `spread_down` | float/empty | Ask - bid for Down token |
| `book_source` | string | `live` or `missing` (CLOB has no backfill) |

**Edge cases**:
- Empty string means no data available (connection down or not yet received)
- First few rows of a new interval may have empty Chainlink/Binance prices
- `book_source: "missing"` means CLOB was disconnected — all book columns will be empty/zero
- BTC rows appear in both `5m/snapshots/` and `15m/snapshots/` — same price data, different `interval_id` and book data (different tokens)

### Interval JSONL

Two record types per interval, identified by the `type` field:

**Summary record** (written when interval ends):
```json
{
  "interval_id": "btc-updown-5m-1739836800",
  "type": "summary",
  "asset": "btc",
  "timeframe": "5m",
  "start_ts": 1739836800,
  "end_ts": 1739837100,
  "up_token_id": "0x1234...",
  "down_token_id": "0x5678...",
  "chainlink_open": 97000.45,
  "chainlink_close": 97152.30,
  "chainlink_high": 97210.50,
  "chainlink_low": 96980.20,
  "chainlink_high_ts": 1739836950,
  "chainlink_low_ts": 1739836870,
  "binance_open": 97001.20,
  "binance_close": 97150.80,
  "delta": 0.00157,
  "abs_delta": 0.00157,
  "resolution": null,
  "resolved_at": null,
  "chainlink_tick_count": 298,
  "chainlink_gap_count": 2,
  "chainlink_max_gap_ms": 3200,
  "realized_vol_20": 0.00145,
  "open_basis_bps": 0.77,
  "close_basis_bps": 1.54
}
```

**Resolution record** (written when outcome is determined):
```json
{
  "interval_id": "btc-updown-5m-1739836800",
  "type": "resolution",
  "resolution": "up",
  "resolved_at": 1739837145
}
```

Key fields:
- `delta`: `(chainlink_close - chainlink_open) / chainlink_open` — the value the strategy computes
- `resolution`: `"up"`, `"down"`, or `"unresolved"` — the label for ML training
- `chainlink_tick_count`: Expected ~300 for 5-min, ~900 for 15-min. Lower means data gaps.
- `chainlink_gap_count`: Number of times >2 seconds passed between Chainlink ticks
- `open_basis_bps`: `|chainlink_open - binance_open| / chainlink_open × 10000` — measures how different the two feeds are at interval start
- `realized_vol_20`: Standard deviation of the last 20 interval deltas. This is what the trading formula uses as `vol`.

### Health Log

```json
{
  "timestamp": 1739836800,
  "timestamp_iso": "2026-02-18T00:00:00Z",
  "source": "chainlink_rtds",
  "event": "stale",
  "details": {"timeout_s": 5}
}
```

Events: `connecting`, `connected`, `disconnected`, `stale`, `reconnecting`, `error`, `gap_detected`.

### Joining Snapshots to Intervals for ML

The join key is `interval_id`. Every snapshot row has an `interval_id`, and each interval JSONL record has the same `interval_id` plus the `resolution` label.

```python
import pandas as pd
import json

# Load snapshots
snapshots = pd.read_csv("data/15m/snapshots/btc_2026-02-17.csv")

# Load interval resolutions
resolutions = {}
with open("data/15m/intervals/btc_2026-02-17.jsonl") as f:
    for line in f:
        record = json.loads(line)
        if record["type"] == "resolution":
            resolutions[record["interval_id"]] = record["resolution"]

# Join
snapshots["resolution"] = snapshots["interval_id"].map(resolutions)

# Now every row is a potential decision point with features and label
```

The `market_phase` column provides a categorical feature useful for tree-based models. `seconds_into_interval` is the continuous equivalent for any model type.

---

## 5. Connections & APIs

### Chainlink RTDS (Primary Price Source)

**Endpoint**: `wss://ws-live-data.polymarket.com`
**Authentication**: None required
**Topic**: `crypto_prices_chainlink`

**Subscription message**:
```json
{
  "action": "subscribe",
  "subscriptions": [{
    "topic": "crypto_prices_chainlink",
    "type": "*",
    "filters": ""
  }]
}
```

**Incoming message format**:
```json
{
  "topic": "crypto_prices_chainlink",
  "payload": {
    "symbol": "btc/usd",
    "value": 97000.45,
    "timestamp": 1739836800123
  }
}
```

Update frequency is approximately once per second. This is Polymarket's relay of Chainlink Data Streams, which is the pull-based, sub-second product — not the traditional slow push oracle that updates every 60 seconds.

**Known issue**: GitHub issue #31 on Polymarket's `real-time-data-client` repo documents that this feed sometimes drops ticks. The reporter observed 8-second gaps in what should be per-second data. The Binance feed was continuous during the same gaps. No fix or workaround was provided by Polymarket. This is the primary reliability concern for the Observer.

**Known issue**: The first WebSocket message after connect is sometimes empty or whitespace. The Observer skips empty messages before attempting JSON parsing.

**Why this is the primary source**: Polymarket settles 5-min and 15-min markets against Chainlink prices. Using any other source for delta calculation introduces basis risk — the spread between feeds can flip the outcome on tight moves.

### Binance WebSocket (Secondary Price Source)

**Endpoint**: `wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade`
**Authentication**: None required

Uses the combined stream format. Streams are specified in the URL path.

**Incoming message format**:
```json
{
  "stream": "btcusdt@trade",
  "data": {
    "e": "trade",
    "s": "BTCUSDT",
    "p": "97001.20",
    "T": 1739836800456
  }
}
```

Fires dozens of times per second per symbol. Used for:
- Backfill when Chainlink drops (marked as `chainlink_source: "backfill_binance"`)
- Cold-start volatility estimation via REST API
- Basis measurement (how much Chainlink and Binance diverge)
- Future: possible leading indicator signal (Binance moves ~hundreds of milliseconds before Chainlink)

**Latency vs Chainlink**: Sub-second. Chainlink Data Streams sources from multiple premium aggregators that almost certainly include Binance. The gap is hundreds of milliseconds, not seconds. The latency arbitrage strategy (watch Binance, front-run Chainlink) was killed by the taker fee introduction.

### CLOB WebSocket (Order Book Data)

**Endpoint**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
**Authentication**: None required

**Subscription message**:
```json
{
  "assets_ids": ["0x1234...", "0x5678..."],
  "type": "market"
}
```

Note: the field is `assets_ids` (plural with underscore), not `asset_ids`.

**Event types**:

The CLOB WebSocket sends three event types. Approximately 75% of messages are `price_change` (incremental level updates), 15% are `book` (full snapshots), and 10% are `last_trade_price` (ignored).

**Full book snapshot** (`book` event):
```json
{
  "event_type": "book",
  "asset_id": "0x1234...",
  "bids": [{"price": "0.72", "size": "500"}],
  "asks": [{"price": "0.74", "size": "300"}]
}
```

Note: asks may appear as `"sells"` or `"asks"` depending on the message. Bids arrive sorted ascending (lowest first) — the Observer re-sorts bids descending (highest/best first) and asks ascending (lowest/best first) after parsing.

**Incremental level update** (`price_change` event):
```json
{
  "event_type": "price_change",
  "price_changes": [
    {
      "asset_id": "0x1234...",
      "side": "BUY",
      "price": "0.72",
      "size": "600"
    }
  ]
}
```

The `price_changes` array contains one or more per-token level changes. Each change specifies a side (`BUY` for bids, `SELL` for asks), a price level, and the new size at that level. A size of `"0"` means the level should be removed from the book. The Observer merges these into the running `TokenBook` via sorted insert, in-place update, or removal.

Some CLOB messages are arrays (not dicts) — the Observer guards against this with an `isinstance(msg, dict)` check before processing.

**Resolution detection**: When a token's best bid snaps to ≥$0.95 or <$0.01, the market has likely settled. The Observer uses this as a fast-path resolution signal for pending intervals only (active intervals end at their time boundary regardless of price).

**No backfill available**: If the CLOB connection drops, there is no way to recover historical order book snapshots. Rows during the outage will have `book_source: "missing"` and empty book columns.

### Gamma REST API (Market Discovery)

**Endpoint**: `GET https://gamma-api.polymarket.com/events?slug=<slug>`
**Authentication**: None required

The Observer constructs expected slugs directly (`{asset}-updown-{tf}-{start_ts}`) and queries for the current and next interval. This replaced an earlier tag-based approach (`?tag=crypto&tag=up-or-down`) which returned no results for up-down markets.

Returns event objects containing nested market objects. Each market has:
- `slug`: e.g., `"btc-updown-15m-1739836800"` — parsed via regex to extract asset, timeframe, and Unix start timestamp
- `clobTokenIds`: **JSON-encoded string** (not a native array) — e.g., `"[\"0x1234...\",\"0x5678...\"]"`. Requires `json.loads()` to deserialize into a list of token IDs
- `outcomes`: Array like `["Up", "Down"]` — mapped to token IDs by position
- `outcomePrices`: Current prices (used for resolution checking)

**Polling strategy**: Every 30 seconds continuously, plus an extra poll 5 seconds after each interval boundary (when new markets are expected to appear).

**Token ID lifecycle**: When GammaPoller discovers a future interval's market, the token IDs are stored in `_discovered_markets` but not immediately applied to `AssetState`. Token IDs are only applied when the interval becomes active (via `_sync_active_tokens()`), preventing stale book data from overwriting the current interval's tokens.

**Resolution checking**: `GET https://gamma-api.polymarket.com/markets?slug=<slug>` — checks if `closed: true` and reads `outcomePrices` to determine winner.

### Binance REST API (Backfill & Cold Start)

**Klines endpoint**: `GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=20`

Used for two purposes:

1. **Cold-start volatility**: On startup, fetches the last 20 klines at the matching interval (5m klines for 5m markets, 15m klines for 15m markets). Computes (close - open) / open for each to seed the realized volatility history. Without this, the first 20 intervals would have no vol estimate.

2. **Gap filling**: When the Chainlink WebSocket reconnects after a drop, fetches 1-second klines from Binance to fill the gap. These rows are written with `chainlink_source: "backfill_binance"`. Gaps exceeding `BACKFILL_MAX_GAP_S` (300 seconds) are skipped — too much missing data to be useful.

---

## 6. Resilience & Recovery

### Connection Level

Every WebSocket connection inherits from `BaseWebSocket`, which provides:

**Auto-reconnect with exponential backoff**: Starts at 1 second, doubles each attempt, caps at 10 seconds. Resets to 1 second on successful connection. Runs indefinitely until `stop()` is called.

**Staleness watchdog**: Each connection has a staleness timeout. If no message is received within the timeout window, the connection is forcibly closed and reconnected. This catches silent failures where the TCP socket stays open but the server stops sending data.

| Connection | Staleness Timeout |
|-----------|-------------------|
| Chainlink RTDS | 5 seconds |
| Binance WS | 3 seconds |
| CLOB WS | 10 seconds |

**SSL context**: All connections (WebSocket and aiohttp) use a shared `SSL_CONTEXT` created with `certifi`'s CA bundle. This is required on macOS Homebrew Python, which lacks system CA certificates. The SSL context is created once in `config.py` and imported by all connection modules.

**Health event emission**: Every connect, disconnect, stale detection, reconnect, and error emits a health event to the `HealthMonitor`, which writes it to the daily health JSONL.

### Process Level

The main loop and each connection run as independent asyncio tasks. Critical design principle: **no single component failure takes down the process**.

- If the CLOB connection dies and can't reconnect, the snapshot loop keeps running — book columns will be empty/zero with `book_source: "missing"`, but price data continues flowing.
- If the Chainlink stream drops, Binance data still writes, and the interval tracker still captures Binance ticks for basis comparison.
- If the Gamma poller fails, existing market subscriptions continue. New markets won't be discovered until the poller recovers, but this means at most one missed interval per asset.

Every callback and loop body is wrapped in try/except with error logging. Unhandled exceptions in a task are caught by the `asyncio.wait(FIRST_EXCEPTION)` pattern in `Observer.start()`, which logs the failure and initiates graceful shutdown.

### System Level

The process itself can crash (out of memory, OS kill, power loss). For production deployment:

**Use a process supervisor**. The simplest option is a systemd service:

```ini
[Unit]
Description=Polymarket Observer
After=network.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/path/to/observer
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Or use `supervisord`, `pm2`, or any process manager that restarts on exit.

**Crash recovery on startup**: When the Observer starts, it doesn't need to know where it left off in a sophisticated way. It simply:

1. Reads the current time, computes the current interval boundaries
2. Opens new (or appends to existing) snapshot CSV files
3. Starts capturing from the current moment forward
4. The gap between crash and restart will be visible as missing rows in the snapshot CSV — any analysis pipeline should handle gaps gracefully
5. Cold-start vol is re-estimated from Binance klines, so the vol estimate is available immediately

The data files are append-only. No risk of corruption on crash — the worst case is a partially written CSV row (last line), which can be detected and discarded during analysis.

**Graceful shutdown**: `SIGINT` and `SIGTERM` trigger `Observer.stop()`, which closes all WebSocket connections, flushes write buffers, and closes file handles.

---

## 7. Design Decisions & Known Limitations

### Key Decisions

**One process instead of two**: The original plan was separate processes for 5-min and 15-min timeframes. With only BTC on 5-minute, that means the 5m process would open redundant Chainlink and Binance connections just for one asset. A single process with three connections serves all assets and both timeframes, sharing the same price ticks. BTC Chainlink ticks write to both `5m/snapshots/btc_*.csv` and `15m/snapshots/btc_*.csv`. When more assets get 5-minute markets, just update the config.

**Chainlink primary over Binance**: Polymarket settles 5/15-min markets against Chainlink. Using Binance as the delta reference introduces basis risk — even a tiny divergence at the boundary can flip the outcome. Binance is secondary: backfill, vol estimation, and basis measurement.

**Backfill Binance instead of interpolating Chainlink**: When Chainlink drops ticks, we substitute Binance prices (flagged as `chainlink_source: "backfill_binance"`) rather than interpolating between the last and next Chainlink ticks. Interpolation assumes linear price movement, which is often wrong during volatile moments — exactly when gaps matter most. Binance provides actual market prices at every second.

**Taker-only, not maker**: The Trader (not yet built) will use taker orders for guaranteed fills. Maker orders have zero fees and earn rebates, but fill probability degrades as the outcome becomes more certain — exactly when the strategy's signal is strongest. Taker fees at favorable price levels ($0.80+) are under 1%, which the expected edge should exceed after calibration. The strategy accepts this cost for execution certainty.

**CSV + JSONL over database**: Flat files are the simplest format for a data collection system that writes continuously and reads offline. No database server to manage, no schema migrations, no connection pooling. CSV loads directly into pandas. JSONL is append-only and crash-safe. For the data volumes involved (~350 MB/day), this is more than adequate. If volumes grow (more assets, sub-second resolution), Parquet compression is the natural next step.

**Full interval capture, not just trading window**: The Observer captures from second 0 to the end of the interval, not just the trading window. This enables retroactive testing of different window sizes and gate configurations without needing to re-collect data.

### Known Limitations

**No Chainlink historical API**: If the Chainlink stream drops at a critical moment (like the interval boundary where open price is captured), that price is lost permanently. There is no Chainlink REST endpoint to fetch historical prices. The Binance backfill provides a substitute, but it's a different price source, introducing basis risk for that interval.

**CLOB has no backfill**: Order book data has no historical API. If the CLOB connection drops, those seconds of book data are gone. Snapshot rows will have empty book columns. For backtesting, these rows should be excluded from any analysis that depends on book data.

**Token ID mapping is fragile**: The Gamma API provides `clobTokenIds` and `outcomes` as separate arrays. The Observer assumes the first outcome containing "up" maps to the first matching token ID. If Polymarket changes the outcome labeling or ordering, this mapping could break silently.

**No deduplication on restart**: If the process crashes and restarts mid-interval, the new interval record starts fresh — it doesn't know what the previous instance already captured. This means the open price for the current interval will be the first tick after restart, not the true interval-start price. The snapshot CSV will also have a gap during the crash period. Analysis pipelines should detect intervals with anomalously low tick counts.

**Single point of failure**: One process means one machine. If the machine goes down, all data collection stops. For higher reliability, run a second Observer instance on a different machine writing to separate files, and reconcile afterward. The system doesn't support this natively but the append-only file format makes manual reconciliation straightforward.

**Realized vol cold start is approximate**: On startup, vol is estimated from Binance klines, not Chainlink. The basis between feeds means these vol estimates are slightly different from what steady-state Chainlink-derived vol would be. After 20 intervals of live data (~100 minutes for 5-min, ~5 hours for 15-min), the cold-start values are fully replaced.

---

## 8. Next Steps

### Immediate: Run Observer & Collect Data

Deploy the Observer and let it run for 1–2 weeks. The goal is to accumulate enough data to:
- Validate that Chainlink ticks arrive reliably (check gap frequency and duration)
- Measure the Chainlink-Binance basis distribution
- Confirm that Gamma market discovery catches all markets
- Verify resolution detection works (all intervals get resolved, not timing out)
- Build a dataset large enough for parameter calibration (~2,000+ intervals per timeframe)

### Backtesting Pipeline

Build an offline analysis pipeline that:
1. Loads snapshot CSVs and interval JSONLs
2. Replays the trading formula second-by-second against historical data
3. Simulates entries, flips, and settlement
4. Computes PnL after fees for each interval
5. Aggregates into calibration curves, profit factors, win rates, per-market breakdowns

The snapshot data already contains everything the formula needs: `chainlink_price` (for delta from open), `seconds_into_interval` (for elapsed), book data (for liquidity gates). The interval JSONL provides `realized_vol_20` and `resolution` (for computing P&L).

### Parameter Calibration

Tune `a`, `b`, `offset`, and `F` separately for 5-min and 15-min markets. Shorter intervals have less time for deltas to develop and noisier vol — the same parameters likely don't work for both.

Calibration targets:
- **Calibration curve**: Does the formula's confidence correlate with actual win rate? If it says 80% confident, does the direction hold ~80% of the time?
- **Profit factor after fees**: Total gains / total losses, accounting for taker fees at the entry price
- **Per-market breakdown**: Does the formula work equally well across BTC/ETH/SOL/XRP, or does it need per-asset tuning?

### Correlation Analysis

BTC, ETH, SOL, and XRP are heavily correlated. A strong BTC trend triggers entries on all four assets simultaneously. A reversal flips all four. What looks like 4-asset diversification is really 1 concentrated bet × 4. The Observer data will show exactly how correlated the deltas are across assets during the same time windows. This informs a cross-asset exposure cap in the Trader.

### Trader Component

Depends on calibrated parameters. Key components:
- Phantom wallet integration for programmatic order execution on Polymarket
- Taker order submission via Polymarket CLOB REST API
- Position tracking (current holdings per interval)
- Dry run mode: execute the full decision pipeline but log trades instead of submitting orders
- P&L tracking and reporting

The Trader reads from the same data streams as the Observer (or reads Observer output directly) and applies the calibrated formula in real time.

### AI Orchestrator Layer

The eventual goal is an AI layer that manages the entire system:
- Monitors Observer health and restarts if needed
- Runs backtesting and recalibration periodically as more data accumulates
- Adjusts parameters based on recent performance
- Manages risk limits (per-interval position size, cross-asset exposure)
- Decides when to go live vs. dry-run based on confidence in calibration
- Handles edge cases the rule-based system can't (unusual market conditions, API changes)

The Observer's flat-file output format is designed to be readable by any process. The AI orchestrator can consume CSVs and JSONLs without needing a shared database or IPC mechanism.

---

## 9. Analysis Tooling

### Setup

Analysis dependencies are separate from runtime:

```bash
uv venv .venv                                    # Creates venv with Python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt -r requirements-analysis.txt
```

### Module Map

```
analysis/
├── __init__.py
├── data_loader.py              # Load CSVs + JSONLs into DataFrames
├── backtest.py                 # Sigmoid formula replay engine
├── eda.ipynb                   # Exploratory data analysis
└── backtest_results.ipynb      # Backtest results + parameter search
```

### Data Loader (`analysis/data_loader.py`)

Key functions:

- `load_snapshots(asset, timeframe, date=None)` — Load snapshot CSV(s) into a DataFrame. Coerces numeric columns, auto-discovers dates if none given.
- `load_intervals(asset, timeframe, date=None)` — Load interval JSONL(s), joining summary + resolution records into one row per interval.
- `load_all_intervals(timeframe)` / `load_all_snapshots(timeframe)` — Load all assets for a timeframe.
- `join_snapshots_intervals(snapshots, intervals)` — Merge resolution labels and interval-level features (chainlink_open, realized_vol_20) onto snapshot rows via `interval_id`.
- `add_formula_features(df, timeframe)` — Compute derived columns: `delta`, `abs_delta`, `in_trading_window`, `window_elapsed`, `window_fraction`, `fee_up`, `fee_down`.

### Backtest Engine (`analysis/backtest.py`)

Replays the sigmoid confidence formula second-by-second:

```python
from analysis.backtest import run_backtest, FormulaParams, GateConfig

bt = run_backtest(snapshots, intervals,
                  params=FormulaParams(a=5, b=3, offset=4),
                  gates=GateConfig(min_token_price=0.65, max_token_price=0.85),
                  timeframe="5m")
print(bt.summary())
```

**FormulaParams**: `a` (delta sensitivity), `b` (time weight), `offset` (sigmoid centering), `flip_threshold`.

**GateConfig**: `min_token_price`, `max_token_price`, `min_depth`, `require_live_book`, `require_live_chainlink`.

**BacktestResult** properties: `win_rate`, `profit_factor`, `total_pnl`, `total_fees`, `summary()`.

**PnL model**: Each entry buys 1 share at the ask price + fee. On settlement, the token pays $1 (correct) or $0 (wrong). Flips abandon the first position (worst case $0 payout) and enter a new one.

### Running Notebooks

```bash
source .venv/bin/activate
jupyter notebook analysis/        # Interactive
# Or headless:
jupyter nbconvert --execute analysis/eda.ipynb --to notebook
```
