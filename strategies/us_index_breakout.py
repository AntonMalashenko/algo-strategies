"""S023 -- US Index Breakout, NAS100 (USTEC), M15, entries 13:30-18:00 UTC.

Spec (claude/strategies-registry.md S023; Bot 2 in Anton's original three-bot
brief, claude/prompts-new-strategy-candidates.md P13), verbatim: "NAS100
(USTEC), M15, 13:30-18:00 UTC. Range = High/Low over 08:00-13:30 UTC. BUY
STOP=High+2pts, SELL STOP=Low-2pts. SL=opposite range boundary (max 1% ATR).
TP=SL x 2.0."

Data proxy: histdata.com NSXUSD M1 (the same NAS100 CFD proxy S021 /
strategies/orb_intraday already uses), loaded in true UTC via
utils.histdata.load_histdata_m1_utc and resampled to M15 with
utils.histdata.resample_ohlc (bar timestamp = its own open time).

Design choices made to turn this into mechanical, testable rules (documented
here per the project's convention of recording judgment calls, not silently
guessing -- see strategies/orb_intraday/engine.py's seasonal-anchor note,
strategies/eia_calendar.py's holiday-shift table and
strategies/asia_mean_reversion.py for the pattern this follows):

* STOP-LOSS INTERPRETATION -- the one genuinely ambiguous clause. The spec
  says "SL=opposite range boundary (max 1% ATR)". Read literally, "1% ATR" is
  not a sensible distance unit on its own (one percent of an ATR would be a
  sub-point stop on NAS100 M15), so this engine implements the reading that
  the clause is a CAP on the range-based stop: the raw stop distance is from
  the entry price to the OPPOSITE range boundary (long entered at
  range_high + buffer -> raw = entry - range_low; short entered at
  range_low - buffer -> raw = range_high - entry), and that raw distance is
  capped at atr_cap_mult * ATR(14) (atr_cap_mult = 1.0, i.e. "max 1x ATR")
  whenever the range-based stop would be wider:
      stop_distance = min(raw_opposite_boundary_distance, atr_cap_mult * ATR14)
      stop_price    = entry -/+ stop_distance   (long / short)
  ATR(14) is Wilder's (utils.indicators.wilder_atr), computed continuously over
  the whole M15 series (not reset per day). This is a deliberate, stated
  choice, not a silent guess: if Anton meant something else by "1% ATR" (e.g.
  a percent-of-price cap, or a percent of a daily ATR), only atr_cap_mult /
  the cap formula in simulate() need to change. Empirically the cap binds on
  the large majority of trades (a 5.5h pre-US range is typically several
  M15 ATRs wide), so in practice S023's stop is "1x M15 ATR" far more often
  than "opposite range boundary" -- see backtest/run_us_index_breakout.py's
  cap-binding-rate line.
* ATR timing (a clarification of "ATR evaluated at the entry bar"): the ATR
  used is the value as of the LAST FULLY CLOSED M15 bar BEFORE the entry bar
  (atr.shift(1) at the entry bar). The entry bar's own ATR includes that
  bar's high/low/close, which are not yet known at the moment the resting
  stop order fills mid-bar -- using it would be look-ahead. A live bot does
  the equivalent by recomputing the pending orders' SL at each M15 close.
  Days where that ATR is still in warm-up (NaN) are skipped.
* Range window: range_high/range_low = max(high)/min(low) over M15 bars whose
  open time is in [08:00, 13:30) UTC of that calendar day -- 22 bars when
  complete. Days with fewer than min_range_bars (20) in-window bars are
  skipped (data-gap guard, same defensive pattern as
  strategies/orb_intraday's min_session_bars): the NSXUSD file set has ~120
  days with holes in this window (clustered in Mar-2020 and Feb/Mar-2023),
  where a range built from a partial window would not be the spec's range.
* Entry: resting BUY STOP at range_high + breakout_buffer_pts and SELL STOP at
  range_low - breakout_buffer_pts (2.0 index points = 2.0 native price units
  of the raw histdata quote, e.g. 30484.24 -> 30486.24), live only for M15
  bars whose open time is in [13:30, 18:00) UTC. Bars are scanned in order;
  the first bar whose high reaches BUY STOP fires a long, the first whose low
  reaches SELL STOP fires a short. A bar that reaches BOTH levels is
  ambiguous (intra-bar order unknown) and the whole day is skipped, same
  "hit_u and hit_l -> skip" convention as strategies/orb_intraday.engine. At
  most one trade per day; the opposite order is cancelled on fill (no
  stop-and-reverse). No level hit by 18:00 UTC -> no trade.
* Entry price = the stop level itself (idealized stop fill, same as
  strategies/orb_intraday's `entry_price = U if long else L`). Because NSXUSD
  trades nearly 24h, the 13:30 bar is contiguous with 13:15 and genuine
  gap-through fills are rare, but a bar can still OPEN beyond the level (e.g.
  a 13:30 UTC data release); the runner reports how many entries that affects
  and what a fill-at-open would cost, rather than silently changing the rule.
* Take-profit: tp = entry +/- tp_mult * stop_distance (tp_mult = 2.0, i.e. a
  fixed 2R target on the (possibly ATR-capped) stop distance).
* Exit monitoring starts ON the entry bar (same as strategies/orb_intraday):
  each bar checks SL and TP; if a bar's range would hit both, SL is assumed
  first (conservative worst-case ordering, consistent with this project's
  other engines). This includes the entry bar itself: a long whose entry bar
  also trades down through the stop is booked as a stop-out.
* Time exit: if neither SL nor TP is hit, the trade is force-closed at the
  close of the last M15 bar opening before 18:00 UTC (the 17:45 bar, i.e. at
  18:00), reason "time" -- a single-session trade, consistent with its sibling
  bots S022/S024 and with strategies/orb_intraday's end-of-session close.
  Holes in the ENTRY window are deliberately not filtered (that would require
  knowing at 13:30 which later bars will be missing -- not causal); a trade is
  graded on whatever bars exist.
* Costs: cost_bps_roundtrip of the entry price, charged once per trade (same
  convention as strategies/orb_intraday). R = net_price_move / stop_distance.

No look-ahead: the range uses only bars that closed by 13:30 UTC; ATR at entry
uses only bars strictly before the entry bar; the entry scan and SL/TP checks
walk forward bar by bar and never consult a later bar. The Gate 0 tests in
tests/strategies/test_us_index_breakout.py assert this by truncating the input
and checking already-emitted trades are unchanged.

S021 MECHANICAL-OVERLAP NOTE (Gate 3, required by the registry before S023 can
be finalized; written from reading strategies/orb_intraday/config.py+engine.py
and checked empirically by backtest/run_us_index_breakout.py, 2019-01..2026-06):
Same instrument and data (NSXUSD M1), same premise (trade the direction of a
breakout around the US cash open), same one-trade/day, idealized stop-price
fill and "bar hits both levels -> skip day". The construction differs: S021
anchors on the price at fixed-EST 09:30 = 14:30 UTC (true cash open in EST
months, one hour into the cash session in EDT months), its band is that open
+/- 0.20 * ADR14 (prior 14 sessions' 09:30-15:59 ranges), entries 14:30-19:29
UTC, stop 0.75 * ADR14 (hundreds of points), no TP, exit 20:59 UTC. S023's
levels come from the EARLIER 08:00-13:30 UTC range +/- 2pt, entries
13:30-18:00 UTC (so it can fire before S021's anchor even exists -- ~37% of
S023 entries are on the 13:30 bar), stop <= 1 x M15 ATR (median ~19pt), 2R TP,
exit 18:00 UTC. The entry windows overlap on 14:30-18:00 UTC and both fire on
almost every valid day, so same-date co-firing is near-total by construction
(84% of S023 days, 96% of S021 days) and says little by itself; the
informative numbers are direction agreement on co-fire days (63%) and per-day
R correlation (+0.05 for the base ATR-capped S023, +0.29 under the uncapped
opposite-boundary reading, where S023's positive days are concentrated on
S021-agreeing days: +0.35R same-direction vs -0.51R opposite). Verdict:
mechanically distinct in construction (different reference level, window and
risk unit), but not an independent source of edge -- both express "go with
the US-session breakout on NAS100", point the same way on ~2/3 of shared
days, and running both adds correlated directional exposure on the same
instrument. The base S023's near-zero R correlation is an artifact of its
1-ATR stop being dominated by noise stop-outs, not genuine diversification.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time

import numpy as np
import pandas as pd

from utils.indicators import wilder_atr


@dataclass(frozen=True)
class UsIndexBreakoutConfig:
    range_start: time = time(8, 0)      # UTC, inclusive: first bar of the range
    range_end: time = time(13, 30)      # UTC, exclusive: range ends, entry window opens
    entry_end: time = time(18, 0)       # UTC, exclusive: entry window end; forced time exit
    breakout_buffer_pts: float = 2.0    # stop levels = range_high + this / range_low - this (pts)
    atr_period: int = 14                # Wilder ATR period on the M15 series
    atr_cap_mult: float = 1.0           # stop distance capped at this * ATR14 ("max 1x ATR")
    tp_mult: float = 2.0                # TP distance = tp_mult * stop distance (2R)
    min_range_bars: int = 20            # skip day below this many range-window bars (22 = full)
    cost_bps_roundtrip: float = 1.0     # round-trip cost, bps of entry price, charged once/trade

    def with_(self, **overrides) -> "UsIndexBreakoutConfig":
        return replace(self, **overrides)


US_INDEX_BREAKOUT_BASE = UsIndexBreakoutConfig()  # frozen baseline -- the spec; never edit in place


@dataclass
class Trade:
    day: pd.Timestamp          # UTC calendar date
    direction: str             # "long" | "short"
    range_high: float
    range_low: float
    entry_time: pd.Timestamp   # open time of the M15 bar in which the stop order filled
    entry_price: float
    raw_stop_dist: float       # entry -> opposite range boundary
    atr_at_entry: float        # ATR14 as of the last closed bar before the entry bar
    stop_dist: float           # min(raw_stop_dist, atr_cap_mult * atr_at_entry)
    atr_capped: bool           # True when the ATR cap was the binding constraint
    stop_price: float
    tp_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str           # "stop" | "tp" | "time"
    gross: float
    net: float
    r_multiple: float


def add_indicators(bars: pd.DataFrame, cfg: UsIndexBreakoutConfig) -> pd.DataFrame:
    """Attach ATR14 (continuous, causal) and atr_prev = ATR as of the previous
    closed bar to a copy of the M15 frame."""
    df = bars.copy()
    df["atr"] = wilder_atr(df["high"], df["low"], df["close"], cfg.atr_period)
    df["atr_prev"] = df["atr"].shift(1)
    return df


def simulate(bars: pd.DataFrame,
             cfg: UsIndexBreakoutConfig = US_INDEX_BREAKOUT_BASE) -> list[Trade]:
    """Run S023 over an M15 OHLC frame indexed in tz-naive UTC (open-time labels)."""
    df = add_indicators(bars, cfg)
    trades: list[Trade] = []

    for d, day_df in df.groupby(df.index.normalize(), sort=True):
        tod = day_df.index.time
        rng = day_df.loc[(tod >= cfg.range_start) & (tod < cfg.range_end)]
        if len(rng) < cfg.min_range_bars:
            continue
        range_high = float(rng["high"].max())
        range_low = float(rng["low"].min())
        buy_stop = range_high + cfg.breakout_buffer_pts
        sell_stop = range_low - cfg.breakout_buffer_pts

        win = day_df.loc[(tod >= cfg.range_end) & (tod < cfg.entry_end)]
        if win.empty:
            continue
        highs = win["high"].to_numpy(dtype=float)
        lows = win["low"].to_numpy(dtype=float)
        closes = win["close"].to_numpy(dtype=float)
        atr_prev = win["atr_prev"].to_numpy(dtype=float)

        direction = None
        k = -1
        for i in range(len(win)):
            hit_u = highs[i] >= buy_stop
            hit_l = lows[i] <= sell_stop
            if hit_u and hit_l:
                direction = "skip"
                break
            if hit_u:
                direction, k = "long", i
                break
            if hit_l:
                direction, k = "short", i
                break
        if direction is None or direction == "skip":
            continue

        atr_at_entry = atr_prev[k]
        if np.isnan(atr_at_entry) or atr_at_entry <= 0:
            continue  # ATR warm-up -- no valid cap yet

        is_long = direction == "long"
        entry_price = buy_stop if is_long else sell_stop
        raw_stop_dist = (entry_price - range_low) if is_long else (range_high - entry_price)
        atr_cap = cfg.atr_cap_mult * atr_at_entry
        stop_dist = min(raw_stop_dist, atr_cap)
        atr_capped = atr_cap < raw_stop_dist
        if is_long:
            stop_price = entry_price - stop_dist
            tp_price = entry_price + cfg.tp_mult * stop_dist
        else:
            stop_price = entry_price + stop_dist
            tp_price = entry_price - cfg.tp_mult * stop_dist

        exit_idx, exit_price, exit_reason = len(win) - 1, closes[-1], "time"
        for j in range(k, len(win)):
            if is_long:
                stop_hit = lows[j] <= stop_price
                tp_hit = highs[j] >= tp_price
            else:
                stop_hit = highs[j] >= stop_price
                tp_hit = lows[j] <= tp_price
            if stop_hit:  # conservative ordering: stop assumed before TP within the same bar
                exit_idx, exit_price, exit_reason = j, stop_price, "stop"
                break
            if tp_hit:
                exit_idx, exit_price, exit_reason = j, tp_price, "tp"
                break

        gross = (exit_price - entry_price) if is_long else (entry_price - exit_price)
        cost = entry_price * cfg.cost_bps_roundtrip / 10_000.0
        net = gross - cost
        trades.append(Trade(d, direction, range_high, range_low, win.index[k], entry_price,
                            raw_stop_dist, float(atr_at_entry), stop_dist, bool(atr_capped),
                            stop_price, tp_price, win.index[exit_idx], float(exit_price),
                            exit_reason, float(gross), float(net), float(net / stop_dist)))
    return trades


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([t.__dict__ for t in trades])
