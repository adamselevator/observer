"""
Time-Based Force Exit Simulation

Tests force-exiting positions when a certain amount of time remains in the
interval, instead of holding to resolution. Applies to both single-scalp
and flip-scalp strategies.

The idea: instead of losing $5.00 on a hold-loss, sell at current bid
with time remaining to recover partial value.
"""

import pandas as pd
import numpy as np
import os
import glob
import json
from dataclasses import dataclass, field


# ── Fee formula ─────────────────────────────────────────────────────────────

FEE_RATE = 0.25
FEE_EXPONENT = 2

def fee(p: float) -> float:
    return FEE_RATE * (p * (1 - p)) ** FEE_EXPONENT


# ── Data loading ────────────────────────────────────────────────────────────

def load_snapshots(tf: str) -> pd.DataFrame:
    frames = []
    for base_dir in ["data", "data/observer-data"]:
        pattern = f"{base_dir}/{tf}/snapshots/*.csv"
        for f in sorted(glob.glob(pattern)):
            asset = os.path.basename(f).split("_")[0]
            date = os.path.basename(f).split("_")[1].replace(".csv", "")
            df = pd.read_csv(f)
            df["asset"] = asset
            df["_date"] = date
            df["_source"] = base_dir
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined["_prio"] = combined["_source"].map(
        {"data/observer-data": 1, "data": 0}
    )
    combined = combined.sort_values("_prio", ascending=False)
    combined = combined.drop_duplicates(
        subset=["asset", "_date", "interval_id", "seconds_into_interval"],
        keep="first",
    )
    combined = combined.drop(columns=["_date", "_source", "_prio"])
    return combined.sort_values(["asset", "timestamp"]).reset_index(drop=True)


def load_intervals(tf: str) -> pd.DataFrame:
    frames = []
    for base_dir in ["data", "data/observer-data"]:
        pattern = f"{base_dir}/{tf}/intervals/*.jsonl"
        for f in sorted(glob.glob(pattern)):
            records = [json.loads(line) for line in open(f)]
            if records:
                df = pd.DataFrame(records)
                df["_source"] = base_dir
                frames.append(df)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined["_prio"] = combined["_source"].map(
        {"data/observer-data": 1, "data": 0}
    )
    combined = combined.sort_values("_prio", ascending=False)
    combined = combined.drop_duplicates(
        subset=["interval_id", "type"], keep="first"
    )
    combined = combined.drop(columns=["_source", "_prio"])
    return combined.reset_index(drop=True)


def prepare_data(tf: str) -> pd.DataFrame:
    snaps = load_snapshots(tf)
    ivs = load_intervals(tf)
    sums = ivs[ivs["type"] == "summary"].copy()
    res = ivs[ivs["type"] == "resolution"][["interval_id", "resolution"]].rename(
        columns={"resolution": "res_out"}
    )
    mi = sums.merge(res, on="interval_id", how="left")
    mi["outcome"] = mi["resolution"].where(
        mi["resolution"].isin(["up", "down"]), mi.get("res_out")
    )
    mi = mi.dropna(subset=["outcome"])
    mi = mi[mi["outcome"].isin(["up", "down"])]
    snap_iv = snaps.merge(
        mi[["interval_id", "chainlink_open", "outcome"]],
        on="interval_id",
        how="inner",
    )
    return snap_iv


# ── Trade result ────────────────────────────────────────────────────────────

@dataclass
class TradeResult:
    interval_id: str
    side: str
    is_flip: bool
    entry_sec: int
    entry_ask: float
    entry_cost: float
    shares: float
    exit_type: str      # "scalp", "time_exit", "hold_win", "hold_loss"
    exit_sec: int | None
    exit_bid: float | None
    pnl: float


# ── Single scalp with time exit ─────────────────────────────────────────────

def simulate_single_scalp(
    rows: list[dict],
    outcome: str,
    target: float,
    bet_size: float,
    gate_lo: float,
    gate_hi: float,
    interval_dur: int,
    force_exit_remaining: int | None,
) -> TradeResult | None:
    """Single scalp with optional time-based force exit."""
    entered = False
    entry_sec = 0
    entry_ask = 0.0
    entry_cost = 0.0
    shares = 0.0
    pred_dir = ""
    force_exit_sec = interval_dur - force_exit_remaining if force_exit_remaining else None

    for r in rows:
        sec = r["seconds_into_interval"]
        delta = r.get("delta", 0)
        if pd.isna(delta):
            continue

        pdir = "up" if delta >= 0 else "down"

        if not entered:
            ask = r["up_token_ask"] if pdir == "up" else r["down_token_ask"]
            if pd.isna(ask) or ask < gate_lo or ask > gate_hi:
                continue
            entry_sec = sec
            entry_ask = ask
            entry_cost = ask + fee(ask)
            shares = bet_size / entry_cost
            pred_dir = pdir
            entered = True
            continue

        # Check scalp target
        bid = r["up_token_bid"] if pred_dir == "up" else r["down_token_bid"]
        if pd.isna(bid) or bid <= 0:
            # Check time exit even without bid
            if force_exit_sec is not None and sec >= force_exit_sec:
                return TradeResult(
                    interval_id=r["interval_id"], side=pred_dir, is_flip=False,
                    entry_sec=entry_sec, entry_ask=entry_ask, entry_cost=entry_cost,
                    shares=shares, exit_type="time_exit", exit_sec=sec,
                    exit_bid=0, pnl=-entry_cost * shares,
                )
            continue

        sell_revenue = bid - fee(bid)
        net = sell_revenue - entry_cost

        if net >= target:
            return TradeResult(
                interval_id=r["interval_id"], side=pred_dir, is_flip=False,
                entry_sec=entry_sec, entry_ask=entry_ask, entry_cost=entry_cost,
                shares=shares, exit_type="scalp", exit_sec=sec,
                exit_bid=bid, pnl=net * shares,
            )

        # Check time-based force exit
        if force_exit_sec is not None and sec >= force_exit_sec:
            return TradeResult(
                interval_id=r["interval_id"], side=pred_dir, is_flip=False,
                entry_sec=entry_sec, entry_ask=entry_ask, entry_cost=entry_cost,
                shares=shares, exit_type="time_exit", exit_sec=sec,
                exit_bid=bid, pnl=net * shares,
            )

    if not entered:
        return None

    # Hold to resolution
    won = (pred_dir == outcome)
    if won:
        pnl = (1.0 - fee(1.0) - entry_cost) * shares
        return TradeResult(
            interval_id=rows[0]["interval_id"], side=pred_dir, is_flip=False,
            entry_sec=entry_sec, entry_ask=entry_ask, entry_cost=entry_cost,
            shares=shares, exit_type="hold_win", exit_sec=None,
            exit_bid=None, pnl=pnl,
        )
    else:
        return TradeResult(
            interval_id=rows[0]["interval_id"], side=pred_dir, is_flip=False,
            entry_sec=entry_sec, entry_ask=entry_ask, entry_cost=entry_cost,
            shares=shares, exit_type="hold_loss", exit_sec=None,
            exit_bid=None, pnl=-entry_cost * shares,
        )


# ── Flip scalp with time exit ──────────────────────────────────────────────

@dataclass
class Position:
    side: str
    entry_sec: int
    entry_ask: float
    entry_cost: float
    shares: float
    is_flip: bool


def simulate_flip_scalp(
    rows: list[dict],
    outcome: str,
    target: float,
    bet_size: float,
    gate_lo: float,
    gate_hi: float,
    interval_dur: int,
    force_exit_remaining: int | None,
) -> list[TradeResult]:
    """Flip-scalp with optional time-based force exit."""
    results = []
    positions: list[Position] = []
    entered_sides: set[str] = set()
    force_exit_sec = interval_dur - force_exit_remaining if force_exit_remaining else None

    for r in rows:
        sec = r["seconds_into_interval"]
        delta = r.get("delta", 0)
        if pd.isna(delta):
            continue

        pred_dir = "up" if delta >= 0 else "down"

        up_ask = r.get("up_token_ask", float("nan"))
        down_ask = r.get("down_token_ask", float("nan"))
        up_bid = r.get("up_token_bid", float("nan"))
        down_bid = r.get("down_token_bid", float("nan"))

        # Check existing positions for scalp exit or time exit
        still_active = []
        for pos in positions:
            bid = up_bid if pos.side == "up" else down_bid

            # Time-based force exit
            if force_exit_sec is not None and sec >= force_exit_sec:
                if pd.isna(bid) or bid <= 0:
                    # No bid available, total loss
                    results.append(TradeResult(
                        interval_id=r["interval_id"], side=pos.side,
                        is_flip=pos.is_flip, entry_sec=pos.entry_sec,
                        entry_ask=pos.entry_ask, entry_cost=pos.entry_cost,
                        shares=pos.shares, exit_type="time_exit", exit_sec=sec,
                        exit_bid=0, pnl=-pos.entry_cost * pos.shares,
                    ))
                else:
                    sell_revenue = bid - fee(bid)
                    net = sell_revenue - pos.entry_cost
                    results.append(TradeResult(
                        interval_id=r["interval_id"], side=pos.side,
                        is_flip=pos.is_flip, entry_sec=pos.entry_sec,
                        entry_ask=pos.entry_ask, entry_cost=pos.entry_cost,
                        shares=pos.shares, exit_type="time_exit", exit_sec=sec,
                        exit_bid=bid, pnl=net * pos.shares,
                    ))
                continue

            if pd.isna(bid) or bid <= 0:
                still_active.append(pos)
                continue

            sell_revenue = bid - fee(bid)
            net = sell_revenue - pos.entry_cost

            if net >= target:
                results.append(TradeResult(
                    interval_id=r["interval_id"], side=pos.side,
                    is_flip=pos.is_flip, entry_sec=pos.entry_sec,
                    entry_ask=pos.entry_ask, entry_cost=pos.entry_cost,
                    shares=pos.shares, exit_type="scalp", exit_sec=sec,
                    exit_bid=bid, pnl=net * pos.shares,
                ))
            else:
                still_active.append(pos)

        positions = still_active

        # Primary entry
        if not entered_sides:
            ask = up_ask if pred_dir == "up" else down_ask
            if not pd.isna(ask) and gate_lo <= ask <= gate_hi:
                ecost = ask + fee(ask)
                shares = bet_size / ecost
                positions.append(Position(
                    side=pred_dir, entry_sec=sec, entry_ask=ask,
                    entry_cost=ecost, shares=shares, is_flip=False,
                ))
                entered_sides.add(pred_dir)

        # Flip entry
        elif len(entered_sides) == 1 and pred_dir not in entered_sides:
            ask = up_ask if pred_dir == "up" else down_ask
            if not pd.isna(ask) and gate_lo <= ask <= gate_hi:
                ecost = ask + fee(ask)
                shares = bet_size / ecost
                positions.append(Position(
                    side=pred_dir, entry_sec=sec, entry_ask=ask,
                    entry_cost=ecost, shares=shares, is_flip=True,
                ))
                entered_sides.add(pred_dir)

    # Resolve remaining positions (hold to resolution)
    for pos in positions:
        won = (pos.side == outcome)
        if won:
            pnl = (1.0 - fee(1.0) - pos.entry_cost) * pos.shares
            etype = "hold_win"
        else:
            pnl = -pos.entry_cost * pos.shares
            etype = "hold_loss"
        results.append(TradeResult(
            interval_id=rows[0]["interval_id"] if rows else "?",
            side=pos.side, is_flip=pos.is_flip, entry_sec=pos.entry_sec,
            entry_ask=pos.entry_ask, entry_cost=pos.entry_cost,
            shares=pos.shares, exit_type=etype, exit_sec=None,
            exit_bid=None, pnl=pnl,
        ))

    return results


# ── Backtest runners ────────────────────────────────────────────────────────

def run_single(
    snap_iv: pd.DataFrame, asset: str, target: float, bet_size: float,
    gate_lo: float, gate_hi: float, interval_dur: int,
    force_exit_remaining: int | None,
) -> list[TradeResult]:
    a = snap_iv[snap_iv["asset"] == asset].copy()
    if a.empty:
        return []
    a["delta"] = a["chainlink_price"] - a["chainlink_open"]
    iv_groups = {
        iid: grp.sort_values("seconds_into_interval").to_dict("records")
        for iid, grp in a.groupby("interval_id")
    }
    outcomes = a.groupby("interval_id")["outcome"].first().to_dict()
    results = []
    for iid, rows in iv_groups.items():
        outcome = outcomes.get(iid)
        if not outcome:
            continue
        tr = simulate_single_scalp(
            rows, outcome, target, bet_size, gate_lo, gate_hi,
            interval_dur, force_exit_remaining,
        )
        if tr:
            results.append(tr)
    return results


def run_flip(
    snap_iv: pd.DataFrame, asset: str, target: float, bet_size: float,
    gate_lo: float, gate_hi: float, interval_dur: int,
    force_exit_remaining: int | None,
) -> list[TradeResult]:
    a = snap_iv[snap_iv["asset"] == asset].copy()
    if a.empty:
        return []
    a["delta"] = a["chainlink_price"] - a["chainlink_open"]
    iv_groups = {
        iid: grp.sort_values("seconds_into_interval").to_dict("records")
        for iid, grp in a.groupby("interval_id")
    }
    outcomes = a.groupby("interval_id")["outcome"].first().to_dict()
    results = []
    for iid, rows in iv_groups.items():
        outcome = outcomes.get(iid)
        if not outcome:
            continue
        trades = simulate_flip_scalp(
            rows, outcome, target, bet_size, gate_lo, gate_hi,
            interval_dur, force_exit_remaining,
        )
        results.extend(trades)
    return results


# ── Summary helpers ─────────────────────────────────────────────────────────

def summarize(results: list[TradeResult]) -> dict:
    if not results:
        return {}
    scalps = [r for r in results if r.exit_type == "scalp"]
    time_exits = [r for r in results if r.exit_type == "time_exit"]
    hold_wins = [r for r in results if r.exit_type == "hold_win"]
    hold_losses = [r for r in results if r.exit_type == "hold_loss"]

    return {
        "trades": len(results),
        "scalps": len(scalps),
        "time_exits": len(time_exits),
        "hold_wins": len(hold_wins),
        "hold_losses": len(hold_losses),
        "scalp_pnl": sum(r.pnl for r in scalps),
        "time_exit_pnl": sum(r.pnl for r in time_exits),
        "hold_win_pnl": sum(r.pnl for r in hold_wins),
        "hold_loss_pnl": sum(r.pnl for r in hold_losses),
        "total_pnl": sum(r.pnl for r in results),
        "avg_time_exit_pnl": (sum(r.pnl for r in time_exits) / len(time_exits))
            if time_exits else 0,
    }


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    GATE_LO, GATE_HI = 0.55, 0.65
    BET = 5.0
    ASSETS = ["btc", "eth", "sol"]

    TF_CONFIG = {
        "5m": {
            "duration": 300,
            "exit_times": [None, 15, 30, 45, 60, 90, 120],
        },
        "15m": {
            "duration": 900,
            "exit_times": [None, 30, 60, 90, 120, 180, 240],
        },
    }

    TARGETS = [0.10, 0.20, 0.30]

    for tf, cfg in TF_CONFIG.items():
        print(f"\n{'#'*80}")
        print(f"# TIME-BASED EXIT SIMULATION — {tf} (interval={cfg['duration']}s)")
        print(f"{'#'*80}")

        snap_iv = prepare_data(tf)
        asset_hours = {}
        for asset in ASSETS:
            n = len(snap_iv[snap_iv["asset"] == asset])
            hrs = n / 3600
            asset_hours[asset] = hrs
            n_ivs = snap_iv[snap_iv["asset"] == asset]["interval_id"].nunique()
            print(f"  {asset.upper()}: {n:,} snapshots = {hrs:.1f}h, {n_ivs} intervals")

        # ── SINGLE SCALP + TIME EXIT ───────────────────────────────────────
        for target in TARGETS:
            print(f"\n{'='*80}")
            print(f"SINGLE SCALP + TIME EXIT — target=${target:.2f} ({tf})")
            print(f"{'='*80}")
            print(f"  {'asset':>5} {'exitAt':>8} {'trades':>6} {'scalps':>6} "
                  f"{'tExit':>5} {'hldW':>5} {'hldL':>5} "
                  f"{'scalpPnL':>10} {'tExitPnL':>10} {'holdPnL':>10} "
                  f"{'netPnL':>10} {'$/day':>9} {'avgTExit':>9}")
            print(f"  {'-'*115}")

            daily_by_exit = {}

            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]
                for fer in cfg["exit_times"]:
                    results = run_single(
                        snap_iv, asset, target, BET, GATE_LO, GATE_HI,
                        cfg["duration"], fer,
                    )
                    s = summarize(results)
                    if not s:
                        continue
                    daily = s["total_pnl"] * sc
                    label = "none" if fer is None else f"{fer}s"
                    hold_pnl = s["hold_win_pnl"] + s["hold_loss_pnl"]
                    tag = " ◀ BASE" if fer is None else ""

                    if fer not in daily_by_exit:
                        daily_by_exit[fer] = {}
                    daily_by_exit[fer][asset] = daily

                    print(f"  {asset.upper():>5} {label:>8} {s['trades']:>6} {s['scalps']:>6} "
                          f"{s['time_exits']:>5} {s['hold_wins']:>5} {s['hold_losses']:>5} "
                          f"{s['scalp_pnl']:>+10.2f} {s['time_exit_pnl']:>+10.2f} "
                          f"{hold_pnl:>+10.2f} "
                          f"{s['total_pnl']:>+10.2f} {daily:>+9.2f} "
                          f"{s['avg_time_exit_pnl']:>+9.4f}{tag}")

            # Combined summary
            print(f"\n  SINGLE SCALP COMBINED $/DAY (target=${target:.2f}, {tf}):")
            print(f"  {'exitAt':>8} {'BTC':>9} {'ETH':>9} {'SOL':>9} {'TOTAL':>10} {'vs_base':>10}")
            print(f"  {'-'*55}")
            base_total = sum(daily_by_exit.get(None, {}).values())
            for fer in cfg["exit_times"]:
                vals = daily_by_exit.get(fer, {})
                total = sum(vals.values())
                label = "none" if fer is None else f"{fer}s"
                diff = total - base_total
                print(f"  {label:>8} {vals.get('btc',0):>+9.2f} {vals.get('eth',0):>+9.2f} "
                      f"{vals.get('sol',0):>+9.2f} {total:>+10.2f} {diff:>+10.2f}")

        # ── FLIP SCALP + TIME EXIT ─────────────────────────────────────────
        for target in TARGETS:
            print(f"\n{'='*80}")
            print(f"FLIP SCALP + TIME EXIT — target=${target:.2f} ({tf})")
            print(f"{'='*80}")
            print(f"  {'asset':>5} {'exitAt':>8} {'trades':>6} {'scalps':>6} "
                  f"{'tExit':>5} {'hldW':>5} {'hldL':>5} "
                  f"{'scalpPnL':>10} {'tExitPnL':>10} {'holdPnL':>10} "
                  f"{'netPnL':>10} {'$/day':>9} {'avgTExit':>9}")
            print(f"  {'-'*115}")

            daily_by_exit = {}

            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]
                for fer in cfg["exit_times"]:
                    results = run_flip(
                        snap_iv, asset, target, BET, GATE_LO, GATE_HI,
                        cfg["duration"], fer,
                    )
                    s = summarize(results)
                    if not s:
                        continue
                    daily = s["total_pnl"] * sc
                    label = "none" if fer is None else f"{fer}s"
                    hold_pnl = s["hold_win_pnl"] + s["hold_loss_pnl"]
                    tag = " ◀ BASE" if fer is None else ""

                    if fer not in daily_by_exit:
                        daily_by_exit[fer] = {}
                    daily_by_exit[fer][asset] = daily

                    print(f"  {asset.upper():>5} {label:>8} {s['trades']:>6} {s['scalps']:>6} "
                          f"{s['time_exits']:>5} {s['hold_wins']:>5} {s['hold_losses']:>5} "
                          f"{s['scalp_pnl']:>+10.2f} {s['time_exit_pnl']:>+10.2f} "
                          f"{hold_pnl:>+10.2f} "
                          f"{s['total_pnl']:>+10.2f} {daily:>+9.2f} "
                          f"{s['avg_time_exit_pnl']:>+9.4f}{tag}")

            # Combined summary
            print(f"\n  FLIP SCALP COMBINED $/DAY (target=${target:.2f}, {tf}):")
            print(f"  {'exitAt':>8} {'BTC':>9} {'ETH':>9} {'SOL':>9} {'TOTAL':>10} {'vs_base':>10}")
            print(f"  {'-'*55}")
            base_total = sum(daily_by_exit.get(None, {}).values())
            for fer in cfg["exit_times"]:
                vals = daily_by_exit.get(fer, {})
                total = sum(vals.values())
                label = "none" if fer is None else f"{fer}s"
                diff = total - base_total
                print(f"  {label:>8} {vals.get('btc',0):>+9.2f} {vals.get('eth',0):>+9.2f} "
                      f"{vals.get('sol',0):>+9.2f} {total:>+10.2f} {diff:>+10.2f}")


if __name__ == "__main__":
    main()
