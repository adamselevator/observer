"""
Market state.
Holds the latest price and order book data for each asset.
Updated by WebSocket connections, read by the snapshot loop.
Thread-safe via asyncio (single-threaded event loop).
"""

import time
from dataclasses import dataclass, field


@dataclass
class PriceTick:
    """A single price observation."""

    price: float
    timestamp_ms: int  # source timestamp in milliseconds
    received_at: float = field(default_factory=time.time)  # local receipt time

    @property
    def age_ms(self) -> int:
        """Milliseconds since this tick was received."""
        return int((time.time() - self.received_at) * 1000)


@dataclass
class BookLevel:
    """One price level in the order book."""

    price: float
    size: float


@dataclass
class TokenBook:
    """Order book for one outcome token (Up or Down)."""

    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    last_update: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def age_ms(self) -> int:
        if self.last_update == 0.0:
            return -1
        return int((time.time() - self.last_update) * 1000)

    def top_depths(self, n: int = 3) -> tuple[list[float], list[float]]:
        """Return top N bid sizes and top N ask sizes."""
        bid_sizes = [b.size for b in self.bids[:n]]
        ask_sizes = [a.size for a in self.asks[:n]]
        # Pad with 0 if fewer than n levels
        bid_sizes.extend([0.0] * (n - len(bid_sizes)))
        ask_sizes.extend([0.0] * (n - len(ask_sizes)))
        return bid_sizes, ask_sizes


@dataclass
class AssetState:
    """Complete observable state for one asset."""

    asset: str
    chainlink: PriceTick | None = None
    binance: PriceTick | None = None
    up_book: TokenBook = field(default_factory=TokenBook)
    down_book: TokenBook = field(default_factory=TokenBook)

    # Token IDs for the current active markets (keyed by timeframe)
    up_token_ids: dict[str, str] = field(default_factory=dict)
    down_token_ids: dict[str, str] = field(default_factory=dict)

    def update_chainlink(self, price: float, timestamp_ms: int):
        self.chainlink = PriceTick(price=price, timestamp_ms=timestamp_ms)

    def update_binance(self, price: float, timestamp_ms: int):
        self.binance = PriceTick(price=price, timestamp_ms=timestamp_ms)

    def update_book(self, token_id: str, bids: list[BookLevel], asks: list[BookLevel]):
        """Update the order book for a token. Matches token_id to up/down."""
        book = TokenBook(bids=bids, asks=asks, last_update=time.time())
        # Check across all timeframes which side this token belongs to
        for tf, tid in self.up_token_ids.items():
            if tid == token_id:
                self.up_book = book
                return
        for tf, tid in self.down_token_ids.items():
            if tid == token_id:
                self.down_book = book
                return

    def set_token_ids(self, timeframe: str, up_id: str, down_id: str):
        self.up_token_ids[timeframe] = up_id
        self.down_token_ids[timeframe] = down_id

    def get_all_token_ids(self) -> list[str]:
        """All active token IDs for CLOB subscription."""
        ids = []
        ids.extend(self.up_token_ids.values())
        ids.extend(self.down_token_ids.values())
        return ids


class MarketState:
    """
    Aggregated state for all tracked assets.
    Central read/write point between connections and writers.
    """

    def __init__(self, assets: list[str]):
        self._states: dict[str, AssetState] = {
            asset: AssetState(asset=asset) for asset in assets
        }

    def get(self, asset: str) -> AssetState:
        return self._states[asset]

    def all_assets(self) -> list[AssetState]:
        return list(self._states.values())

    def get_all_token_ids(self) -> list[str]:
        """All active token IDs across all assets."""
        ids = []
        for state in self._states.values():
            ids.extend(state.get_all_token_ids())
        return ids

    def update_chainlink(self, symbol: str, price: float, timestamp_ms: int):
        """Update Chainlink price. Symbol is like 'btc/usd'."""
        asset = symbol.split("/")[0].lower()
        if asset in self._states:
            self._states[asset].update_chainlink(price, timestamp_ms)

    def update_binance(self, symbol: str, price: float, timestamp_ms: int):
        """Update Binance price. Symbol is like 'btcusdt'."""
        # Strip 'usdt' suffix to get asset
        asset = symbol.lower().replace("usdt", "")
        if asset in self._states:
            self._states[asset].update_binance(price, timestamp_ms)

    def update_book(self, token_id: str, bids: list[BookLevel], asks: list[BookLevel]):
        """Route book update to the correct asset by token ID."""
        for state in self._states.values():
            all_ids = state.get_all_token_ids()
            if token_id in all_ids:
                state.update_book(token_id, bids, asks)
                return
