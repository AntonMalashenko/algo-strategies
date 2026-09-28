"""S022.1 -- Asia Sweep & Reversion, EUR/USD, M5, sweep-and-reclaim of the Asian
range at the European open.

Spec (claude/prompts-new-strategy-candidates.md P14, "S022.1 -- Asia Sweep &
Reversion"; own reformalization of an idea from an external review (Google
Gemini) of S022/S023/S024's honest negative results -- NOT Gemini's literal
text/code, see the prompt's preamble for what was and wasn't taken from that
review). Suffix variant of S022 by the S004i precedent (same instrument/niche,
different signal mechanics/window) -- NOT a modifier of S022 (see
claude/decisions-log.md, 2026-09-24 entry, for the full classification).

Rules: Asia_High/Asia_Low = the high/low over 00:00-06:55 UTC (a DIFFERENT,
narrower window than S022's own 22:00-06:00 UTC session -- deliberately not
inherited from S022, see the prompt). Signal window 07:00-09:30 UTC (the
Frankfurt/London open). BUY if Low[i] < Asia_Low AND Close[i] > Asia_Low
(price swept below the Asian low and closed back inside it); SELL if
High[i] > Asia_High AND Close[i] < Asia_High. At most one trade/day. SL beyond
the sweep bar's extremum + a buffer. TP = nearest of (a) the opposite Asia
range boundary, (b) 1.5xSL. Forced close at 16:00 UTC.

Design choices made to turn this into mechanical, testable rules (documented
here per the project's convention of recording judgment calls -- see
strategies/asia_mean_reversion.py's and strategies/gold_session_momentum.py's
module docstrings for the pattern this follows):

* Asia range is computed on M5 bars whose OWN calendar day (UTC) falls in
  [00:00, 06:55) -- i.e. bars with timestamp (bar open, label=left) in that
  window on day D. This is entirely disjoint from S022's 22:00-06:00 session
  (which spans midnight D-1/D) -- no bar is shared between the two ranges.
* Entry timing (NOT specified explicitly in the prompt -- a judgment call).
  The sweep-and-reclaim condition is evaluated on bar i's fully closed OHLC;
  the trade is entered at bar i+1's OPEN, the earliest a real bot could act on
  a just-closed bar -- same convention as strategies/asia_mean_reversion.py
  (S022) and strategies/us_index_breakout.py (S023), avoiding an idealized
  same-bar-close fill. This is the more conservative reading and keeps this
  engine consistent with its two closest siblings.
* SL: beyond the SWEEP bar's own extremum (bar i's low for BUY, bar i's high
  for SELL -- the wick that pierced the range, not the entry bar i+1) plus a
  named buffer constant, sweep_buffer_price (in price units; EUR/USD baseline
  0.00020, i.e. 2 pips at the conventional 4th-decimal EUR/USD pip). This is
  an honest first guess, NOT a tuned/optimized value -- explicitly flagged in
  the prompt as "непроверенный первый тест".
* TP: "nearest of the opposite range boundary or 1.5xSL" -- read as: whichever
  of the two candidate TARGET PRICES is closer to entry (smaller |distance|).
  Edge case (not addressed in the prompt): if the opposite boundary has
  already been breached by the reclaim itself (i.e. lies on the WRONG side of
  entry, distance <= 0 in the trade's favorable direction -- can happen with a
  wide reclaim bar), that candidate is discarded and TP falls back to 1.5xSL
  alone. Documented here as a deliberate, honest handling of an unspecified
  corner case, not a silent default.
* Same-bar SL/TP tie-break: SL is assumed hit first (conservative, worst-case
  ordering -- same convention as S022/S023/S024).
* Forced time exit at the session's last in-window bar (< 16:00 UTC) if
  neither SL nor TP has been hit by then, reason "time" -- same convention as
  S022/S024's forced-close rule.
* ATR(14) is NOT part of this strategy's signal or risk sizing (unlike S022);
  the buffer and 1.5x multiplier are both plain price-distance constructions
  per the prompt's own spec, so no indicator warm-up is required beyond having
  a complete Asia range for the day.
* Costs: cost_bps_roundtrip (bps of entry price), same convention as
  S022/S023/S024, charged once per trade.

No look-ahead: the Asia range for day D uses only D's own 00:00-06:55 UTC
bars (all strictly before the 07:00-09:30 UTC signal window); the sweep
signal on bar i uses only bar i's closed OHLC; the trade acts no earlier than
bar i+1's open; SL/TP are frozen at entry and checked bar-by-bar from
entry_time onward. The Gate 0 tests in
tests/strategies/test_asia_sweep_reversion.py assert this by truncating the
input and checking already-emitted signals/trades are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import time

import numpy as np
import pandas as pd

ASIA_RANGE_START = time(0, 0)     # UTC, inclusive
ASIA_RANGE_END = time(6, 55)      # UTC, exclusive -- last Asia-range bar opens at 06:50 (M5)
SIGNAL_START = time(7, 0)         # UTC, inclusive -- start of the signal/entry window
SIGNAL_END = time(9, 30)          # UTC, exclusive -- end of the signal window
FORCE_CLOSE = time(16, 0)         # UTC -- forced time exit


@dataclass(frozen=True)
class AsiaSweepConfig:
    sweep_buffer_price: float = 0.00020   # SL buffer beyond the sweep extremum, price units (2 pips EURUSD)
    tp_r_mult: float = 1.5                # TP candidate B = entry +/- tp_r_mult * stop_dist
    asia_range_start: time = ASIA_RANGE_START
    asia_range_end: time = ASIA_RANGE_END
    signal_start: time = SIGNAL_START
    signal_end: time = SIGNAL_END
    force_close: time = FORCE_CLOSE
    cost_bps_roundtrip: float = 1.0       # round-trip cost, bps of entry price, charged once/trade

    def with_(self, **overrides) -> "AsiaSweepConfig":
        return replace(self, **overrides)


# Frozen baseline -- matches the prompt spec exactly, never edit in place.
ASIA_SWEEP_BASE = AsiaSweepConfig()


@dataclass
class Trade:
    day: pd.Timestamp          # UTC calendar day the Asia range belongs to
    direction: str             # "long" | "short"
    signal_time: pd.Timestamp  # sweep bar's own label (its open time)
    entry_time: pd.Timestamp   # signal_time + one bar = fill clock time
    entry_price: float
    stop_price: float
    tp_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str           # "stop" | "tp" | "time"
    asia_high: float
    asia_low: float
    sweep_extreme: float       # the sweep bar's own low (long) / high (short)
    stop_dist: float
    tp_candidate: str          # "opposite_boundary" | "r_mult" -- which candidate won
    gross: float
    net: float
    r_multiple: float


def compute_asia_range(bars: pd.DataFrame, cfg: AsiaSweepConfig = ASIA_SWEEP_BASE) -> pd.DataFrame:
    """One row per UTC calendar day with a complete Asia-range window: the
    day's Asia_High/Asia_Low over [asia_range_start, asia_range_end). A day
    with no bars in that window is simply absent (no signal can fire for it)."""
    if len(bars) == 0:
        return pd.DataFrame(columns=["asia_high", "asia_low"])
    t = bars.index.time
    in_range = (t >= cfg.asia_range_start) & (t < cfg.asia_range_end)
    sub = bars.loc[in_range]
    if sub.empty:
        return pd.DataFrame(columns=["asia_high", "asia_low"])
    day = sub.index.normalize()
    out = pd.DataFrame({
        "asia_high": sub["high"].groupby(day).max(),
        "asia_low": sub["low"].groupby(day).min(),
    })
    out.index.name = "day"
    return out


def _day_key(ts: pd.Timestamp) -> pd.Timestamp:
    return ts.normalize()


def simulate(bars: pd.DataFrame, cfg: AsiaSweepConfig = ASIA_SWEEP_BASE) -> list[Trade]:
    if len(bars) == 0:
        return []
    asia = compute_asia_range(bars, cfg)
    n = len(bars)
    idx = bars.index
    t = idx.time
    in_signal = (t >= cfg.signal_start) & (t < cfg.signal_end)
    days = idx.normalize()

    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    open_ = bars["open"].to_numpy(dtype=float)

    trades: list[Trade] = []
    traded_days: set = set()

    for i in range(n - 1):  # need bar i+1 to enter on
        if not in_signal[i]:
            continue
        d = days[i]
        if d in traded_days or d not in asia.index:
            continue
        a_hi, a_lo = asia.at[d, "asia_high"], asia.at[d, "asia_low"]
        if pd.isna(a_hi) or pd.isna(a_lo):
            continue

        direction = None
        if low[i] < a_lo and close[i] > a_lo:
            direction = "long"
            sweep_extreme = low[i]
        elif high[i] > a_hi and close[i] < a_hi:
            direction = "short"
            sweep_extreme = high[i]
        if direction is None:
            continue

        entry_time = idx[i + 1]
        # signal must leave room to enter within the same day's signal window
        if days[i + 1] != d or not (t[i + 1] >= cfg.signal_start and t[i + 1] < cfg.signal_end):
            continue

        entry_price = open_[i + 1]
        if direction == "long":
            stop_price = sweep_extreme - cfg.sweep_buffer_price
            stop_dist = entry_price - stop_price
        else:
            stop_price = sweep_extreme + cfg.sweep_buffer_price
            stop_dist = stop_price - entry_price
        if stop_dist <= 0:
            continue  # degenerate (buffer larger than the move) -- no valid risk unit

        r_target = entry_price + cfg.tp_r_mult * stop_dist if direction == "long" \
            else entry_price - cfg.tp_r_mult * stop_dist
        if direction == "long":
            boundary_valid = a_hi > entry_price
            tp_price, tp_candidate = ((a_hi, "opposite_boundary") if boundary_valid and a_hi <= r_target
                                       else (r_target, "r_mult"))
        else:
            boundary_valid = a_lo < entry_price
            tp_price, tp_candidate = ((a_lo, "opposite_boundary") if boundary_valid and a_lo >= r_target
                                       else (r_target, "r_mult"))

        # walk forward from entry_time to force-close, same day only
        rest_mask = (idx >= entry_time) & (days == d) & (t < cfg.force_close)
        rest_pos = np.nonzero(rest_mask)[0]
        if len(rest_pos) == 0:
            continue  # no room left in the session -- skip (does not consume the day)

        exit_pos = rest_pos[-1]
        exit_time, exit_price, exit_reason = idx[exit_pos], close[exit_pos], "time"
        for j in rest_pos:
            if direction == "long":
                stop_hit, tp_hit = low[j] <= stop_price, high[j] >= tp_price
            else:
                stop_hit, tp_hit = high[j] >= stop_price, low[j] <= tp_price
            if stop_hit:  # conservative ordering: stop assumed hit before TP within the same bar
                exit_time, exit_price, exit_reason = idx[j], stop_price, "stop"
                break
            if tp_hit:
                exit_time, exit_price, exit_reason = idx[j], tp_price, "tp"
                break

        gross = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        cost = entry_price * cfg.cost_bps_roundtrip / 10_000.0
        net = gross - cost
        r = net / stop_dist if stop_dist > 0 else np.nan

        trades.append(Trade(d, direction, idx[i], entry_time, entry_price, stop_price, tp_price,
                             exit_time, exit_price, exit_reason, a_hi, a_lo, sweep_extreme,
                             stop_dist, tp_candidate, gross, net, r))
        traded_days.add(d)
    return trades


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    """One row per trade; an empty list still yields the full column set."""
    return pd.DataFrame([t.__dict__ for t in trades], columns=[f.name for f in fields(Trade)])
