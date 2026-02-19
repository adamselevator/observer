"""
Observer configuration.
All tunable parameters in one place. No magic numbers in code.
"""

import argparse
import ssl
from dataclasses import dataclass, field
from pathlib import Path

import certifi


# ── Asset / timeframe definitions ──────────────────────────────────────────

ASSET_REGISTRY = {
    "btc": {
        "chainlink_symbol": "btc/usd",
        "binance_symbol": "btcusdt",
        "available_timeframes": ["5m", "15m"],
    },
    "eth": {
        "chainlink_symbol": "eth/usd",
        "binance_symbol": "ethusdt",
        "available_timeframes": ["15m"],
    },
    "sol": {
        "chainlink_symbol": "sol/usd",
        "binance_symbol": "solusdt",
        "available_timeframes": ["15m"],
    },
    "xrp": {
        "chainlink_symbol": "xrp/usd",
        "binance_symbol": "xrpusdt",
        "available_timeframes": ["15m"],
    },
}

TIMEFRAME_SETTINGS = {
    "5m": {"duration_s": 300, "trading_window_s": 240},
    "15m": {"duration_s": 900, "trading_window_s": 600},
}

# ── Endpoints ──────────────────────────────────────────────────────────────

CHAINLINK_RTDS_URL = "wss://ws-live-data.polymarket.com"
BINANCE_WS_BASE = "wss://stream.binance.com:9443"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_REST_URL = "https://clob.polymarket.com"
GAMMA_REST_URL = "https://gamma-api.polymarket.com"

# ── Timing ─────────────────────────────────────────────────────────────────

SNAPSHOT_INTERVAL_MS = 1000
GAMMA_POLL_INTERVAL_S = 30
GAMMA_BOUNDARY_DELAY_S = 5  # seconds after interval boundary to query for new market
RESOLUTION_POLL_INTERVAL_S = 15
RESOLUTION_TIMEOUT_S = 600
HEALTH_LOG_INTERVAL_S = 60

# ── Backfill ───────────────────────────────────────────────────────────────

BACKFILL_MAX_GAP_S = 300
BINANCE_REST_URL = "https://api.binance.com"

# ── Connection resilience ──────────────────────────────────────────────────

WS_RECONNECT_BASE_S = 1
WS_RECONNECT_MAX_S = 10
CHAINLINK_STALENESS_TIMEOUT_S = 5
BINANCE_STALENESS_TIMEOUT_S = 3
CLOB_STALENESS_TIMEOUT_S = 10

# ── SSL ───────────────────────────────────────────────────────────────────

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# ── Volatility ─────────────────────────────────────────────────────────────

VOL_LOOKBACK_INTERVALS = 20

# ── Data paths ─────────────────────────────────────────────────────────────

DEFAULT_DATA_DIR = Path("data")


@dataclass
class ObserverConfig:
    """Runtime configuration built from CLI args and defaults."""

    assets: list[str]
    timeframes: list[str]
    data_dir: Path = DEFAULT_DATA_DIR

    # Derived on init
    chainlink_symbols: list[str] = field(default_factory=list)
    binance_symbols: list[str] = field(default_factory=list)
    asset_timeframe_pairs: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self):
        self.chainlink_symbols = list({
            ASSET_REGISTRY[a]["chainlink_symbol"] for a in self.assets
        })
        self.binance_symbols = list({
            ASSET_REGISTRY[a]["binance_symbol"] for a in self.assets
        })

        # Build valid (asset, timeframe) pairs
        for asset in self.assets:
            available = ASSET_REGISTRY[asset]["available_timeframes"]
            for tf in self.timeframes:
                if tf in available:
                    self.asset_timeframe_pairs.append((asset, tf))

        if not self.asset_timeframe_pairs:
            raise ValueError(
                f"No valid asset/timeframe combinations. "
                f"Assets: {self.assets}, Timeframes: {self.timeframes}"
            )

    def get_binance_symbol(self, asset: str) -> str:
        return ASSET_REGISTRY[asset]["binance_symbol"]

    def get_chainlink_symbol(self, asset: str) -> str:
        return ASSET_REGISTRY[asset]["chainlink_symbol"]

    def get_duration(self, timeframe: str) -> int:
        return TIMEFRAME_SETTINGS[timeframe]["duration_s"]

    def get_trading_window(self, timeframe: str) -> int:
        return TIMEFRAME_SETTINGS[timeframe]["trading_window_s"]


def parse_args() -> ObserverConfig:
    """Parse CLI arguments into ObserverConfig."""
    parser = argparse.ArgumentParser(description="Polymarket Observer")

    parser.add_argument(
        "--asset",
        type=str,
        default=None,
        help="Comma-separated assets to observe (e.g., btc,eth). Default: all.",
    )
    parser.add_argument(
        "--timeframe",
        type=str,
        default=None,
        help="Comma-separated timeframes (e.g., 5m,15m). Default: all.",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=str(DEFAULT_DATA_DIR),
        help=f"Data output directory. Default: {DEFAULT_DATA_DIR}",
    )

    args = parser.parse_args()

    # Parse assets
    if args.asset:
        assets = [a.strip().lower() for a in args.asset.split(",")]
        invalid = [a for a in assets if a not in ASSET_REGISTRY]
        if invalid:
            parser.error(
                f"Unknown assets: {invalid}. Valid: {list(ASSET_REGISTRY.keys())}"
            )
    else:
        assets = list(ASSET_REGISTRY.keys())

    # Parse timeframes
    if args.timeframe:
        timeframes = [t.strip().lower() for t in args.timeframe.split(",")]
        invalid = [t for t in timeframes if t not in TIMEFRAME_SETTINGS]
        if invalid:
            parser.error(
                f"Unknown timeframes: {invalid}. Valid: {list(TIMEFRAME_SETTINGS.keys())}"
            )
    else:
        timeframes = list(TIMEFRAME_SETTINGS.keys())

    return ObserverConfig(
        assets=assets,
        timeframes=timeframes,
        data_dir=Path(args.data_dir),
    )
