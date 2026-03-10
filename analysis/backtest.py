"""Backtest engine for the sigmoid confidence trading formula.

Replays the formula second-by-second against historical snapshot data,
simulating entries, flips, and settlement to compute PnL after fees.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.special import expit as sigmoid

from analysis.data_loader import TIMEFRAME_SETTINGS


# ── Configuration ─────────────────────────────────────────────────────────

@dataclass
class FormulaParams:
    """Parameters for the sigmoid confidence formula."""
    a: float = 5.0       # delta sensitivity
    b: float = 3.0       # time elapsed weight
    offset: float = 4.0  # sigmoid centering
    flip_threshold: float = 0.6  # confidence threshold for flip


@dataclass
class GateConfig:
    """Gate conditions that filter which seconds are eligible for entry."""
    min_token_price: float = 0.65
    max_token_price: float = 0.85
    min_depth: float = 10.0        # minimum depth at best ask
    require_live_book: bool = True
    require_live_chainlink: bool = True


# ── Results ───────────────────────────────────────────────────────────────

@dataclass
class Trade:
    """A single entry or flip within an interval."""
    timestamp: int
    seconds_into_interval: int
    direction: str           # "up" or "down"
    token_price: float       # ask price at entry
    fee: float               # fee paid
    confidence: float        # formula confidence at entry
    delta: float             # delta at entry
    is_flip: bool = False


@dataclass
class IntervalResult:
    """Result of backtesting one interval."""
    interval_id: str
    asset: str
    timeframe: str
    resolution: str          # actual outcome
    trades: list[Trade] = field(default_factory=list)
    pnl: float = 0.0        # net PnL after fees
    gross_pnl: float = 0.0  # PnL before fees
    fees_paid: float = 0.0
    skipped: bool = False
    skip_reason: str = ""

    @property
    def entered(self) -> bool:
        return len(self.trades) > 0

    @property
    def win(self) -> bool | None:
        if not self.entered:
            return None
        return self.pnl > 0

    @property
    def final_direction(self) -> str | None:
        if not self.trades:
            return None
        return self.trades[-1].direction


@dataclass
class BacktestResult:
    """Aggregated results across all intervals."""
    intervals: list[IntervalResult]
    params: FormulaParams
    gates: GateConfig
    timeframe: str

    @property
    def entered_intervals(self) -> list[IntervalResult]:
        return [r for r in self.intervals if r.entered]

    @property
    def wins(self) -> list[IntervalResult]:
        return [r for r in self.entered_intervals if r.win]

    @property
    def losses(self) -> list[IntervalResult]:
        return [r for r in self.entered_intervals if r.win is False]

    @property
    def total_pnl(self) -> float:
        return sum(r.pnl for r in self.entered_intervals)

    @property
    def total_fees(self) -> float:
        return sum(r.fees_paid for r in self.entered_intervals)

    @property
    def win_rate(self) -> float:
        entered = self.entered_intervals
        if not entered:
            return 0.0
        return len(self.wins) / len(entered)

    @property
    def profit_factor(self) -> float:
        gains = sum(r.pnl for r in self.wins)
        losses_abs = abs(sum(r.pnl for r in self.losses))
        if losses_abs == 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses_abs

    def summary(self) -> dict:
        entered = self.entered_intervals
        return {
            "total_intervals": len(self.intervals),
            "entered": len(entered),
            "wins": len(self.wins),
            "losses": len(self.losses),
            "win_rate": self.win_rate,
            "total_pnl": self.total_pnl,
            "total_fees": self.total_fees,
            "profit_factor": self.profit_factor,
            "avg_pnl_per_entry": self.total_pnl / len(entered) if entered else 0.0,
            "avg_confidence_at_entry": (
                np.mean([r.trades[0].confidence for r in entered]) if entered else 0.0
            ),
        }


# ── Core engine ───────────────────────────────────────────────────────────

def compute_fee(price: float) -> float:
    """Taker fee per share at a given token price."""
    return 0.0624 * price * (1 - price)


def backtest_interval(
    snap_df: pd.DataFrame,
    interval_row: pd.Series,
    params: FormulaParams,
    gates: GateConfig,
    timeframe: str,
) -> IntervalResult:
    """Replay the formula second-by-second for one interval.

    snap_df should be pre-filtered to rows for this interval, sorted by timestamp.
    interval_row provides chainlink_open, realized_vol_20, and resolution.
    """
    result = IntervalResult(
        interval_id=interval_row["interval_id"],
        asset=interval_row["asset"],
        timeframe=timeframe,
        resolution=interval_row.get("resolution", "unresolved"),
    )

    # Skip if no resolution
    if result.resolution not in ("up", "down"):
        result.skipped = True
        result.skip_reason = f"unresolved ({result.resolution})"
        return result

    # Skip if no vol
    vol = interval_row.get("realized_vol_20")
    if vol is None or pd.isna(vol) or vol <= 0:
        result.skipped = True
        result.skip_reason = "no realized_vol"
        return result

    chainlink_open = interval_row["chainlink_open"]
    if pd.isna(chainlink_open) or chainlink_open <= 0:
        result.skipped = True
        result.skip_reason = "no chainlink_open"
        return result

    settings = TIMEFRAME_SETTINGS[timeframe]
    duration = settings["duration_s"]
    window = settings["trading_window_s"]
    window_start = duration - window

    current_direction: str | None = None
    has_flipped = False

    for _, row in snap_df.iterrows():
        sec = row["seconds_into_interval"]

        # Only trade during trading window
        if sec < window_start:
            continue

        # Gate: live data
        if gates.require_live_chainlink and row.get("chainlink_source") != "live":
            continue
        if gates.require_live_book and row.get("book_source") != "live":
            continue

        price = row.get("chainlink_price")
        if pd.isna(price) or price <= 0:
            continue

        # Compute delta and confidence
        delta = (price - chainlink_open) / chainlink_open
        abs_delta = abs(delta)
        elapsed = sec - window_start
        window_frac = elapsed / window

        signal = params.a * abs_delta / vol + params.b * window_frac - params.offset
        confidence = float(sigmoid(signal))

        # Determine predicted direction
        predicted_dir = "up" if delta >= 0 else "down"

        if current_direction is None:
            # No position yet — check entry
            token_ask = row["up_token_ask"] if predicted_dir == "up" else row["down_token_ask"]
            depth = row["up_depth_1"] if predicted_dir == "up" else row["down_depth_1"]

            if pd.isna(token_ask) or token_ask <= 0:
                continue

            # Gate checks
            if token_ask < gates.min_token_price or token_ask > gates.max_token_price:
                continue
            if depth < gates.min_depth:
                continue

            fee = compute_fee(token_ask)
            if confidence > token_ask + fee:
                trade = Trade(
                    timestamp=int(row["timestamp"]),
                    seconds_into_interval=sec,
                    direction=predicted_dir,
                    token_price=token_ask,
                    fee=fee,
                    confidence=confidence,
                    delta=delta,
                )
                result.trades.append(trade)
                current_direction = predicted_dir

        elif predicted_dir != current_direction and not has_flipped:
            # Direction reversed — check flip
            if confidence > params.flip_threshold:
                new_dir = predicted_dir
                token_ask = row["up_token_ask"] if new_dir == "up" else row["down_token_ask"]
                depth = row["up_depth_1"] if new_dir == "up" else row["down_depth_1"]

                if pd.isna(token_ask) or token_ask <= 0:
                    continue
                if token_ask < gates.min_token_price or token_ask > gates.max_token_price:
                    continue
                if depth < gates.min_depth:
                    continue

                fee = compute_fee(token_ask)
                trade = Trade(
                    timestamp=int(row["timestamp"]),
                    seconds_into_interval=sec,
                    direction=new_dir,
                    token_price=token_ask,
                    fee=fee,
                    confidence=confidence,
                    delta=delta,
                    is_flip=True,
                )
                result.trades.append(trade)
                current_direction = new_dir
                has_flipped = True

    # Compute PnL
    if result.trades:
        _compute_pnl(result)

    return result


def _compute_pnl(result: IntervalResult) -> None:
    """Compute PnL for an interval with trades.

    Each trade buys tokens at the ask price. On settlement:
    - If the resolution matches the token direction, the token pays $1.00
    - Otherwise the token pays $0.00

    For flips, the original position is abandoned (tokens become worthless
    if resolution doesn't match, or we already hold the right token).
    We simplify: only the final position matters for PnL.
    """
    total_cost = 0.0
    total_fees = 0.0

    # Each trade costs token_price + fee per share
    for trade in result.trades:
        total_cost += trade.token_price
        total_fees += trade.fee

    # Settlement: final direction determines payout
    final_trade = result.trades[-1]
    if final_trade.direction == result.resolution:
        # Token pays $1.00
        payout = 1.0
    else:
        # Token pays $0.00
        payout = 0.0

    # PnL = payout - cost of final position - all fees
    # For simplicity: we model as buying 1 share per entry.
    # With a flip, you lose the first position and enter a new one.
    if len(result.trades) == 1:
        # Simple: buy at ask, settle at 0 or 1
        result.gross_pnl = payout - final_trade.token_price
        result.fees_paid = final_trade.fee
        result.pnl = result.gross_pnl - result.fees_paid
    else:
        # Flip: first position is a loss (pays $0 since we flipped away),
        # second position settles normally.
        first = result.trades[0]
        # First trade: bought token, now abandoned. If it happened to be
        # the right direction we could sell, but we model worst case = $0.
        first_pnl = 0.0 - first.token_price - first.fee

        # Final trade: settles normally
        final_pnl = payout - final_trade.token_price - final_trade.fee

        result.gross_pnl = (0.0 - first.token_price) + (payout - final_trade.token_price)
        result.fees_paid = first.fee + final_trade.fee
        result.pnl = first_pnl + final_pnl


# ── Runner ────────────────────────────────────────────────────────────────

def run_backtest(
    snapshots: pd.DataFrame,
    intervals: pd.DataFrame,
    params: FormulaParams | None = None,
    gates: GateConfig | None = None,
    timeframe: str = "5m",
) -> BacktestResult:
    """Run the backtest across all intervals.

    snapshots: DataFrame with all snapshot rows (from load_all_snapshots)
    intervals: DataFrame with all intervals (from load_all_intervals),
               must include 'resolution' column.
    """
    if params is None:
        params = FormulaParams()
    if gates is None:
        gates = GateConfig()

    results = []
    grouped = snapshots.groupby("interval_id")

    for _, interval_row in intervals.iterrows():
        iid = interval_row["interval_id"]
        if iid not in grouped.groups:
            result = IntervalResult(
                interval_id=iid,
                asset=interval_row["asset"],
                timeframe=timeframe,
                resolution=interval_row.get("resolution", "unresolved"),
                skipped=True,
                skip_reason="no snapshot data",
            )
            results.append(result)
            continue

        snap_df = grouped.get_group(iid).sort_values("seconds_into_interval")
        result = backtest_interval(snap_df, interval_row, params, gates, timeframe)
        results.append(result)

    return BacktestResult(
        intervals=results,
        params=params,
        gates=gates,
        timeframe=timeframe,
    )


def results_to_dataframe(bt: BacktestResult) -> pd.DataFrame:
    """Convert backtest results to a DataFrame for analysis."""
    rows = []
    for r in bt.intervals:
        row = {
            "interval_id": r.interval_id,
            "asset": r.asset,
            "resolution": r.resolution,
            "entered": r.entered,
            "skipped": r.skipped,
            "skip_reason": r.skip_reason,
            "pnl": r.pnl,
            "gross_pnl": r.gross_pnl,
            "fees_paid": r.fees_paid,
            "win": r.win,
            "final_direction": r.final_direction,
            "num_trades": len(r.trades),
        }
        if r.trades:
            first = r.trades[0]
            row["entry_sec"] = first.seconds_into_interval
            row["entry_confidence"] = first.confidence
            row["entry_price"] = first.token_price
            row["entry_delta"] = first.delta
            row["flipped"] = any(t.is_flip for t in r.trades)
        rows.append(row)
    return pd.DataFrame(rows)
