"""
Interval clock.
Knows the schedule of market intervals, detects boundaries, and computes
timing metadata for each snapshot row.
"""

import time
from dataclasses import dataclass

from config import TIMEFRAME_SETTINGS


@dataclass
class IntervalInfo:
    """Describes where we are within a specific interval."""

    interval_start_ts: int  # Unix timestamp of interval start
    interval_end_ts: int  # Unix timestamp of interval end
    seconds_into_interval: int
    seconds_remaining: int
    duration_s: int
    trading_window_s: int
    in_trading_window: bool
    market_phase: str  # "early", "mid", "late"


def get_current_interval_start(timeframe: str, ts: float | None = None) -> int:
    """Get the start timestamp of the current interval for a given timeframe."""
    duration = TIMEFRAME_SETTINGS[timeframe]["duration_s"]
    now = int(ts or time.time())
    return now - (now % duration)


def get_next_interval_start(timeframe: str, ts: float | None = None) -> int:
    """Get the start timestamp of the next interval."""
    duration = TIMEFRAME_SETTINGS[timeframe]["duration_s"]
    return get_current_interval_start(timeframe, ts) + duration


def get_interval_info(timeframe: str, ts: float | None = None) -> IntervalInfo:
    """Compute full timing context for the current moment within an interval."""
    settings = TIMEFRAME_SETTINGS[timeframe]
    duration = settings["duration_s"]
    trading_window = settings["trading_window_s"]

    now = ts or time.time()
    now_int = int(now)
    start = now_int - (now_int % duration)
    end = start + duration
    elapsed = now_int - start
    remaining = end - now_int

    # Trading window is the last N seconds of the interval
    window_start_offset = duration - trading_window
    in_window = elapsed >= window_start_offset

    # Phase: split interval into thirds
    third = duration / 3
    if elapsed < third:
        phase = "early"
    elif elapsed < 2 * third:
        phase = "mid"
    else:
        phase = "late"

    return IntervalInfo(
        interval_start_ts=start,
        interval_end_ts=end,
        seconds_into_interval=elapsed,
        seconds_remaining=remaining,
        duration_s=duration,
        trading_window_s=trading_window,
        in_trading_window=in_window,
        market_phase=phase,
    )


def make_interval_id(asset: str, timeframe: str, start_ts: int) -> str:
    """Construct a Polymarket-style interval identifier."""
    return f"{asset}-updown-{timeframe}-{start_ts}"


def parse_interval_id(interval_id: str) -> tuple[str, str, int]:
    """Parse interval_id back into (asset, timeframe, start_ts)."""
    parts = interval_id.split("-")
    asset = parts[0]
    timeframe = parts[2]
    start_ts = int(parts[3])
    return asset, timeframe, start_ts


def is_at_boundary(timeframe: str, ts: float | None = None, tolerance_s: int = 2) -> bool:
    """Check if we're within tolerance_s of an interval boundary."""
    duration = TIMEFRAME_SETTINGS[timeframe]["duration_s"]
    now = int(ts or time.time())
    offset = now % duration
    return offset < tolerance_s or offset > (duration - tolerance_s)
