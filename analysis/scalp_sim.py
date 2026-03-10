"""
Polymarket Scalp Strategy Simulation

Runs single-scalp backtest with:
  - Baseline (no stop-loss)
  - Fixed stop-loss
  - Trailing stop

Loads data from both data/ and data/observer-data/ directories,
deduplicates overlapping dates.
"""

import pandas as pd
import numpy as np
import os
import glob
import json
import sys
from dataclasses import dataclass


# ── Fee formula (Polymarket Jan 2026+) ──────────────────────────────────────

FEE_RATE = 0.25
FEE_EXPONENT = 2

def fee(p: float) -> float:
    return FEE_RATE * (p * (1 - p)) ** FEE_EXPONENT


# ── Data loading ────────────────────────────────────────────────────────────

def load_snapshots(tf: str) -> pd.DataFrame:
    """Load snapshots from both data dirs, deduplicate by (asset, date)."""
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
    # Deduplicate: prefer observer-data for overlapping (asset, date)
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
    """Load intervals from both data dirs, deduplicate."""
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
    """Load, merge snapshots with interval outcomes."""
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


# ── Simulation engine ───────────────────────────────────────────────────────

@dataclass
class TradeResult:
    interval_id: str
    entry_sec: int
    entry_ask: float
    entry_cost: float
    pred_dir: str
    outcome: str
    exit_type: str       # "scalp", "stop_loss", "trailing_stop", "hold_win", "hold_loss"
    exit_sec: int | None
    exit_bid: float | None
    pnl: float
    shares: float
    peak_bid: float
    trough_bid: float


def simulate_interval(
    iv_rows: list[dict],
    entry: dict,
    target: float,
    bet_size: float,
    stop_loss: float | None,
    trailing_stop: float | None,
) -> TradeResult:
    """Simulate a single scalp trade within an interval."""
    esec = entry["seconds_into_interval"]
    pdir = entry["pred_dir"]
    ask_p = entry["entry_ask"]
    outcome = entry["outcome"]

    ecost = ask_p + fee(ask_p)
    shares = bet_size / ecost

    peak_bid = 0.0
    trough_bid = 999.0
    peak_net = -999.0  # best net PnL per share seen

    for r in iv_rows:
        if r["seconds_into_interval"] <= esec:
            continue

        bid = r["up_token_bid"] if pdir == "up" else r["down_token_bid"]
        if pd.isna(bid) or bid <= 0:
            continue

        sell_revenue = bid - fee(bid)
        net = sell_revenue - ecost

        if bid > peak_bid:
            peak_bid = bid
        if bid < trough_bid:
            trough_bid = bid
        if net > peak_net:
            peak_net = net

        # Check profit target
        if net >= target:
            return TradeResult(
                interval_id=entry["interval_id"],
                entry_sec=esec, entry_ask=ask_p, entry_cost=ecost,
                pred_dir=pdir, outcome=outcome,
                exit_type="scalp",
                exit_sec=r["seconds_into_interval"],
                exit_bid=bid, pnl=net * shares, shares=shares,
                peak_bid=peak_bid, trough_bid=trough_bid,
            )

        # Check fixed stop-loss (net loss exceeds threshold)
        if stop_loss is not None and net <= -stop_loss:
            return TradeResult(
                interval_id=entry["interval_id"],
                entry_sec=esec, entry_ask=ask_p, entry_cost=ecost,
                pred_dir=pdir, outcome=outcome,
                exit_type="stop_loss",
                exit_sec=r["seconds_into_interval"],
                exit_bid=bid, pnl=net * shares, shares=shares,
                peak_bid=peak_bid, trough_bid=trough_bid,
            )

        # Check trailing stop (drop from peak net)
        if trailing_stop is not None and peak_net > 0 and (peak_net - net) >= trailing_stop:
            return TradeResult(
                interval_id=entry["interval_id"],
                entry_sec=esec, entry_ask=ask_p, entry_cost=ecost,
                pred_dir=pdir, outcome=outcome,
                exit_type="trailing_stop",
                exit_sec=r["seconds_into_interval"],
                exit_bid=bid, pnl=net * shares, shares=shares,
                peak_bid=peak_bid, trough_bid=trough_bid,
            )

    # Hold to resolution
    won = (pdir == "up" and outcome == "up") or (
        pdir == "down" and outcome == "down"
    )
    if won:
        pnl = (1.0 - fee(1.0) - ecost) * shares
        etype = "hold_win"
    else:
        pnl = -ecost * shares
        etype = "hold_loss"

    return TradeResult(
        interval_id=entry["interval_id"],
        entry_sec=esec, entry_ask=ask_p, entry_cost=ecost,
        pred_dir=pdir, outcome=outcome,
        exit_type=etype,
        exit_sec=None, exit_bid=None, pnl=pnl, shares=shares,
        peak_bid=peak_bid, trough_bid=trough_bid if trough_bid < 999 else 0,
    )


def run_backtest(
    snap_iv: pd.DataFrame,
    asset: str,
    target: float,
    bet_size: float,
    gate_lo: float,
    gate_hi: float,
    stop_loss: float | None = None,
    trailing_stop: float | None = None,
) -> list[TradeResult]:
    """Run single-scalp backtest for one asset."""
    a = snap_iv[snap_iv["asset"] == asset].copy()
    if a.empty:
        return []

    a["delta"] = a["chainlink_price"] - a["chainlink_open"]
    a["pred_dir"] = np.where(a["delta"] >= 0, "up", "down")
    a["entry_ask"] = np.where(
        a["pred_dir"] == "up", a["up_token_ask"], a["down_token_ask"]
    )

    # Gate filter
    g = a[(a["entry_ask"] >= gate_lo) & (a["entry_ask"] <= gate_hi)]
    entries = (
        g.sort_values("seconds_into_interval")
        .groupby("interval_id")
        .first()
        .reset_index()
    )

    if entries.empty:
        return []

    results = []
    # Pre-group interval rows for speed
    iv_groups = {
        iid: grp.sort_values("seconds_into_interval").to_dict("records")
        for iid, grp in a.groupby("interval_id")
    }

    for _, e in entries.iterrows():
        iid = e["interval_id"]
        if iid not in iv_groups:
            continue
        tr = simulate_interval(
            iv_groups[iid], e.to_dict(), target, bet_size, stop_loss, trailing_stop
        )
        results.append(tr)

    return results


def summarize(results: list[TradeResult]) -> dict:
    """Summarize a list of trade results."""
    if not results:
        return {}
    n = len(results)
    scalps = [r for r in results if r.exit_type == "scalp"]
    stops = [r for r in results if r.exit_type == "stop_loss"]
    trails = [r for r in results if r.exit_type == "trailing_stop"]
    hold_wins = [r for r in results if r.exit_type == "hold_win"]
    hold_losses = [r for r in results if r.exit_type == "hold_loss"]

    total_pnl = sum(r.pnl for r in results)
    return {
        "trades": n,
        "scalps": len(scalps),
        "stops": len(stops),
        "trails": len(trails),
        "hold_wins": len(hold_wins),
        "hold_losses": len(hold_losses),
        "scalp_pnl": sum(r.pnl for r in scalps),
        "stop_pnl": sum(r.pnl for r in stops),
        "trail_pnl": sum(r.pnl for r in trails),
        "hold_win_pnl": sum(r.pnl for r in hold_wins),
        "hold_loss_pnl": sum(r.pnl for r in hold_losses),
        "total_pnl": total_pnl,
        "per_trade": total_pnl / n,
        "exit_pct": (len(scalps) + len(stops) + len(trails)) / n * 100,
    }


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    GATE_LO, GATE_HI = 0.55, 0.65
    BET = 5.0
    TARGETS = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
    STOP_LOSSES = [None, 0.03, 0.05, 0.08, 0.10, 0.15]
    TRAILING_STOPS = [None, 0.03, 0.05, 0.08, 0.10]
    ASSETS = ["btc", "eth", "sol"]

    for tf in ["5m", "15m"]:
        print(f"\n{'#'*80}")
        print(f"# LOADING {tf} DATA")
        print(f"{'#'*80}")

        snap_iv = prepare_data(tf)
        asset_hours = {}
        for asset in ASSETS:
            n_snaps = len(snap_iv[snap_iv["asset"] == asset])
            hrs = n_snaps / 3600
            asset_hours[asset] = hrs
            n_ivs = snap_iv[snap_iv["asset"] == asset]["interval_id"].nunique()
            print(f"  {asset.upper()}: {n_snaps:,} snapshots = {hrs:.1f}h, {n_ivs} intervals")

        # ── BASELINE (no stop-loss) ──────────────────────────────────────
        print(f"\n{'='*80}")
        print(f"BASELINE — gate {GATE_LO}–{GATE_HI}, no stop-loss ({tf})")
        print(f"{'='*80}")
        print(f"  {'target':>8} {'asset':>5} {'trades':>7} {'scalps':>7} {'hldW':>5} {'hldL':>5} "
              f"{'scalpPnL':>10} {'holdPnL':>10} {'netPnL':>10} {'$/day':>9} {'$/trd':>8}")
        print(f"  {'-'*95}")

        baseline_daily = {}
        for t in TARGETS:
            baseline_daily[t] = {}
            for asset in ASSETS:
                results = run_backtest(snap_iv, asset, t, BET, GATE_LO, GATE_HI)
                s = summarize(results)
                if not s:
                    continue
                sc = 24.0 / asset_hours[asset]
                daily = s["total_pnl"] * sc
                baseline_daily[t][asset] = daily
                print(f"  ${t:>7.2f} {asset.upper():>5} {s['trades']:>7} {s['scalps']:>7} "
                      f"{s['hold_wins']:>5} {s['hold_losses']:>5} "
                      f"{s['scalp_pnl']:>+10.2f} {s['hold_win_pnl']+s['hold_loss_pnl']:>+10.2f} "
                      f"{s['total_pnl']:>+10.2f} {daily:>+9.2f} {s['per_trade']:>+8.4f}")

        # Baseline combined
        print(f"\n  BASELINE COMBINED $/DAY ({tf}):")
        print(f"  {'target':>8} {'BTC':>9} {'ETH':>9} {'SOL':>9} {'TOTAL':>10}")
        print(f"  {'-'*50}")
        for t in TARGETS:
            vals = baseline_daily.get(t, {})
            total = sum(vals.values())
            print(f"  ${t:>7.2f} {vals.get('btc',0):>+9.2f} {vals.get('eth',0):>+9.2f} "
                  f"{vals.get('sol',0):>+9.2f} {total:>+10.2f}")

        # ── STOP-LOSS SWEEP ──────────────────────────────────────────────
        print(f"\n{'='*80}")
        print(f"STOP-LOSS SWEEP — gate {GATE_LO}–{GATE_HI} ({tf})")
        print(f"{'='*80}")

        for t in [0.10, 0.20]:
            print(f"\n  target=${t:.2f}")
            print(f"  {'asset':>5} {'SL':>6} {'trades':>7} {'scalps':>7} {'stops':>6} {'hldW':>5} {'hldL':>5} "
                  f"{'scalpPnL':>10} {'stopPnL':>10} {'holdPnL':>10} {'netPnL':>10} {'$/day':>9}")
            print(f"  {'-'*105}")

            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]
                for sl in STOP_LOSSES:
                    results = run_backtest(snap_iv, asset, t, BET, GATE_LO, GATE_HI, stop_loss=sl)
                    s = summarize(results)
                    if not s:
                        continue
                    daily = s["total_pnl"] * sc
                    sl_label = "none" if sl is None else f"${sl:.2f}"
                    hold_pnl = s["hold_win_pnl"] + s["hold_loss_pnl"]
                    tag = " ◀ BASE" if sl is None else ""
                    print(f"  {asset.upper():>5} {sl_label:>6} {s['trades']:>7} {s['scalps']:>7} "
                          f"{s['stops']:>6} {s['hold_wins']:>5} {s['hold_losses']:>5} "
                          f"{s['scalp_pnl']:>+10.2f} {s['stop_pnl']:>+10.2f} {hold_pnl:>+10.2f} "
                          f"{s['total_pnl']:>+10.2f} {daily:>+9.2f}{tag}")

        # Stop-loss combined summary
        print(f"\n  STOP-LOSS COMBINED $/DAY ({tf}, target=$0.10):")
        print(f"  {'SL':>8} {'BTC':>9} {'ETH':>9} {'SOL':>9} {'TOTAL':>10} {'vs_base':>10}")
        print(f"  {'-'*55}")
        for sl in STOP_LOSSES:
            vals = {}
            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]
                results = run_backtest(snap_iv, asset, 0.10, BET, GATE_LO, GATE_HI, stop_loss=sl)
                s = summarize(results)
                vals[asset] = s["total_pnl"] * sc if s else 0
            total = sum(vals.values())
            bl = sum(baseline_daily.get(0.10, {}).values())
            sl_label = "none" if sl is None else f"${sl:.2f}"
            diff = total - bl
            print(f"  {sl_label:>8} {vals.get('btc',0):>+9.2f} {vals.get('eth',0):>+9.2f} "
                  f"{vals.get('sol',0):>+9.2f} {total:>+10.2f} {diff:>+10.2f}")

        print(f"\n  STOP-LOSS COMBINED $/DAY ({tf}, target=$0.20):")
        print(f"  {'SL':>8} {'BTC':>9} {'ETH':>9} {'SOL':>9} {'TOTAL':>10} {'vs_base':>10}")
        print(f"  {'-'*55}")
        for sl in STOP_LOSSES:
            vals = {}
            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]
                results = run_backtest(snap_iv, asset, 0.20, BET, GATE_LO, GATE_HI, stop_loss=sl)
                s = summarize(results)
                vals[asset] = s["total_pnl"] * sc if s else 0
            total = sum(vals.values())
            bl = sum(baseline_daily.get(0.20, {}).values())
            sl_label = "none" if sl is None else f"${sl:.2f}"
            diff = total - bl
            print(f"  {sl_label:>8} {vals.get('btc',0):>+9.2f} {vals.get('eth',0):>+9.2f} "
                  f"{vals.get('sol',0):>+9.2f} {total:>+10.2f} {diff:>+10.2f}")

        # ── TRAILING STOP SWEEP ──────────────────────────────────────────
        print(f"\n{'='*80}")
        print(f"TRAILING STOP SWEEP — gate {GATE_LO}–{GATE_HI} ({tf})")
        print(f"  (trailing stop activates only after net PnL goes positive)")
        print(f"{'='*80}")

        for t in [0.10, 0.20]:
            print(f"\n  target=${t:.2f}")
            print(f"  {'asset':>5} {'TS':>6} {'trades':>7} {'scalps':>7} {'trails':>7} {'hldW':>5} {'hldL':>5} "
                  f"{'scalpPnL':>10} {'trailPnL':>10} {'holdPnL':>10} {'netPnL':>10} {'$/day':>9}")
            print(f"  {'-'*110}")

            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]
                for ts in TRAILING_STOPS:
                    results = run_backtest(snap_iv, asset, t, BET, GATE_LO, GATE_HI, trailing_stop=ts)
                    s = summarize(results)
                    if not s:
                        continue
                    daily = s["total_pnl"] * sc
                    ts_label = "none" if ts is None else f"${ts:.2f}"
                    hold_pnl = s["hold_win_pnl"] + s["hold_loss_pnl"]
                    tag = " ◀ BASE" if ts is None else ""
                    print(f"  {asset.upper():>5} {ts_label:>6} {s['trades']:>7} {s['scalps']:>7} "
                          f"{s['trails']:>7} {s['hold_wins']:>5} {s['hold_losses']:>5} "
                          f"{s['scalp_pnl']:>+10.2f} {s['trail_pnl']:>+10.2f} {hold_pnl:>+10.2f} "
                          f"{s['total_pnl']:>+10.2f} {daily:>+9.2f}{tag}")

        # Trailing stop combined summary
        print(f"\n  TRAILING STOP COMBINED $/DAY ({tf}, target=$0.10):")
        print(f"  {'TS':>8} {'BTC':>9} {'ETH':>9} {'SOL':>9} {'TOTAL':>10} {'vs_base':>10}")
        print(f"  {'-'*55}")
        for ts in TRAILING_STOPS:
            vals = {}
            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]
                results = run_backtest(snap_iv, asset, 0.10, BET, GATE_LO, GATE_HI, trailing_stop=ts)
                s = summarize(results)
                vals[asset] = s["total_pnl"] * sc if s else 0
            total = sum(vals.values())
            bl = sum(baseline_daily.get(0.10, {}).values())
            ts_label = "none" if ts is None else f"${ts:.2f}"
            diff = total - bl
            print(f"  {ts_label:>8} {vals.get('btc',0):>+9.2f} {vals.get('eth',0):>+9.2f} "
                  f"{vals.get('sol',0):>+9.2f} {total:>+10.2f} {diff:>+10.2f}")

        print(f"\n  TRAILING STOP COMBINED $/DAY ({tf}, target=$0.20):")
        print(f"  {'TS':>8} {'BTC':>9} {'ETH':>9} {'SOL':>9} {'TOTAL':>10} {'vs_base':>10}")
        print(f"  {'-'*55}")
        for ts in TRAILING_STOPS:
            vals = {}
            for asset in ASSETS:
                sc = 24.0 / asset_hours[asset]
                results = run_backtest(snap_iv, asset, 0.20, BET, GATE_LO, GATE_HI, trailing_stop=ts)
                s = summarize(results)
                vals[asset] = s["total_pnl"] * sc if s else 0
            total = sum(vals.values())
            bl = sum(baseline_daily.get(0.20, {}).values())
            ts_label = "none" if ts is None else f"${ts:.2f}"
            diff = total - bl
            print(f"  {ts_label:>8} {vals.get('btc',0):>+9.2f} {vals.get('eth',0):>+9.2f} "
                  f"{vals.get('sol',0):>+9.2f} {total:>+10.2f} {diff:>+10.2f}")

        # ── COMBINED STOP + TRAILING ─────────────────────────────────────
        print(f"\n{'='*80}")
        print(f"STOP-LOSS + TRAILING STOP COMBINED ({tf}, target=$0.10)")
        print(f"{'='*80}")
        print(f"  {'SL':>6} {'TS':>6} {'BTC':>9} {'ETH':>9} {'SOL':>9} {'TOTAL':>10} {'vs_base':>10}")
        print(f"  {'-'*60}")
        bl = sum(baseline_daily.get(0.10, {}).values())
        for sl in [None, 0.05, 0.10]:
            for ts in [None, 0.03, 0.05]:
                if sl is None and ts is None:
                    continue  # Already shown as baseline
                vals = {}
                for asset in ASSETS:
                    sc = 24.0 / asset_hours[asset]
                    results = run_backtest(snap_iv, asset, 0.10, BET, GATE_LO, GATE_HI,
                                           stop_loss=sl, trailing_stop=ts)
                    s = summarize(results)
                    vals[asset] = s["total_pnl"] * sc if s else 0
                total = sum(vals.values())
                sl_l = "none" if sl is None else f"${sl:.2f}"
                ts_l = "none" if ts is None else f"${ts:.2f}"
                print(f"  {sl_l:>6} {ts_l:>6} {vals.get('btc',0):>+9.2f} {vals.get('eth',0):>+9.2f} "
                      f"{vals.get('sol',0):>+9.2f} {total:>+10.2f} {total-bl:>+10.2f}")


if __name__ == "__main__":
    main()
