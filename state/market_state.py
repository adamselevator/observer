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
    """Order book for one outcome token (Up or Down).

    Supports both full snapshots (replace) and incremental updates (merge).
    Bids are sorted descending by price, asks ascending.
    Tracks per-side update timestamps to detect stale quotes.
    """

    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    last_update: float = 0.0
    last_bid_update: float = 0.0
    last_ask_update: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def best_bid_size(self) -> float | None:
        """Size available at the best bid."""
        return self.bids[0].size if self.bids else None

    @property
    def best_ask_size(self) -> float | None:
        """Size available at the best ask."""
        return self.asks[0].size if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def is_crossed(self) -> bool:
        """True if best ask <= best bid (book sides are out of sync)."""
        if self.best_bid is None or self.best_ask is None:
            return False
        return self.best_ask <= self.best_bid

    @property
    def side_age_ms(self) -> tuple[int, int]:
        """(bid_age_ms, ask_age_ms). -1 if never updated."""
        now = time.time()
        bid_age = int((now - self.last_bid_update) * 1000) if self.last_bid_update > 0 else -1
        ask_age = int((now - self.last_ask_update) * 1000) if self.last_ask_update > 0 else -1
        return bid_age, ask_age

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

    def top_prices(self, n: int = 3) -> tuple[list[float], list[float]]:
        """Return top N bid prices and top N ask prices."""
        bid_prices = [b.price for b in self.bids[:n]]
        ask_prices = [a.price for a in self.asks[:n]]
        # Pad with 0 if fewer than n levels
        bid_prices.extend([0.0] * (n - len(bid_prices)))
        ask_prices.extend([0.0] * (n - len(ask_prices)))
        return bid_prices, ask_prices

    def apply_level(self, side: str, price: float, size: float):
        """Merge a single level update into the book.

        Args:
            side: "BUY" for bids, "SELL" for asks
            price: Price level
            size: New size (0 = remove the level)
        """
        now = time.time()
        if side == "BUY":
            self._merge_level(self.bids, price, size, reverse=True)
            self.last_bid_update = now
        else:
            self._merge_level(self.asks, price, size, reverse=False)
            self.last_ask_update = now
        self.last_update = now

    def apply_levels_batch(self, changes: list[tuple[str, float, float]]):
        """Apply multiple level updates with a single timestamp.

        When a price_changes message contains both BUY and SELL updates,
        both sides get the same timestamp, preventing false crossed-book
        detection from asynchronous side updates.

        Args:
            changes: list of (side, price, size) tuples
        """
        now = time.time()
        has_bid = False
        has_ask = False

        for side, price, size in changes:
            if side == "BUY":
                self._merge_level(self.bids, price, size, reverse=True)
                has_bid = True
            else:
                self._merge_level(self.asks, price, size, reverse=False)
                has_ask = True

        if has_bid:
            self.last_bid_update = now
        if has_ask:
            self.last_ask_update = now
        self.last_update = now

    def _merge_level(
        self, levels: list[BookLevel], price: float, size: float, reverse: bool
    ):
        """Insert, update, or remove a price level in a sorted list."""
        # Find existing level at this price
        for i, level in enumerate(levels):
            if level.price == price:
                if size == 0:
                    levels.pop(i)
                else:
                    levels[i] = BookLevel(price=price, size=size)
                return

        # Not found — insert in sorted position (skip if size 0)
        if size == 0:
            return

        new_level = BookLevel(price=price, size=size)
        # Bids: descending (highest first). Asks: ascending (lowest first).
        for i, level in enumerate(levels):
            if reverse and price > level.price:
                levels.insert(i, new_level)
                return
            if not reverse and price < level.price:
                levels.insert(i, new_level)
                return
        levels.append(new_level)

    def reset(self):
        """Clear all levels. Called on token ID change."""
        self.bids.clear()
        self.asks.clear()
        self.last_update = 0.0
        self.last_bid_update = 0.0
        self.last_ask_update = 0.0


@dataclass
class AssetState:
    """Complete observable state for one asset."""

    asset: str
    chainlink: PriceTick | None = None
    binance: PriceTick | None = None

    # Per-timeframe order books (keyed by timeframe, e.g. "5m", "15m")
    up_books: dict[str, TokenBook] = field(default_factory=dict)
    down_books: dict[str, TokenBook] = field(default_factory=dict)

    # Token IDs for the current active markets (keyed by timeframe)
    up_token_ids: dict[str, str] = field(default_factory=dict)
    down_token_ids: dict[str, str] = field(default_factory=dict)

    def get_book(self, timeframe: str) -> tuple[TokenBook, TokenBook]:
        """Get (up_book, down_book) for a specific timeframe."""
        up = self.up_books.get(timeframe)
        if up is None:
            up = TokenBook()
            self.up_books[timeframe] = up
        down = self.down_books.get(timeframe)
        if down is None:
            down = TokenBook()
            self.down_books[timeframe] = down
        return up, down

    def update_chainlink(self, price: float, timestamp_ms: int):
        self.chainlink = PriceTick(price=price, timestamp_ms=timestamp_ms)

    def update_binance(self, price: float, timestamp_ms: int):
        self.binance = PriceTick(price=price, timestamp_ms=timestamp_ms)

    def update_book(self, token_id: str, bids: list[BookLevel], asks: list[BookLevel]):
        """Replace the full order book for a token (snapshot). Matches token_id to up/down."""
        now = time.time()
        book = TokenBook(
            bids=bids, asks=asks,
            last_update=now, last_bid_update=now, last_ask_update=now,
        )
        for tf, tid in self.up_token_ids.items():
            if tid == token_id:
                self.up_books[tf] = book
                return
        for tf, tid in self.down_token_ids.items():
            if tid == token_id:
                self.down_books[tf] = book
                return

    def update_book_level(self, token_id: str, side: str, price: float, size: float):
        """Merge a single level change into the book for a token."""
        for tf, tid in self.up_token_ids.items():
            if tid == token_id:
                self.up_books.setdefault(tf, TokenBook()).apply_level(side, price, size)
                return
        for tf, tid in self.down_token_ids.items():
            if tid == token_id:
                self.down_books.setdefault(tf, TokenBook()).apply_level(side, price, size)
                return

    def update_book_levels_batch(
        self, token_id: str, changes: list[tuple[str, float, float]]
    ):
        """Apply a batch of level changes atomically for a token."""
        for tf, tid in self.up_token_ids.items():
            if tid == token_id:
                self.up_books.setdefault(tf, TokenBook()).apply_levels_batch(changes)
                return
        for tf, tid in self.down_token_ids.items():
            if tid == token_id:
                self.down_books.setdefault(tf, TokenBook()).apply_levels_batch(changes)
                return

    def set_token_ids(self, timeframe: str, up_id: str, down_id: str):
        """Set token IDs and reset the timeframe's books when tokens change."""
        old_up = self.up_token_ids.get(timeframe)
        old_down = self.down_token_ids.get(timeframe)
        if up_id != old_up:
            self.up_books[timeframe] = TokenBook()
        if down_id != old_down:
            self.down_books[timeframe] = TokenBook()
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
        """Route full book snapshot to the correct asset by token ID."""
        for state in self._states.values():
            all_ids = state.get_all_token_ids()
            if token_id in all_ids:
                state.update_book(token_id, bids, asks)
                return

    def update_book_level(self, token_id: str, side: str, price: float, size: float):
        """Route a single level change to the correct asset by token ID."""
        for state in self._states.values():
            all_ids = state.get_all_token_ids()
            if token_id in all_ids:
                state.update_book_level(token_id, side, price, size)
                return

    def update_book_levels_batch(
        self, token_id: str, changes: list[tuple[str, float, float]]
    ):
        """Route a batch of level changes to the correct asset by token ID."""
        for state in self._states.values():
            all_ids = state.get_all_token_ids()
            if token_id in all_ids:
                state.update_book_levels_batch(token_id, changes)
                return
