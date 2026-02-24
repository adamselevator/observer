"""
Flip-Scalp Strategy Simulation

Strategy: Enter predicted side. If delta flips and the OTHER side's ask enters
the gate range, ALSO enter that side (while still holding the original).
Both positions tracked independently — each can scalp out or hold to resolution.

Key insight: If both are held to resolution, one wins and one loses. The combined
resolution payout is always ~$1.00 but you paid ~$1.20 total entry, so holding
both is a guaranteed net loss. The edge is scalping one or both before resolution.
"""

import pandas as pd
import numpy as np
import os
import glob
import json
from dataclasses import dataclass, field


# ── Fee formula (Polymarket Jan 2026+) ──────────────────────────────────────

FEE_RATE = 0.25
FEE_EXPONENT = 2

def fee(p: float) -> float:
    return FEE_RATE * (p * (1 - p)) ** FEE_EXPONENT


# ── Data loading (reused from scalp_sim.py) ─────────────────────────────────

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


# ── Position tracking ───────────────────────────────────────────────────────

@dataclass
class Position:
    side: str           # "up" or "down"
    entry_sec: int
    entry_ask: float
    entry_cost: float   # ask + fee(ask)
    shares: float
    is_flip: bool       # True if this is the flip position


@dataclass
class TradeResult:
    interval_id: str
    side: str
    is_flip: bool
    entry_sec: int
    entry_ask: float
    exit_type: str      # "scalp", "hold_win", "hold_loss"
    exit_sec: int | None
    pnl: float


@dataclass
class IntervalResult:
    interval_id: str
    asset: str
    outcome: str
    trades: list[TradeResult] = field(default_factory=list)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def had_flip(self) -> bool:
        return any(t.is_flip for t in self.trades)


# ── Simulation engine ───────────────────────────────────────────────────────

def simulate_interval_flip(
    rows: list[dict],
    outcome: str,
    target: float,
    bet_size: float,
    gate_lo: float,
    gate_hi: float,
) -> list[TradeResult]:
    """
    Simulate an interval with flip-scalp logic.

    1. Wait for predicted side's ask to enter gate range → enter position A
    2. If delta flips and other side's ask enters gate range → also enter position B
    3. Each position independently: scalp if target hit, else hold to resolution
    """
    results = []
    positions: list[Position] = []  # active positions
    entered_sides: set[str] = set()  # track which sides we've entered

    for r in rows:
        sec = r["seconds_into_interval"]
        delta = r.get("delta", 0)
        if pd.isna(delta):
            continue

        # Determine predicted direction from delta
        pred_dir = "up" if delta >= 0 else "down"

        up_ask = r.get("up_token_ask", float("nan"))
        down_ask = r.get("down_token_ask", float("nan"))
        up_bid = r.get("up_token_bid", float("nan"))
        down_bid = r.get("down_token_bid", float("nan"))

        # Check existing positions for scalp exit
        still_active = []
        for pos in positions:
            bid = up_bid if pos.side == "up" else down_bid
            if pd.isna(bid) or bid <= 0:
                still_active.append(pos)
                continue

            sell_revenue = bid - fee(bid)
            net_per_share = sell_revenue - pos.entry_cost

            if net_per_share >= target:
                results.append(TradeResult(
                    interval_id=r["interval_id"],
                    side=pos.side,
                    is_flip=pos.is_flip,
                    entry_sec=pos.entry_sec,
                    entry_ask=pos.entry_ask,
                    exit_type="scalp",
                    exit_sec=sec,
                    pnl=net_per_share * pos.shares,
                ))
            else:
                still_active.append(pos)

        positions = still_active

        # Primary entry: first position only, no entries yet
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

        # Flip entry: already have a primary position, delta flipped,
        # and the NEW predicted side's ask is in gate range
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

    # Resolve remaining positions
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
            side=pos.side,
            is_flip=pos.is_flip,
            entry_sec=pos.entry_sec,
            entry_ask=pos.entry_ask,
            exit_type=etype,
            exit_sec=None,
            pnl=pnl,
        ))

    return results


def run_flip_backtest(
    snap_iv: pd.DataFrame,
    asset: str,
    target: float,
    bet_size: float,
    gate_lo: float,
    gate_hi: float,
) -> list[IntervalResult]:
    """Run flip-scalp backtest for one asset."""
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

        # Only process intervals where primary entry is possible
        # (at least one row with predicted side ask in gate range)
        has_entry = False
        for r in rows:
            delta = r.get("delta", 0)
            if pd.isna(delta):
                continue
            pred = "up" if delta >= 0 else "down"
            ask = r.get(f"{pred}_token_ask", float("nan"))
            if not pd.isna(ask) and gate_lo <= ask <= gate_hi:
                has_entry = True
                break

        if not has_entry:
            continue

        trades = simulate_interval_flip(rows, outcome, target, bet_size, gate_lo, gate_hi)
        if trades:
            results.append(IntervalResult(
                interval_id=iid, asset=asset, outcome=outcome, trades=trades,
            ))

    return results


# ── Reporting ───────────────────────────────────────────────────────────────

def summarize_flip(intervals: list[IntervalResult]) -> dict:
    if not intervals:
        return {}

    all_trades = [t for iv in intervals for t in iv.trades]
    primary = [t for t in all_trades if not t.is_flip]
    flips = [t for t in all_trades if t.is_flip]

    n_iv = len(intervals)
    n_flip_iv = sum(1 for iv in intervals if iv.had_flip)

    def breakdown(trades):
        scalps = [t for t in trades if t.exit_type == "scalp"]
        hw = [t for t in trades if t.exit_type == "hold_win"]
        hl = [t for t in trades if t.exit_type == "hold_loss"]
        return {
            "count": len(trades),
            "scalps": len(scalps),
            "hold_wins": len(hw),
            "hold_losses": len(hl),
            "scalp_pnl": sum(t.pnl for t in scalps),
            "hold_win_pnl": sum(t.pnl for t in hw),
            "hold_loss_pnl": sum(t.pnl for t in hl),
            "total_pnl": sum(t.pnl for t in trades),
        }

    p = breakdown(primary)
    f = breakdown(flips)
    total_pnl = p["total_pnl"] + f["total_pnl"]

    return {
        "intervals": n_iv,
        "flip_intervals": n_flip_iv,
        "flip_rate": n_flip_iv / n_iv * 100 if n_iv else 0,
        "primary": p,
        "flip": f,
        "total_pnl": total_pnl,
        "total_trades": p["count"] + f["count"],
    }


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    GATE_LO, GATE_HI = 0.55, 0.65
    BET = 5.0
    TARGETS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
    ASSETS = ["btc", "eth", "sol"]

    for tf in ["5m", "15m"]:
        print(f"\n{'#'*80}")
        print(f"# FLIP-SCALP SIMULATION — {tf}")
        print(f"{'#'*80}")

        snap_iv = prepare_data(tf)
        asset_hours = {}
        for asset in ASSETS:
            n_snaps = len(snap_iv[snap_iv["asset"] == asset])
            hrs = n_snaps / 3600
            asset_hours[asset] = hrs
            n_ivs = snap_iv[snap_iv["asset"] == asset]["interval_id"].nunique()
            print(f"  {asset.upper()}: {n_snaps:,} snapshots = {hrs:.1f}h, {n_ivs} intervals")

        # ── Baseline comparison (single scalp, no flip) ────────────────────
        print(f"\n{'='*80}")
        print(f"BASELINE vs FLIP-SCALP — gate {GATE_LO}–{GATE_HI} ({tf})")
        print(f"{'='*80}")

        for target in TARGETS:
            print(f"\n  target=${target:.2f}")
            print(f"  {'asset':>5} {'ivs':>5} {'flipIvs':>7} {'flipRt':>6} │ "
                  f"{'1ry_trds':>8} {'1ry_sclp':>8} {'1ry_hW':>6} {'1ry_hL':>6} {'1ry_PnL':>9} │ "
                  f"{'flp_trds':>8} {'flp_sclp':>8} {'flp_hW':>6} {'flp_hL':>6} {'flp_PnL':>9} │ "
                  f"{'netPnL':>9} {'$/day':>9}")
            print(f"  {'-'*140}")

            combined_base = {}
            combined_flip = {}

            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]

                # Flip-scalp
                ivr = run_flip_backtest(snap_iv, asset, target, BET, GATE_LO, GATE_HI)
                s = summarize_flip(ivr)
                if not s:
                    continue

                daily = s["total_pnl"] * sc
                combined_flip[asset] = daily
                p = s["primary"]
                f = s["flip"]

                print(f"  {asset.upper():>5} {s['intervals']:>5} {s['flip_intervals']:>7} "
                      f"{s['flip_rate']:>5.1f}% │ "
                      f"{p['count']:>8} {p['scalps']:>8} {p['hold_wins']:>6} {p['hold_losses']:>6} "
                      f"{p['total_pnl']:>+9.2f} │ "
                      f"{f['count']:>8} {f['scalps']:>8} {f['hold_wins']:>6} {f['hold_losses']:>6} "
                      f"{f['total_pnl']:>+9.2f} │ "
                      f"{s['total_pnl']:>+9.2f} {daily:>+9.2f}")

            # Combined
            total_flip = sum(combined_flip.values())
            print(f"\n  FLIP-SCALP COMBINED: {total_flip:>+.2f} $/day")

        # ── Detailed flip analysis ──────────────────────────────────────────
        print(f"\n{'='*80}")
        print(f"FLIP TRADE ANALYSIS ({tf})")
        print(f"  When you hold original + enter the flip, what happens?")
        print(f"{'='*80}")

        for target in [0.10, 0.20]:
            print(f"\n  target=${target:.2f}")
            print(f"  {'asset':>5} │ {'both_scalp':>10} {'1ry_sclp+flp_hold':>18} "
                  f"{'1ry_hold+flp_sclp':>18} {'both_hold':>10} │ "
                  f"{'flip_net':>9} {'flip_avg':>9}")
            print(f"  {'-'*110}")

            for asset in ASSETS:
                ivr = run_flip_backtest(snap_iv, asset, target, BET, GATE_LO, GATE_HI)
                flip_ivs = [iv for iv in ivr if iv.had_flip]

                both_scalp = 0
                pri_scalp_flip_hold = 0
                pri_hold_flip_scalp = 0
                both_hold = 0
                flip_pnls = []

                for iv in flip_ivs:
                    pri = [t for t in iv.trades if not t.is_flip]
                    flp = [t for t in iv.trades if t.is_flip]
                    if not pri or not flp:
                        continue

                    pri_scalped = any(t.exit_type == "scalp" for t in pri)
                    flp_scalped = any(t.exit_type == "scalp" for t in flp)

                    if pri_scalped and flp_scalped:
                        both_scalp += 1
                    elif pri_scalped and not flp_scalped:
                        pri_scalp_flip_hold += 1
                    elif not pri_scalped and flp_scalped:
                        pri_hold_flip_scalp += 1
                    else:
                        both_hold += 1

                    flip_pnl = sum(t.pnl for t in flp)
                    flip_pnls.append(flip_pnl)

                n_flip = len(flip_ivs)
                total_flip_pnl = sum(flip_pnls)
                avg_flip_pnl = total_flip_pnl / n_flip if n_flip else 0

                print(f"  {asset.upper():>5} │ {both_scalp:>10} {pri_scalp_flip_hold:>18} "
                      f"{pri_hold_flip_scalp:>18} {both_hold:>10} │ "
                      f"{total_flip_pnl:>+9.2f} {avg_flip_pnl:>+9.4f}")

        # ── Summary comparison ──────────────────────────────────────────────
        print(f"\n{'='*80}")
        print(f"COMPARISON: SINGLE SCALP vs FLIP-SCALP — $/day ({tf})")
        print(f"{'='*80}")
        print(f"  {'target':>8} │ {'strategy':>15} {'BTC':>9} {'ETH':>9} {'SOL':>9} {'TOTAL':>10}")
        print(f"  {'-'*65}")

        for target in TARGETS:
            # Single scalp baseline
            base_vals = {}
            flip_vals = {}
            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]

                # Single scalp (import from scalp_sim logic)
                a_data = snap_iv[snap_iv["asset"] == asset].copy()
                a_data["delta"] = a_data["chainlink_price"] - a_data["chainlink_open"]
                a_data["pred_dir"] = np.where(a_data["delta"] >= 0, "up", "down")
                a_data["entry_ask"] = np.where(
                    a_data["pred_dir"] == "up", a_data["up_token_ask"], a_data["down_token_ask"]
                )
                g = a_data[(a_data["entry_ask"] >= GATE_LO) & (a_data["entry_ask"] <= GATE_HI)]
                entries = g.sort_values("seconds_into_interval").groupby("interval_id").first().reset_index()
                iv_groups = {
                    iid: grp.sort_values("seconds_into_interval").to_dict("records")
                    for iid, grp in a_data.groupby("interval_id")
                }
                total_base_pnl = 0.0
                n_base = 0
                for _, e in entries.iterrows():
                    iid = e["interval_id"]
                    if iid not in iv_groups:
                        continue
                    esec = e["seconds_into_interval"]
                    pdir = e["pred_dir"]
                    ask_p = e["entry_ask"]
                    outcome = e["outcome"]
                    ecost = ask_p + fee(ask_p)
                    shares = BET / ecost

                    scalped = False
                    for r in iv_groups[iid]:
                        if r["seconds_into_interval"] <= esec:
                            continue
                        bid = r["up_token_bid"] if pdir == "up" else r["down_token_bid"]
                        if pd.isna(bid) or bid <= 0:
                            continue
                        sell_rev = bid - fee(bid)
                        net = sell_rev - ecost
                        if net >= target:
                            total_base_pnl += net * shares
                            scalped = True
                            break

                    if not scalped:
                        won = (pdir == "up" and outcome == "up") or (pdir == "down" and outcome == "down")
                        if won:
                            total_base_pnl += (1.0 - fee(1.0) - ecost) * shares
                        else:
                            total_base_pnl += -ecost * shares
                    n_base += 1

                base_vals[asset] = total_base_pnl * sc

                # Flip scalp
                ivr = run_flip_backtest(snap_iv, asset, target, BET, GATE_LO, GATE_HI)
                flip_total = sum(iv.total_pnl for iv in ivr)
                flip_vals[asset] = flip_total * sc

            base_total = sum(base_vals.values())
            flip_total = sum(flip_vals.values())
            diff = flip_total - base_total

            print(f"  ${target:>7.2f} │ {'single':>15} {base_vals.get('btc',0):>+9.2f} "
                  f"{base_vals.get('eth',0):>+9.2f} {base_vals.get('sol',0):>+9.2f} "
                  f"{base_total:>+10.2f}")
            print(f"  {'':>8} │ {'flip-scalp':>15} {flip_vals.get('btc',0):>+9.2f} "
                  f"{flip_vals.get('eth',0):>+9.2f} {flip_vals.get('sol',0):>+9.2f} "
                  f"{flip_total:>+10.2f}")
            print(f"  {'':>8} │ {'delta':>15} {'':>9} {'':>9} {'':>9} "
                  f"{diff:>+10.2f}")
            print(f"  {'-'*65}")


if __name__ == "__main__":
    main()
