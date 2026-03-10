# Polymarket Scalp Strategy — Simulation Report

**Date**: 2026-02-22
**Data**: 3 days (2026-02-19 to 2026-02-21), ~31.4 hours per asset
**Assets**: BTC, ETH, SOL (XRP excluded — insufficient price movement)
**Timeframes**: 5m (239–222 intervals per asset), 15m (93–96 intervals per asset)
**Bet size**: $5.00 per trade

---

## 1. Strategy Overview

### Core Idea

Instead of holding Polymarket outcome tokens to binary resolution ($1 or $0), **scalp the token price swing** — buy the predicted side's token when it enters a price range, sell when a small profit target is reached, and only hold to resolution if the target is never hit.

### Entry Logic

1. Compute `delta = chainlink_price - chainlink_open` (dollar move from interval open)
2. Predicted direction: `up` if delta >= 0, `down` otherwise
3. Check if predicted side's ask price is within the **gate range** (e.g., $0.55–$0.65)
4. Optionally require `|delta| >= min_delta` for direction confirmation
5. Buy at ask price + taker fee

### Exit Logic

1. Each subsequent second, check if selling at the bid would yield net profit >= target
2. Net profit per share = `(bid - fee(bid)) - entry_cost`
3. If target hit → sell, pocket profit
4. If interval ends without hitting target → hold to resolution ($1 win or $0 loss)

### Fee Formula

**Current Polymarket taker fee** (as of Jan 2026):
```
fee(price) = 0.25 × (price × (1 - price))²
```

Makers pay zero. This replaced the older `0.0624 × price × (1 - price)` formula. The new formula produces nearly identical fees at p=0.50 but drops off much faster toward the extremes:

| Price | Old Fee | New Fee | Difference |
|-------|---------|---------|------------|
| 0.50 | $0.0156 | $0.0156 | ~same |
| 0.55 | $0.0155 | $0.0153 | 1% lower |
| 0.60 | $0.0150 | $0.0144 | 4% lower |
| 0.65 | $0.0142 | $0.0130 | 9% lower |
| 0.90 | $0.0056 | $0.0020 | 64% lower |
| 0.95 | $0.0030 | $0.0006 | 81% lower |

**Impact**: The old formula slightly overestimated fees. Correcting to the new formula adds ~$5–12/day to combined results in the 0.55–0.65 gate range.

---

## 2. Single-Scalp Results (one entry per interval)

### Configuration
- Gate: 0.55–0.65
- Profit target: $0.10
- Fee: `0.25 × (p(1-p))²`
- $5/trade

### 5m Results

| Asset | Trades | Exits | Exit% | Hold W | Hold L | Exit PnL | Hold PnL | Net PnL | $/day | $/trade |
|-------|--------|-------|-------|--------|--------|----------|----------|---------|-------|---------|
| BTC | 239 | 189 | 79.1% | 31 | 19 | +$255.51 | +$7.57 | +$263.09 | +$200.95 | +$1.10 |
| ETH | 239 | 184 | 77.0% | 29 | 26 | +$259.36 | -$34.69 | +$224.67 | +$171.61 | +$0.94 |
| SOL | 222 | 167 | 75.2% | 25 | 30 | +$244.50 | -$64.54 | +$179.96 | +$137.46 | +$0.81 |
| **Combined** | **700** | **540** | **77.1%** | **85** | **75** | **+$759.37** | **-$91.66** | **+$667.72** | **+$510.02** | **+$0.95** |

### 15m Results

| Asset | Trades | Exits | Exit% | Hold W | Hold L | Exit PnL | Hold PnL | Net PnL | $/day | $/trade |
|-------|--------|-------|-------|--------|--------|----------|----------|---------|-------|---------|
| BTC | 96 | 86 | 89.6% | 7 | 3 | +$104.14 | +$10.75 | +$114.89 | +$87.75 | +$1.20 |
| ETH | 93 | 85 | 91.4% | 8 | 0 | +$117.11 | +$29.23 | +$146.33 | +$111.77 | +$1.57 |
| SOL | 96 | 86 | 89.6% | 5 | 5 | +$115.64 | -$7.88 | +$107.76 | +$82.31 | +$1.12 |
| **Combined** | **285** | **257** | **90.2%** | **20** | **8** | **+$336.89** | **+$32.10** | **+$368.98** | **+$281.84** | **+$1.29** |

### Key Observations
- 15m has higher exit rates (90% vs 77%) and higher $/trade because more time for swings
- 5m has ~2.5x more intervals, producing more total daily profit ($510 vs $282)
- ETH 15m: zero hold losses, 91% exit rate — strongest single slot
- SOL: weakest hold win rates (45–50%), most hold losses
- BTC: most consistent across both timeframes

---

## 3. Profit Target Sweep

### 5m Combined $/day (BTC + ETH + SOL)

| Target | BTC | ETH | SOL | Total | Avg Exit% |
|--------|-----|-----|-----|-------|-----------|
| $0.01 | +$130 | +$107 | +$79 | +$317 | 87.5% |
| $0.03 | +$147 | +$128 | +$97 | +$372 | 85.4% |
| $0.05 | +$157 | +$145 | +$116 | +$418 | 82.3% |
| $0.10 | +$201 | +$172 | +$137 | +$510 | 77.1% |
| $0.15 | +$239 | +$190 | +$161 | +$590 | 68.9% |
| $0.20 | +$281 | +$224 | +$167 | +$672 | 64.1% |
| $0.25 | +$285 | +$223 | +$180 | +$688 | 58.1% |
| **$0.30** | **+$285** | **+$212** | **+$201** | **+$698** | **51.2%** |
| $0.35 | +$224 | +$156 | +$117 | +$498 | 35.0% |

### 15m Combined $/day

| Target | BTC | ETH | SOL | Total | Avg Exit% |
|--------|-----|-----|-----|-------|-----------|
| $0.01 | +$41 | +$40 | +$37 | +$118 | 97.2% |
| $0.03 | +$54 | +$55 | +$51 | +$160 | 96.5% |
| $0.05 | +$72 | +$70 | +$63 | +$205 | 94.8% |
| $0.10 | +$88 | +$112 | +$82 | +$282 | 90.2% |
| $0.15 | +$113 | +$125 | +$109 | +$346 | 87.0% |
| $0.20 | +$138 | +$149 | +$116 | +$403 | 82.8% |
| $0.25 | +$155 | +$172 | +$133 | +$460 | 80.0% |
| **$0.30** | **+$172** | **+$175** | **+$151** | **+$498** | **77.2%** |
| $0.35 | +$151 | +$170 | +$133 | +$453 | 63.5% |

### Per-asset optimal targets
- **BTC**: $0.30 (5m: +$285, 15m: +$172) — flat from $0.20 onward on 5m
- **ETH**: $0.20 on 5m (+$224), $0.30 on 15m (+$175) — more hold losses at higher targets on 5m
- **SOL**: $0.30 (5m: +$201, 15m: +$151) — but worst hold win rate

### Overall peak: $0.30 target → +$1,195/day combined (5m + 15m)

The cliff is at $0.35 where exit rates crash below 60% on 5m and hold losses dominate.

---

## 4. Delta Gate Analysis

Testing whether requiring a minimum |delta| (price move from open) before entry improves results.

### Delta distributions at entry points

| Asset | 5m p25 | 5m p50 | 5m p75 | 15m p25 | 15m p50 | 15m p75 |
|-------|--------|--------|--------|---------|---------|---------|
| BTC | $0.53 | $4.48 | $18.10 | $0.99 | $6.45 | $24.56 |
| ETH | $0.02 | $0.20 | $0.56 | $0.03 | $0.27 | $0.61 |
| SOL | $0.003 | $0.014 | $0.038 | $0.003 | $0.020 | $0.042 |

### Results (single-scalp, $0.10 target)

**15m — best |delta| threshold vs baseline**

| Asset | Baseline $/day | Best Threshold | Best $/day | Improvement |
|-------|---------------|----------------|-----------|-------------|
| BTC | +$86 | |d| >= $3.30 | +$96 | +$10 |
| ETH | +$110 | none needed | +$110 | $0 |
| SOL | +$80 | |d| >= $0.02 | +$95 | +$14 |

**5m — best |delta| threshold vs baseline**

| Asset | Baseline $/day | Best Threshold | Best $/day | Improvement |
|-------|---------------|----------------|-----------|-------------|
| BTC | +$197 | none needed | +$197 | $0 |
| ETH | +$167 | none needed | +$167 | $0 |
| SOL | +$134 | |d| >= $0.014 | +$156 | +$22 |

### Conclusion
Delta gate is a modest filter (+$10–22/day on specific assets). It helps by filtering out near-zero-delta entries that are coin flips when held to resolution. The effect is small because the scalp exit rate is already high.

**Recommended defaults**: BTC |d| >= $3, ETH none, SOL |d| >= $0.02.

---

## 5. Multi-Scalp Results (re-enter every time a side enters gate range)

Instead of one entry per interval, re-enter after each successful exit whenever a token re-enters the gate range.

### 5m Combined $/day

| Target | Single | Multi | Trades/interval | Exit% |
|--------|--------|-------|-----------------|-------|
| $0.03 | +$372 | +$8,214 | ~10 | 97% |
| $0.05 | +$418 | +$7,891 | ~9 | 96% |
| $0.10 | +$510 | +$6,828 | ~7 | 94% |
| $0.15 | +$590 | +$5,666 | ~5 | 91% |
| $0.20 | +$672 | +$4,683 | ~3.5 | 88% |
| $0.25 | +$688 | +$3,712 | ~2.8 | 83% |

### 15m Combined $/day

| Target | Single | Multi | Trades/interval | Exit% |
|--------|--------|-------|-----------------|-------|
| $0.03 | +$160 | +$8,402 | ~27 | 99% |
| $0.05 | +$205 | +$8,087 | ~24 | 99% |
| $0.10 | +$282 | +$6,848 | ~17 | 98% |
| $0.15 | +$346 | +$5,660 | ~12 | 97% |
| $0.20 | +$403 | +$4,707 | ~9 | 96% |
| $0.25 | +$460 | +$3,793 | ~7 | 94% |

### Observations
- Token prices oscillate back into the gate range many times per interval
- 15m edges out 5m because longer windows = more oscillation opportunities
- Peak single-interval trades: 80+ (5m), 160+ (15m) at $0.03 target
- These numbers are theoretical upper bounds — see caveats section

---

## 6. Wider Gate (0.50–0.65) with Delta Confirmation

Dropping gate-low from 0.55 to 0.50 and using |delta| to determine direction (since at p=0.50 both tokens are 50/50).

### Multi-scalp combined $/day

**5m**

| Target | 0.55–0.65 baseline | 0.50–0.65 no filter | 0.50 + p10 |d| |
|--------|-------------------|--------------------|----|
| $0.05 | +$7,891 | +$9,242 | +$9,412 (+19%) |
| $0.10 | +$6,828 | +$8,294 | +$8,323 (+22%) |
| $0.15 | +$5,666 | +$6,893 | +$6,999 (+24%) |
| $0.20 | +$4,683 | +$5,579 | +$5,590 (+19%) |

**15m**

| Target | 0.55–0.65 baseline | 0.50–0.65 no filter | 0.50 + p25 |d| |
|--------|-------------------|--------------------|----|
| $0.05 | +$8,087 | +$11,558 | +$12,465 (+54%) |
| $0.10 | +$6,848 | +$10,014 | +$11,420 (+67%) |
| $0.15 | +$5,660 | +$8,729 | +$9,784 (+73%) |
| $0.20 | +$4,707 | +$7,259 | +$8,474 (+80%) |

### Why it helps
- Cheaper entries at 0.50 = more shares per $5 = more profit per successful scalp
- More entry opportunities (tokens spend time in the 0.50–0.55 zone)
- Delta confirmation prevents blind coin-flip entries
- 15m benefits most because longer intervals have more oscillation through the wider zone

---

## 7. Failed Strategies

### Stop-loss + flip reversal
Tested: if trade goes against you by X, sell at a loss and buy the other side. **Every combo was deeply negative** vs baseline. Reason: most winning trades dip before they rip — stop-losses cut winners prematurely. At $0.05 stop on BTC, 74/96 trades were stopped out.

### Time-based exit
Tested: if trade hasn't hit target within N seconds, sell at market. **Always underperformed baseline.** The "slow" trades are slow winners, not stuck losers. Cutting them early sells at a loss.

### XRP
Excluded entirely. |delta| is essentially zero ($0.001) — no price movement within intervals.

---

## 8. Tunable Parameters

```
--gate-range     Entry price range for predicted side's ask    [0.55, 0.65]
--profit-target  Net profit per share to trigger exit           0.10
--bet-size       USDC per trade                                 5.00
--min-delta      Minimum |delta| for direction confirmation     0 (off)
--multi-scalp    Re-enter within same interval after exit       false
--timeframe      Which intervals to trade                       5m,15m
--asset          Which assets to trade                          btc,eth,sol
```

---

## 9. Economics at $5/trade

### How a typical winning scalp works
```
Entry: ask = $0.59, fee = $0.0146, cost = $0.6046/share
Shares: $5.00 / $0.6046 = 8.27 shares
Exit:   bid = $0.72, fee = $0.0102, revenue = $0.7098/share
Net:    $0.7098 - $0.6046 = $0.1052/share
PnL:    $0.1052 × 8.27 = +$0.87
```

### How a losing hold works
```
Entry cost: $0.6046/share × 8.27 shares = $5.00
Resolution: token goes to $0 (wrong prediction)
PnL: -$5.00 (full bet lost)
```

### How a winning hold works
```
Entry cost: $0.6046/share × 8.27 shares = $5.00
Resolution: token goes to $1, sell revenue = $1.00 - fee($1.00) = $0.9376
PnL: ($0.9376 - $0.6046) × 8.27 = +$2.75
```

### Break-even analysis
At $0.10 target, a single hold loss (-$5.00) requires ~5–6 successful scalp exits (+$0.87 each) to recover. With 77–90% exit rates, this works out positive.

---

## 10. Caveats and Risks

### Data limitations
- **31.4 hours across 3 days** — small sample, single market regime
- **Extrapolated to 24h** assuming uniform activity — overnight/weekend may differ significantly
- Markets may have different volatility profiles on different days or during different macro conditions

### Execution assumptions
- **No slippage**: sim fills at the displayed bid/ask. Real orders may fill worse, especially on thin books
- **No depth check**: sim doesn't verify sufficient depth behind the displayed price
- **No competition**: other bots are trading these same books and may consume liquidity
- **No latency model**: sim reacts instantly to price changes. Real bot has ~60–250ms latency (websocket → decision → REST API order)

### Multi-scalp specific
- Assumes perfect re-entry on every oscillation back into gate range
- Repeated fills on the same book — depth may not support 10+ fills per interval
- 1-second exits may represent fleeting quotes, not real fill opportunities
- Numbers should be treated as theoretical upper bounds

### Strategy risks
- **Hold losses are total losses** (-$5.00 per trade). A regime shift increasing hold losses could erase profits
- **Direction prediction** relies on delta, which can reverse within an interval
- **Market maker behavior** could change — spreads could widen, depth could thin
- **Fee structure could change** — Polymarket has adjusted fees before
- **Regulatory risk** — prediction market regulation is evolving

---

## 11. Summary of Best Configurations

### Conservative (single-scalp)
```
gate:    0.55–0.65
target:  $0.10
assets:  BTC + ETH + SOL
result:  +$510/day (5m) + $282/day (15m) = +$792/day at $5/trade
         77–90% exit rate, 8–75 hold losses across all assets
```

### Balanced (single-scalp, higher target)
```
gate:    0.55–0.65
target:  $0.20
assets:  BTC + ETH + SOL
result:  +$672/day (5m) + $403/day (15m) = +$1,075/day at $5/trade
         64–84% exit rate, more hold exposure
```

### Aggressive (single-scalp, peak target)
```
gate:    0.55–0.65
target:  $0.30
assets:  BTC + ETH + SOL
result:  +$698/day (5m) + $498/day (15m) = +$1,195/day at $5/trade
         52–78% exit rate, ~half of 5m trades hold to resolution
```

### Theoretical maximum (multi-scalp)
```
gate:    0.50–0.65 + p10 |delta| confirmation
target:  $0.10
assets:  BTC + ETH + SOL
result:  +$8,323/day (5m) + $11,420/day (15m) at $5/trade
         94–99% exit rate, 7–25 trades per interval
         ⚠️  Assumes perfect execution on every re-entry
```

---

## 12. Next Steps

1. **Collect more data** — current 31.4h sample is too small for confidence. Need weeks of data across different market conditions
2. **Add depth checks** — verify book depth supports fills at displayed prices
3. **Backtest with latency** — add 100–200ms delay between signal and simulated fill
4. **Build paper trading bot** — execute the strategy without real money, compare simulated vs actual fills
5. **Test at higher bet sizes** — $5 is trivially fillable, but the edge scales with volume. Need to find the depth ceiling
6. **Monitor fee changes** — Polymarket fee structure has changed before and could change again
