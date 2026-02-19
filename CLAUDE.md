# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run (use python3, not python — python is not on PATH)
python3 main.py                              # All assets, all timeframes
python3 main.py --asset btc,eth              # Specific assets
python3 main.py --timeframe 5m               # Specific timeframe
python3 main.py --asset btc --timeframe 5m   # Both filters

# Dependencies
pip install -r requirements.txt

# Syntax check (no test suite or linter configured)
python3 -m py_compile <file>
```

## Architecture

Single-process asyncio application that collects Polymarket prediction market data (crypto up/down markets) via three concurrent WebSocket connections plus a REST poller, writing flat files (CSV + JSONL).

### Data flow

```
Chainlink RTDS WS ──→ MarketState ──→ SnapshotWriter (CSV, 1/sec)
Binance trade WS  ──→ MarketState     │
CLOB book WS      ──→ MarketState     │
                                       ↓
Gamma REST poller ──→ IntervalTracker ──→ IntervalWriter (JSONL, per-interval)
                      (boundary detection,
                       open/close capture,
                       resolution tracking)
```

**Observer** (`main.py`) orchestrates everything. It wires callbacks from connections into state objects and runs three async loops: snapshot (1s), resolution check (15s), and health (60s).

### Connections layer (`connections/`)

All WebSocket clients inherit from **BaseWebSocket** (`base_ws.py`) which provides connect/reconnect with exponential backoff, staleness watchdog (force-reconnect if no messages within timeout), and health stats tracking.

- **ChainlinkWS** — Polymarket's RTDS feed, the settlement price source
- **BinanceWS** — Combined `@trade` stream, secondary price + basis computation
- **ClobWS** — Order book snapshots + incremental level updates for outcome tokens. Subscriptions are dynamic (added as GammaPoller discovers markets)
- **GammaPoller** — REST poller that discovers markets by constructing expected slugs (`{asset}-updown-{tf}-{ts}`) and querying the Gamma API. Also checks resolution status for pending intervals

### State layer (`state/`)

- **MarketState** / **AssetState** — Latest prices (Chainlink, Binance) and order books (up/down TokenBook) per asset. Routes updates by symbol or token ID
- **IntervalTracker** — Manages interval lifecycle: start → active (capturing OHLC) → pending resolution → resolved. Computes realized volatility from historical deltas. Fires callbacks on interval completion and resolution

### Writers layer (`writers/`)

- **SnapshotWriter** — Buffered CSV writer, one file per `(asset, timeframe, date)`. Flushes every 5s. Columns combine price ticks, book state, and interval timing
- **IntervalWriter** — Append-only JSONL, writes summary records when intervals end and resolution records when outcomes are determined

### Key domain concepts

- **Interval ID** format: `{asset}-updown-{timeframe}-{start_ts}` (e.g., `btc-updown-5m-1739836800`)
- **Token IDs** are Polymarket CLOB identifiers for up/down outcome tokens, discovered via Gamma API and used to subscribe to book data
- **Resolution** is determined two ways: CLOB price snapping to 0/1 (real-time), or Gamma API polling (fallback). Pending intervals time out after 600s
- **Trading window** is the last N seconds of an interval (240s for 5m, 600s for 15m) — relevant for `market_phase` in snapshots

## Code style

- Python 3.12+. Modern type hints: `list[str]`, `float | None`, `dict[str, str]`
- snake_case functions/variables, PascalCase classes, UPPER_SNAKE constants
- `logging.getLogger(__name__)` with `[component]` prefixes in messages
- Section separators: `# ── Name ──────...`
- Triple-quoted docstrings on classes and public methods
- All config constants and endpoints live in `config.py` — no magic numbers elsewhere
- `pyproject.toml` configures ruff (line-length 99, rules E/F/W/I) and mypy but neither is enforced in CI yet
- Always update `CHANGELOG.md` when making functional changes (bug fixes, new features, behavior changes). Group by date, describe what changed and why, list affected files
