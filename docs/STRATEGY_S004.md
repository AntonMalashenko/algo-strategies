# Strategy Passport — S004 "H4 FVG bounce, quiet hours"

Version 1.0 · 2026-07-11 · Status: **VALIDATED → ready for paper trading**
Owner: Anton. Rule changes only via the experiments log (E-numbers); no
live tweaking. Russian working copy lives in the Claude project
(`claude/strategy-passport-S004.md`).

## 1. Edge thesis
Limit-order bounce off a higher-timeframe imbalance zone during quiet
hours. During the Asian session EUR/US pairs lack large order flow and
price tends to revert to recent imbalance levels (session microstructure
mean reversion). Known properties: direction-agnostic w.r.t. daily trend
(E5-E6), not improved by M15 confirmations (E1-E4), dies in active
sessions and on gapping instruments (indices).

## 2. Universe & data
Core 7 pairs: GBPJPY, EURUSD, USDCHF, GBPUSD, EURJPY, USDJPY, AUDUSD.
M15, broker/EET server time. Validated on ejtrader 2012-2022 plus
histdata 2022-2026 (cross-checked: corr 0.99999, zero time lag).
Excluded: EURGBP (negative everywhere), USDCAD/XAUUSD/AUDJPY (die at
realistic spread), indices (gaps break limit entries).

## 3. Frozen rules (v1.0)
- Zone: H4 fair value gap (3-bar pattern, `low[i+2] > high[i]` bullish /
  mirrored bearish), tradable only after the 3rd H4 bar closes.
- Entry: limit at the near zone edge, first touch only, entry hour
  00:00-06:59 server time. One trade per zone. Skip if computed risk
  < buffer (gap guard).
- Stop: far zone edge + 2 pip buffer. Take-profit: static 3R.
  No management: no partials, no breakeven, no trailing (E1-E4).
- Sizing: FLAT 0.5% risk per trade (paper); max 1% after paper.
  Sweep tier NOT applied (demoted in E10). Portfolio cap: max 4
  concurrent positions / 2% total open risk.

## 4. Honest expectations (from true OOS 2022-2026)
+0.06..+0.12R per trade at realistic costs (plan on the lower bound).
~370 trades/year across 7 pairs, win rate ~30% (breakeven 25%).
At 0.5% risk: ~10-20%/year nominal; flat or negative years possible
(2022). Historical max drawdown ~40-50R (~20-25% at 0.5%). Losing
streaks of 12-16 are NORMAL. Critical dependency: overnight spread —
economics marginal above 1.5 pips average, do not trade above 2.

## 5. Kill criteria
Stop trading if ANY: (1) portfolio drawdown > 25% from peak;
(2) rolling 200-trade mean R < -0.15R (~-2 sigma below expectation);
(3) realized monthly average spread > 1.8 pips on majors;
(4) paper/live trades diverge from the engine simulation of the same
days for two consecutive months.
Pause (not stop): 10 consecutive losses -> execution audit; 16+ ->
pause until monthly review.

## 6. Paper plan
3 months or >=90 trades (whichever is longer) on the target broker's
demo. Monthly reconciliation: fills vs engine simulation, realized
spread by hour, metrics vs section 4. Log to reports/paper/.
Go-live gate: results within +-1.5 sigma of expectation, execution
matches the model, costs in range. Initial live risk 0.25% for the
first two months.

## 7. Open items
Swaps for the holding-time tail (>12h trades); sweep tag monitoring
(decision after >=200 paper trades); broker DST handling of the Asia
window.

## 8. Validation history
E0-E10 in docs/EXPERIMENTS.md and the Claude project experiments log.
Engine: strategies/fvg_mtf.py (regression-guarded). Data provenance:
data/raw/*/ + scripts/.
