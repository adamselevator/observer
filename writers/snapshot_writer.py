"""
Snapshot writer.
Writes one CSV row per second per asset, combining price and book data.
Handles daily file rotation and buffered writes.
"""

import csv
import io
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from state.market_state import AssetState, MarketState
from state.interval_tracker import IntervalTracker
from clock import get_interval_info

logger = logging.getLogger(__name__)

SNAPSHOT_COLUMNS = [
    "timestamp",
    "timestamp_iso",
    "interval_id",
    "seconds_into_interval",
    "market_phase",
    "chainlink_price",
    "chainlink_tick_age_ms",
    "chainlink_source",
    "binance_price",
    "binance_tick_age_ms",
    "up_token_bid",
    "up_token_ask",
    "up_bid_depth_1",
    "up_bid_depth_2",
    "up_bid_depth_3",
    "up_ask_depth_1",
    "up_ask_depth_2",
    "up_ask_depth_3",
    "up_bid_price_2",
    "up_bid_price_3",
    "up_ask_price_2",
    "up_ask_price_3",
    "down_token_bid",
    "down_token_ask",
    "down_bid_depth_1",
    "down_bid_depth_2",
    "down_bid_depth_3",
    "down_ask_depth_1",
    "down_ask_depth_2",
    "down_ask_depth_3",
    "down_bid_price_2",
    "down_bid_price_3",
    "down_ask_price_2",
    "down_ask_price_3",
    "spread_up",
    "spread_down",
    "up_crossed",
    "down_crossed",
    "up_bid_age_ms",
    "up_ask_age_ms",
    "down_bid_age_ms",
    "down_ask_age_ms",
    "book_source",
]


class SnapshotWriter:
    """
    Writes combined price + book snapshots to CSV files.
    One file per asset per timeframe per day.
    """

    def __init__(self, data_dir: Path, asset_timeframe_pairs: list[tuple[str, str]]):
        self._data_dir = data_dir
        self._pairs = asset_timeframe_pairs

        # Open file handles: (asset, timeframe, date_str) -> file handle
        self._files: dict[tuple[str, str, str], io.TextIOWrapper] = {}
        self._writers: dict[tuple[str, str, str], csv.writer] = {}

        # Write buffer
        self._buffer: list[tuple[tuple[str, str, str], list]] = []
        self._flush_interval_s = 5
        self._last_flush = time.time()

    def write_snapshot(
        self,
        market_state: MarketState,
        interval_tracker: IntervalTracker,
        now: float | None = None,
    ):
        """
        Write one snapshot row per active (asset, timeframe) pair.
        Called once per second from the main loop.
        """
        now = now or time.time()
        now_int = int(now)
        date_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        iso_str = datetime.fromtimestamp(now, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        )

        for asset, timeframe in self._pairs:
            state = market_state.get(asset)
            record = interval_tracker.get_active(asset, timeframe)

            if record is None:
                continue  # No active interval for this pair

            info = get_interval_info(timeframe, now)

            # Chainlink data
            if state.chainlink:
                cl_price = state.chainlink.price
                cl_age = state.chainlink.age_ms
                cl_source = "live"
            else:
                cl_price = ""
                cl_age = -1
                cl_source = "missing"

            # Binance data
            if state.binance:
                bn_price = state.binance.price
                bn_age = state.binance.age_ms
            else:
                bn_price = ""
                bn_age = -1

            # Book data for this specific timeframe
            up_book, down_book = state.get_book(timeframe)
            book_source = "live"

            # Check if book data is present
            if up_book.last_update == 0 or down_book.last_update == 0:
                book_source = "missing"

            up_bid = up_book.best_bid if up_book.best_bid is not None else ""
            up_ask = up_book.best_ask if up_book.best_ask is not None else ""
            down_bid = down_book.best_bid if down_book.best_bid is not None else ""
            down_ask = down_book.best_ask if down_book.best_ask is not None else ""

            up_bid_depths, up_ask_depths = up_book.top_depths(3)
            down_bid_depths, down_ask_depths = down_book.top_depths(3)
            up_bid_prices, up_ask_prices = up_book.top_prices(3)
            down_bid_prices, down_ask_prices = down_book.top_prices(3)

            spread_up = up_book.spread if up_book.spread is not None else ""
            spread_down = down_book.spread if down_book.spread is not None else ""

            # Crossed book flags (bid >= ask = stale/unreliable quote)
            up_crossed = 1 if up_book.is_crossed else 0
            down_crossed = 1 if down_book.is_crossed else 0

            # Per-side staleness (ms since last update for each side)
            up_bid_age, up_ask_age = up_book.side_age_ms
            down_bid_age, down_ask_age = down_book.side_age_ms

            row = [
                now_int,
                iso_str,
                record.interval_id,
                info.seconds_into_interval,
                info.market_phase,
                cl_price,
                cl_age,
                cl_source,
                bn_price,
                bn_age,
                up_bid,
                up_ask,
                up_bid_depths[0],
                up_bid_depths[1] if len(up_bid_depths) > 1 else 0,
                up_bid_depths[2] if len(up_bid_depths) > 2 else 0,
                up_ask_depths[0],
                up_ask_depths[1] if len(up_ask_depths) > 1 else 0,
                up_ask_depths[2] if len(up_ask_depths) > 2 else 0,
                up_bid_prices[1],
                up_bid_prices[2],
                up_ask_prices[1],
                up_ask_prices[2],
                down_bid,
                down_ask,
                down_bid_depths[0],
                down_bid_depths[1] if len(down_bid_depths) > 1 else 0,
                down_bid_depths[2] if len(down_bid_depths) > 2 else 0,
                down_ask_depths[0],
                down_ask_depths[1] if len(down_ask_depths) > 1 else 0,
                down_ask_depths[2] if len(down_ask_depths) > 2 else 0,
                down_bid_prices[1],
                down_bid_prices[2],
                down_ask_prices[1],
                down_ask_prices[2],
                spread_up,
                spread_down,
                up_crossed,
                down_crossed,
                up_bid_age,
                up_ask_age,
                down_bid_age,
                down_ask_age,
                book_source,
            ]

            key = (asset, timeframe, date_str)
            self._buffer.append((key, row))

        # Periodic flush
        if now - self._last_flush >= self._flush_interval_s:
            self.flush()

    def flush(self):
        """Write buffered rows to disk."""
        if not self._buffer:
            return

        for key, row in self._buffer:
            writer = self._get_writer(key)
            writer.writerow(row)

        # Flush file handles
        for fh in self._files.values():
            fh.flush()

        self._buffer.clear()
        self._last_flush = time.time()

    def _get_writer(self, key: tuple[str, str, str]) -> csv.writer:
        """Get or create a CSV writer for the given (asset, timeframe, date) key."""
        if key in self._writers:
            return self._writers[key]

        asset, timeframe, date_str = key
        dir_path = self._data_dir / timeframe / "snapshots"
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"{asset}_{date_str}.csv"

        # Check if file exists (to determine if header is needed)
        write_header = not file_path.exists()

        fh = open(file_path, "a", newline="", buffering=1)
        writer = csv.writer(fh)

        if write_header:
            writer.writerow(SNAPSHOT_COLUMNS)
            logger.info(f"[snapshot] Created {file_path}")

        self._files[key] = fh
        self._writers[key] = writer

        return writer

    def close(self):
        """Close all open file handles."""
        self.flush()
        for fh in self._files.values():
            try:
                fh.close()
            except Exception:
                pass
        self._files.clear()
        self._writers.clear()

    def rotate_date(self, new_date_str: str):
        """Close files from previous date. Called at midnight UTC."""
        old_keys = [
            k for k in self._files if k[2] != new_date_str
        ]
        for key in old_keys:
            fh = self._files.pop(key, None)
            self._writers.pop(key, None)
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
        if old_keys:
            logger.info(f"[snapshot] Rotated {len(old_keys)} files for new date")
