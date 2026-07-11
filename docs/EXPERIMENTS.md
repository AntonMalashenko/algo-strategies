# Experiments log (S004 FVG bounce)

Rules: every engine change ships as an opt-in flag, defaults reproduce the
previous behaviour exactly (regression-tested against saved trade CSVs).
Rollback = disable the flag. Full log with discussion lives in the Claude
project doc `claude/experiments-log.md`; this file tracks code-level status.

## E0 — baseline (ACTIVE)
H4 FVG bounce, base limit entry, zone stop + 2 pip buffer, TP 3R,
Asia session only, risk >= 20 raw filter. Core 7 pairs.
n=3276, WR 32%, avgR +0.190 (IS +0.161 / OOS +0.254), +0.129 at 1.5 pip spread.

## E1 — partial TP 50% at +1R (ROLLED BACK)
`run_backtest(..., partial_tp=0.5)`. avgR +0.117 (-38% vs E0). Off.

## E2 — breakeven at +1R (OPTIONAL)
`run_backtest(..., breakeven_at=1.0)`. avgR +0.148 (-22%), kills 1301 full
losses, shorter losing streaks. Candidate if drawdown comfort is needed.

## E3 — time stop 96 bars (ROLLED BACK)
`run_backtest(..., time_stop_bars=96)`. avgR +0.134 (-29%). Off.
Side-product: median hold = 21 M15 bars (~5h) -> swap risk is tail-only.

## E4 — partial + breakeven (ROLLED BACK)
WR jumps to 56% but avgR +0.090 (-53%), near zero at stressed spread. Off.

## E5 — daily-trend filter, aligned (ROLLED BACK)
`run_backtest(..., trend_ma_days=20|50, trend_align="with")`.
SMA20: avgR +0.162; SMA50: +0.181 — both below E0, totR cut by ~35%.
Win rate unchanged. Off.

## E6 — daily-trend filter, counter (ROLLED BACK)
`trend_align="against"`. SMA20 +0.154 / SMA50 +0.164 — also below E0.
Symmetry with E5 shows the quiet-hours edge is direction-agnostic
(session microstructure mean reversion, not trend-dependent). Off.

## E7 — sweep-backed FVG quality tag (KEPT) ⭐
Zones are tagged `sweep=True` when the FVG-creating impulse wicked beyond
the last confirmed H4 fractal (lookback 20 bars) — a liquidity grab.
Metadata only, no behaviour change. Result (core 7 pairs, Asia, RR3):
sweep avgR +0.274 (IS +0.227 / OOS +0.373, +0.212 at 1.5 pip spread)
vs non-sweep +0.167. Confirmed on aggregate and in stress; no effect on
JPY pairs. Use as a sizing tier, not an exclusive filter (sweep = 21% of
trades). Trades carry a `sweep` column.

## E10 — true OOS 2022-2026 (KEPT; see passport)
Frozen config on histdata 2022-03..2026-06 (cross-checked vs ejtrader:
corr 0.99999, zero lag): n=1687, avgR +0.121, WR 30%, every year and
every pair positive; +0.064 at 1.5 pip spread. Sweep tier did NOT
survive (tier per risk unit +0.122 = flat) -> flat sizing is default.
S004 status: VALIDATED. Passport: docs/STRATEGY_S004.md.

## E11 — re-entries after stop-out (ROLLED BACK)
Diagnostic: 34-36% of stop-outs reach the original TP within a day
(44-45% within two), but 70-72% also extend >=1R beyond the stop.
`run_backtest(..., max_reentries=1|2)`: zone revives after an SL, fresh
touch required, invalidation still applies; trades carry an `attempt`
column. Result on BOTH eras: re-entry trades avgR ~0..-0.03 (WR 27%),
portfolio worse (2012-22: 621R -> 560R; 2022-26: 205R -> 168R).
"One trade per zone" is empirically confirmed. Option off.

Regression test: base config reproduces saved EURUSD trades 1811/1811,
r values identical to 1e-12 (re-verified after E5/E6, E7 and E11 changes).
