"""
Interval tracker.
Manages the lifecycle of market intervals:
  - Detects new intervals at boundaries
  - Captures open/close prices
  - Tracks high/low during interval
  - Manages pending resolution queue
  - Computes realized volatility
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field

from clock import get_current_interval_start, make_interval_id
from config import RESOLUTION_TIMEOUT_S, VOL_LOOKBACK_INTERVALS

logger = logging.getLogger(__name__)


@dataclass
class IntervalRecord:
    """Complete record for one market interval."""

    interval_id: str
    asset: str
    timeframe: str
    start_ts: int
    end_ts: int

    # Token IDs
    up_token_id: str = ""
    down_token_id: str = ""

    # Chainlink prices
    chainlink_open: float | None = None
    chainlink_close: float | None = None
    chainlink_high: float | None = None
    chainlink_low: float | None = None
    chainlink_high_ts: int | None = None
    chainlink_low_ts: int | None = None

    # Binance prices
    binance_open: float | None = None
    binance_close: float | None = None

    # Chainlink quality
    chainlink_tick_count: int = 0
    chainlink_gap_count: int = 0
    chainlink_max_gap_ms: int = 0
    _last_chainlink_tick_ts: float = field(default=0.0, repr=False)

    # Resolution
    resolution: str | None = None  # "up", "down", or None
    resolved_at: int | None = None

    # Volatility
    realized_vol_20: float | None = None

    # Basis
    open_basis_bps: float | None = None
    close_basis_bps: float | None = None

    @property
    def delta(self) -> float | None:
        if self.chainlink_open and self.chainlink_close:
            return (self.chainlink_close - self.chainlink_open) / self.chainlink_open
        return None

    @property
    def abs_delta(self) -> float | None:
        d = self.delta
        return abs(d) if d is not None else None

    def update_chainlink_tick(self, price: float, timestamp_ms: int):
        """Process a new Chainlink tick during this interval."""
        now = time.time()

        # Open price: first tick at or after interval start
        if self.chainlink_open is None:
            self.chainlink_open = price
            logger.info(
                f"[{self.interval_id}] Chainlink open captured: {price}"
            )

        # High / low tracking
        if self.chainlink_high is None or price > self.chainlink_high:
            self.chainlink_high = price
            self.chainlink_high_ts = timestamp_ms
        if self.chainlink_low is None or price < self.chainlink_low:
            self.chainlink_low = price
            self.chainlink_low_ts = timestamp_ms

        # Gap detection
        if self._last_chainlink_tick_ts > 0:
            gap_ms = int((now - self._last_chainlink_tick_ts) * 1000)
            if gap_ms > 2000:
                self.chainlink_gap_count += 1
                self.chainlink_max_gap_ms = max(self.chainlink_max_gap_ms, gap_ms)

        self._last_chainlink_tick_ts = now
        self.chainlink_tick_count += 1

        # Always update close to latest
        self.chainlink_close = price

    def update_binance_tick(self, price: float):
        """Track Binance open/close for basis comparison."""
        if self.binance_open is None:
            self.binance_open = price
        self.binance_close = price

    def finalize(self):
        """Compute derived fields when interval ends."""
        if (
            self.chainlink_open
            and self.binance_open
            and self.chainlink_open != 0
        ):
            self.open_basis_bps = (
                abs(self.chainlink_open - self.binance_open)
                / self.chainlink_open
                * 10000
            )
        if (
            self.chainlink_close
            and self.binance_close
            and self.chainlink_close != 0
        ):
            self.close_basis_bps = (
                abs(self.chainlink_close - self.binance_close)
                / self.chainlink_close
                * 10000
            )

    def to_dict(self) -> dict:
        """Serialize for JSONL output."""
        return {
            "interval_id": self.interval_id,
            "type": "summary",
            "asset": self.asset,
            "timeframe": self.timeframe,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "up_token_id": self.up_token_id,
            "down_token_id": self.down_token_id,
            "chainlink_open": self.chainlink_open,
            "chainlink_close": self.chainlink_close,
            "chainlink_high": self.chainlink_high,
            "chainlink_low": self.chainlink_low,
            "chainlink_high_ts": self.chainlink_high_ts,
            "chainlink_low_ts": self.chainlink_low_ts,
            "binance_open": self.binance_open,
            "binance_close": self.binance_close,
            "delta": self.delta,
            "abs_delta": self.abs_delta,
            "resolution": self.resolution,
            "resolved_at": self.resolved_at,
            "chainlink_tick_count": self.chainlink_tick_count,
            "chainlink_gap_count": self.chainlink_gap_count,
            "chainlink_max_gap_ms": self.chainlink_max_gap_ms,
            "realized_vol_20": self.realized_vol_20,
            "open_basis_bps": self.open_basis_bps,
            "close_basis_bps": self.close_basis_bps,
        }

    def resolution_dict(self) -> dict:
        """Serialize resolution update for JSONL output."""
        return {
            "interval_id": self.interval_id,
            "type": "resolution",
            "resolution": self.resolution,
            "resolved_at": self.resolved_at,
        }


class IntervalTracker:
    """
    Manages interval lifecycles across all asset/timeframe pairs.
    """

    def __init__(self, asset_timeframe_pairs: list[tuple[str, str]]):
        self._pairs = asset_timeframe_pairs

        # Active intervals: (asset, timeframe) -> IntervalRecord
        self._active: dict[tuple[str, str], IntervalRecord] = {}

        # Pending resolution: interval_id -> IntervalRecord
        self._pending: dict[str, IntervalRecord] = {}

        # Historical deltas for vol computation: (asset, timeframe) -> deque of deltas
        self._delta_history: dict[tuple[str, str], deque] = {
            pair: deque(maxlen=VOL_LOOKBACK_INTERVALS) for pair in asset_timeframe_pairs
        }

        # Callbacks
        self._on_interval_complete: list = []
        self._on_resolution: list = []

    def on_interval_complete(self, callback):
        """Register callback for when an interval completes. callback(IntervalRecord)."""
        self._on_interval_complete.append(callback)

    def on_resolution(self, callback):
        """Register callback for when resolution is determined. callback(IntervalRecord)."""
        self._on_resolution.append(callback)

    def get_active(self, asset: str, timeframe: str) -> IntervalRecord | None:
        return self._active.get((asset, timeframe))

    def get_active_intervals(self) -> list[IntervalRecord]:
        return list(self._active.values())

    def get_pending_intervals(self) -> list[IntervalRecord]:
        return list(self._pending.values())

    def check_boundaries(self, now: float | None = None):
        """
        Check if any intervals have ended or new ones started.
        Called every second from the main loop.
        """
        now = now or time.time()
        now_int = int(now)

        for asset, timeframe in self._pairs:
            key = (asset, timeframe)
            current_start = get_current_interval_start(timeframe, now)
            active = self._active.get(key)

            if active is None:
                # No active interval — start one
                self._start_interval(asset, timeframe, current_start, now)
            elif current_start > active.start_ts:
                # Boundary crossed — close old, start new
                self._end_interval(key, now)
                self._start_interval(asset, timeframe, current_start, now)

    def _start_interval(self, asset: str, timeframe: str, start_ts: int, now: float):
        """Initialize a new interval."""
        from config import TIMEFRAME_SETTINGS

        duration = TIMEFRAME_SETTINGS[timeframe]["duration_s"]
        interval_id = make_interval_id(asset, timeframe, start_ts)

        record = IntervalRecord(
            interval_id=interval_id,
            asset=asset,
            timeframe=timeframe,
            start_ts=start_ts,
            end_ts=start_ts + duration,
            realized_vol_20=self._compute_vol(asset, timeframe),
        )

        self._active[(asset, timeframe)] = record
        logger.info(f"[{interval_id}] Interval started")

    def _end_interval(self, key: tuple[str, str], now: float):
        """Finalize an interval and move to pending."""
        record = self._active.pop(key, None)
        if record is None:
            return

        record.finalize()

        # Update delta history for vol
        if record.delta is not None:
            self._delta_history[key].append(record.delta)

        # Move to pending resolution
        self._pending[record.interval_id] = record
        logger.info(
            f"[{record.interval_id}] Interval ended. "
            f"Delta: {record.delta:.6f}, Ticks: {record.chainlink_tick_count}, "
            f"Gaps: {record.chainlink_gap_count}"
        )

        # Notify callbacks
        for cb in self._on_interval_complete:
            try:
                cb(record)
            except Exception as e:
                logger.error(f"interval_complete callback error: {e}")

    def _compute_vol(self, asset: str, timeframe: str) -> float | None:
        """Compute realized vol from historical deltas."""
        history = self._delta_history.get((asset, timeframe))
        if not history or len(history) < 3:
            return None

        deltas = list(history)
        mean = sum(deltas) / len(deltas)
        variance = sum((d - mean) ** 2 for d in deltas) / len(deltas)
        return variance ** 0.5

    def process_chainlink_tick(self, asset: str, price: float, timestamp_ms: int):
        """Route a Chainlink tick to all active intervals for this asset."""
        for (a, tf), record in self._active.items():
            if a == asset:
                record.update_chainlink_tick(price, timestamp_ms)

    def process_binance_tick(self, asset: str, price: float):
        """Route a Binance tick to all active intervals for this asset."""
        for (a, tf), record in self._active.items():
            if a == asset:
                record.update_binance_tick(price)

    def resolve_interval(self, interval_id: str, resolution: str):
        """Mark a pending interval as resolved."""
        record = self._pending.get(interval_id)
        if record is None:
            logger.warning(f"Resolution for unknown interval: {interval_id}")
            return

        record.resolution = resolution
        record.resolved_at = int(time.time())

        logger.info(f"[{interval_id}] Resolved: {resolution}")

        # Notify callbacks
        for cb in self._on_resolution:
            try:
                cb(record)
            except Exception as e:
                logger.error(f"resolution callback error: {e}")

        # Remove from pending
        del self._pending[interval_id]

    def cleanup_stale_pending(self, now: float | None = None):
        """Remove pending intervals that have exceeded the resolution timeout."""
        now = now or time.time()
        stale = [
            iid
            for iid, record in self._pending.items()
            if now - record.end_ts > RESOLUTION_TIMEOUT_S
        ]
        for iid in stale:
            logger.warning(f"[{iid}] Resolution timeout — marking unresolved")
            record = self._pending.pop(iid)
            record.resolution = "unresolved"
            record.resolved_at = int(now)
            for cb in self._on_resolution:
                try:
                    cb(record)
                except Exception as e:
                    logger.error(f"resolution callback error: {e}")

    def set_token_ids(
        self, asset: str, timeframe: str, up_token_id: str, down_token_id: str
    ):
        """Store token IDs for an active interval."""
        record = self._active.get((asset, timeframe))
        if record:
            record.up_token_id = up_token_id
            record.down_token_id = down_token_id
