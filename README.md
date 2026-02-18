# Polymarket Observer

Continuously captures market data for Polymarket's short-duration crypto prediction markets (5-min BTC, 15-min BTC/ETH/SOL/XRP). Designed to produce clean, ML-ready datasets for parameter calibration and strategy backtesting.

## Architecture

One process, three WebSocket connections:

- **Chainlink RTDS** — Primary price source (what Polymarket settles on)
- **Binance trade stream** — Secondary price source + backfill
- **CLOB market WebSocket** — Order book data for all active tokens
- **Gamma REST poller** — Market discovery + resolution checking

## Output

```
data/
├── 5m/
│   ├── snapshots/      # Per-second CSV (price + book combined)
│   │   └── btc_2026-02-17.csv
│   └── intervals/      # Per-interval JSONL (summary + resolution)
│       └── btc_2026-02-17.jsonl
├── 15m/
│   ├── snapshots/
│   │   ├── btc_2026-02-17.csv
│   │   ├── eth_2026-02-17.csv
│   │   ├── sol_2026-02-17.csv
│   │   └── xrp_2026-02-17.csv
│   └── intervals/
│       └── ...
├── health/
│   └── 2026-02-17.jsonl
└── logs/
    └── observer_2026-02-17.log
```

### Snapshot CSV columns
`timestamp, timestamp_iso, interval_id, seconds_into_interval, market_phase, chainlink_price, chainlink_tick_age_ms, chainlink_source, binance_price, binance_tick_age_ms, up_token_bid, up_token_ask, up_depth_1, up_depth_2, up_depth_3, down_token_bid, down_token_ask, down_depth_1, down_depth_2, down_depth_3, spread_up, spread_down, book_source`

### Interval JSONL fields
`interval_id, asset, timeframe, start_ts, end_ts, chainlink_open, chainlink_close, chainlink_high, chainlink_low, delta, abs_delta, resolution, realized_vol_20, chainlink_tick_count, chainlink_gap_count, open_basis_bps, close_basis_bps`

## Usage

```bash
# Install dependencies
pip install -r requirements.txt

# Run all assets, all timeframes (default)
python main.py

# Specific assets
python main.py --asset btc,eth

# Specific timeframe
python main.py --timeframe 5m

# Combined filters
python main.py --asset btc --timeframe 5m

# Custom data directory
python main.py --data-dir /path/to/data
```

## Resilience

- Auto-reconnect with exponential backoff on all WebSocket connections
- Staleness detection (force reconnect if no data received within timeout)
- Binance backfill for price gaps (Chainlink gaps marked as missing)
- Cold-start volatility estimation via Binance klines
- Crash recovery: on restart, resumes from last written timestamp
- Graceful shutdown on SIGINT/SIGTERM

## Current market availability

| Asset | 5-min | 15-min |
|-------|-------|--------|
| BTC   | ✓     | ✓      |
| ETH   |       | ✓      |
| SOL   |       | ✓      |
| XRP   |       | ✓      |

Update `ASSET_REGISTRY` in `config.py` when new markets become available.
