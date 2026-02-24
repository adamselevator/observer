"""Load Observer snapshot CSVs and interval JSONLs into pandas DataFrames."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd

# ── Defaults ──────────────────────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

TIMEFRAME_SETTINGS = {
    "5m": {"duration_s": 300, "trading_window_s": 240},
    "15m": {"duration_s": 900, "trading_window_s": 600},
}

SNAPSHOT_NUMERIC_COLS = [
    "chainlink_price", "binance_price",
    "up_token_bid", "up_token_ask", "up_depth_1", "up_depth_2", "up_depth_3",
    "down_token_bid", "down_token_ask", "down_depth_1", "down_depth_2", "down_depth_3",
    "spread_up", "spread_down",
]


# ── Snapshot loading ──────────────────────────────────────────────────────

def load_snapshots(
    asset: str,
    timeframe: str,
    date: str | None = None,
    data_dir: Path = DATA_DIR,
) -> pd.DataFrame:
    """Load snapshot CSV(s) for one asset/timeframe.

    If date is None, loads all available dates and concatenates.
    """
    snap_dir = data_dir / timeframe / "snapshots"
    if date:
        files = [snap_dir / f"{asset}_{date}.csv"]
    else:
        files = sorted(snap_dir.glob(f"{asset}_*.csv"))

    if not files:
        return pd.DataFrame()

    frames = []
    for f in files:
        if not f.exists():
            continue
        df = pd.read_csv(f, low_memory=False)
        for col in SNAPSHOT_NUMERIC_COLS:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["chainlink_tick_age_ms"] = pd.to_numeric(df["chainlink_tick_age_ms"], errors="coerce")
        df["binance_tick_age_ms"] = pd.to_numeric(df["binance_tick_age_ms"], errors="coerce")
        df["asset"] = asset
        df["timeframe"] = timeframe
        frames.append(df)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ── Interval loading ─────────────────────────────────────────────────────

def load_intervals(
    asset: str,
    timeframe: str,
    date: str | None = None,
    data_dir: Path = DATA_DIR,
) -> pd.DataFrame:
    """Load interval JSONL(s) for one asset/timeframe.

    Joins summary + resolution records into one row per interval.
    """
    int_dir = data_dir / timeframe / "intervals"
    if date:
        files = [int_dir / f"{asset}_{date}.jsonl"]
    else:
        files = sorted(int_dir.glob(f"{asset}_*.jsonl"))

    if not files:
        return pd.DataFrame()

    summaries: dict[str, dict] = {}
    resolutions: dict[str, dict] = {}

    for f in files:
        if not f.exists():
            continue
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                iid = record["interval_id"]
                if record["type"] == "summary":
                    summaries[iid] = record
                elif record["type"] == "resolution":
                    resolutions[iid] = record

    if not summaries:
        return pd.DataFrame()

    rows = []
    for iid, summary in summaries.items():
        row = dict(summary)
        row.pop("type", None)
        if iid in resolutions:
            row["resolution"] = resolutions[iid]["resolution"]
            row["resolved_at"] = resolutions[iid].get("resolved_at")
        rows.append(row)

    df = pd.DataFrame(rows)
    df = df.sort_values("start_ts").reset_index(drop=True)
    return df


# ── Convenience loaders ──────────────────────────────────────────────────

def load_all_intervals(
    timeframe: str,
    data_dir: Path = DATA_DIR,
) -> pd.DataFrame:
    """Load intervals for all assets in a timeframe."""
    int_dir = data_dir / timeframe / "intervals"
    if not int_dir.exists():
        return pd.DataFrame()

    assets = sorted({f.stem.split("_")[0] for f in int_dir.glob("*.jsonl")})
    frames = [load_intervals(a, timeframe, data_dir=data_dir) for a in assets]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values("start_ts").reset_index(drop=True)


def load_all_snapshots(
    timeframe: str,
    data_dir: Path = DATA_DIR,
) -> pd.DataFrame:
    """Load snapshots for all assets in a timeframe."""
    snap_dir = data_dir / timeframe / "snapshots"
    if not snap_dir.exists():
        return pd.DataFrame()

    assets = sorted({f.stem.split("_")[0] for f in snap_dir.glob("*.csv")})
    frames = [load_snapshots(a, timeframe, data_dir=data_dir) for a in assets]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def join_snapshots_intervals(
    snapshots: pd.DataFrame,
    intervals: pd.DataFrame,
) -> pd.DataFrame:
    """Join resolution labels onto snapshot rows via interval_id.

    Also merges chainlink_open and realized_vol_20 from intervals,
    which are needed for the formula replay.
    """
    merge_cols = ["interval_id", "resolution", "chainlink_open", "realized_vol_20"]
    available = [c for c in merge_cols if c in intervals.columns]
    merged = snapshots.merge(
        intervals[available].drop_duplicates(subset=["interval_id"]),
        on="interval_id",
        how="left",
        suffixes=("", "_interval"),
    )
    return merged


# ── Derived features ─────────────────────────────────────────────────────

def add_formula_features(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Add columns needed for the trading formula.

    Expects a joined snapshot+interval DataFrame with chainlink_price,
    chainlink_open, realized_vol_20, seconds_into_interval.
    """
    settings = TIMEFRAME_SETTINGS[timeframe]
    duration = settings["duration_s"]
    window = settings["trading_window_s"]
    window_start = duration - window

    df = df.copy()

    # Delta: (current - open) / open
    df["delta"] = (df["chainlink_price"] - df["chainlink_open"]) / df["chainlink_open"]
    df["abs_delta"] = df["delta"].abs()

    # Trading window elapsed
    df["in_trading_window"] = df["seconds_into_interval"] >= window_start
    df["window_elapsed"] = (df["seconds_into_interval"] - window_start).clip(lower=0)
    df["window_fraction"] = df["window_elapsed"] / window

    # Fee at up token ask price
    df["fee_up"] = 0.0624 * df["up_token_ask"] * (1 - df["up_token_ask"])
    df["fee_down"] = 0.0624 * df["down_token_ask"] * (1 - df["down_token_ask"])

    return df
