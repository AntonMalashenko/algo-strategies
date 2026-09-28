"""S024.1 -- Gold London-Range Breakout with Chandelier Trail, XAU/USD, M15.

Spec (claude/prompts-new-strategy-candidates.md P14, "S024.1 -- Gold London-Range
Breakout with Chandelier Trail"; own reformalization of an idea from an external
review (Google Gemini) of S022/S023/S024's honest negative results -- NOT
Gemini's literal text/code, see the prompt's preamble). Suffix variant of S024 by
the S004i precedent (same instrument/TF/EMA filter/initial-SL size, different
breakout-level construction and exit) -- NOT a modifier of S024 (see
claude/decisions-log.md, 2026-09-24 entry, for the full classification).

Rules: London_High/London_Low = the fixed range over 07:00-12:30 UTC (NOT a
rolling Donchian channel like S024's -- deliberately isolates the effect of a
fixed-session breakout level vs. S024's rolling one). Entry window 13:00-16:30
UTC. BUY if Close > London_High AND Close > EMA(200); SELL if Close < London_Low
AND Close < EMA(200) -- same EMA(200) trend filter as S024, for comparability.
S024's ATR-expansion filter is DELIBERATELY DROPPED (see below). Initial SL =
1.5xATR(14) (same size as S024). No fixed TP: once unrealized profit reaches
>=1.0xATR(14), the stop switches to a Chandelier trail at 2.0xATR(14) behind the
running favorable extreme -- the main new element this hypothesis exists to test
(a direct answer to S024's diagnosed weakness: a static TP=2.5xATR capped the
upside during the 2022-2026 gold rally, which was the entire source of S024's
small positive gross edge). Forced close 20:00 UTC. At most one trade/day.

Design choices made to turn this into mechanical, testable rules (documented
here per the project's convention of recording judgment calls -- see
strategies/gold_session_momentum.py's and strategies/asia_sweep_reversion.py's
module docstrings for the pattern this follows):

* London range is computed the same way as strategies/asia_sweep_reversion.py's
  Asia range: the high/low over M15 bars whose own label (open time, UTC) falls
  in [07:00, 12:30). A day with no bars in that window has no range and cannot
  signal.
* Entry price = the breakout bar's own CLOSE (same explicitly-idealized, no-
  latency fill convention as S024 -- kept for direct comparability; see S024's
  own docstring for the honesty caveat this carries).
* ATR-expansion filter DROPPED, as an explicit, documented simplification (the
  prompt's own instruction) -- to isolate the effect of the single new element
  this hypothesis is actually about (the Chandelier exit), rather than testing
  two changes (dropped filter + new exit) at once and being unable to attribute
  the result to either.
* Chandelier mechanics, causal by construction (THE highest-risk area for a
  subtle look-ahead bug in this engine, per the prompt -- special care taken
  here, and a dedicated Gate 0 test in
  tests/strategies/test_gold_chandelier_breakout.py proves it):
  - atr_at_entry is frozen at the signal bar (same convention as every other
    engine in this project) and used for BOTH the profit-trigger distance
    (1.0x) and the trail distance (2.0x) -- neither is recomputed bar-by-bar,
    per the prompt's own spec ("ATR заморожен на баре входа").
  - The "currently active stop" for bar j is decided using only bars up to and
    including j-1: checked against bar j's own low (long) / high (short)
    FIRST, using that already-decided level. Only AFTER that check (i.e. only
    if the trade survives bar j) is the running favorable extreme updated
    using bar j's OWN high (long) / low (short), and only then may the stop
    ratchet tighter for bar j+1 onward. This ordering is what makes the trail
    causal: bar j's exit decision never depends on bar j's own extremes in the
    trade's favor, only on levels fixed before bar j opened.
  - The stop only ever ratchets in the trade's favor (max() for long, min()
    for short) -- once armed, it can never loosen back toward the initial SL,
    even if price pulls back without a new extreme.
  - exit_reason distinguishes "stop" (the pre-arm initial 1.5xATR SL was hit,
    trail never armed) from "trail" (the trade profited enough to arm the
    trail, which was later hit) -- both are stop-outs mechanically, but this
    split matters for reading the results (a "trail" exit banked at least
    part of a favorable move; a "stop" exit did not).
* Session gate / one trade per day / forced 20:00 UTC close: same "one setup
  per session, force-closed, no overnight risk" convention as S022/S023/S024.
* Costs: cost_bps_roundtrip (bps of entry price) plus cost_price_roundtrip
  (absolute, USD/oz), same convention and same scenario ladder as S024.

No look-ahead: London_High/Low for day D use only D's own 07:00-12:30 UTC bars;
EMA(200)/ATR(14) at bar i are functions of bars <=i only; the breakout decision
on bar i uses only bar i's closed OHLC and fills at that close; every bar-by-bar
exit/trail check from i+1 onward uses only the stop level already fixed before
that bar, per the Chandelier ordering above. The Gate 0 tests in
tests/strategies/test_gold_chandelier_breakout.py assert all of this by
truncating the input and checking already-emitted trades are unchanged, plus a
dedicated adversarial test for the trailing-stop ordering specifically.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import time

import numpy as np
import pandas as pd

from utils.indicators import wilder_atr

LONDON_START = time(7, 0)     # UTC, inclusive -- London range window start
LONDON_END = time(12, 30)     # UTC, exclusive -- London range window end
ENTRY_START = time(13, 0)     # UTC, inclusive -- entry window start (after the range fixes)
ENTRY_END = time(16, 30)      # UTC, exclusive -- entry window end
FORCE_CLOSE = time(20, 0)     # UTC -- forced time exit
BPS_DIVISOR = 10_000.0


@dataclass(frozen=True)
class GoldChandelierConfig:
    ema_period: int = 200                 # trend filter, same as S024
    atr_period: int = 14                  # Wilder ATR for SL/trail sizing
    stop_atr_mult: float = 1.5            # initial SL distance = mult * ATR(14) at signal bar, frozen
    trail_trigger_atr_mult: float = 1.0   # arm the trail once unrealized profit >= mult * ATR(14) (frozen)
    trail_dist_atr_mult: float = 2.0      # trail distance behind the favorable extreme, ATR(14) (frozen)
    london_start: time = LONDON_START
    london_end: time = LONDON_END
    entry_start: time = ENTRY_START
    entry_end: time = ENTRY_END
    force_close: time = FORCE_CLOSE
    bar_minutes: int = 15                 # bar length; fill clock time = signal bar label + this
    cost_bps_roundtrip: float = 1.0       # round-trip cost, bps of entry price, charged once/trade
    cost_price_roundtrip: float = 0.0     # extra round-trip cost, price units (USD/oz), once/trade

    def with_(self, **overrides) -> "GoldChandelierConfig":
        return replace(self, **overrides)


# Frozen baseline -- matches the prompt spec exactly, never edit in place.
GOLD_CHANDELIER_BASE = GoldChandelierConfig()


@dataclass
class Trade:
    day: pd.Timestamp          # UTC calendar day of the signal bar
    direction: str             # "long" | "short"
    signal_time: pd.Timestamp  # breakout bar label (its open time)
    entry_time: pd.Timestamp   # breakout bar close = fill clock time
    entry_price: float
    initial_stop_price: float
    atr_at_entry: float
    stop_dist: float           # = stop_atr_mult * atr_at_entry, the R risk unit (frozen)
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str           # "stop" | "trail" | "time"
    trail_armed: bool          # did the trade ever arm the Chandelier trail
    max_favorable_extreme: float
    london_high: float
    london_low: float
    ema_at_entry: float
    gross: float
    net: float
    r_multiple: float


def ema(close: pd.Series, period: int) -> pd.Series:
    """Standard recursive EMA (alpha = 2/(period+1)), causal. First period-1
    values are NaN (warm-up). Same construction as
    strategies/gold_session_momentum.py's ema() -- re-declared locally rather
    than imported cross-strategy, per this project's module-boundary
    convention (each strategy module is self-contained)."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def compute_london_range(bars: pd.DataFrame, cfg: GoldChandelierConfig = GOLD_CHANDELIER_BASE
                         ) -> pd.DataFrame:
    """One row per UTC calendar day with a complete London-range window: the
    day's London_High/London_Low over [london_start, london_end). A day with no
    bars in that window is simply absent (no signal can fire for it)."""
    if len(bars) == 0:
        return pd.DataFrame(columns=["london_high", "london_low"])
    t = bars.index.time
    in_range = (t >= cfg.london_start) & (t < cfg.london_end)
    sub = bars.loc[in_range]
    if sub.empty:
        return pd.DataFrame(columns=["london_high", "london_low"])
    day = sub.index.normalize()
    out = pd.DataFrame({
        "london_high": sub["high"].groupby(day).max(),
        "london_low": sub["low"].groupby(day).min(),
    })
    out.index.name = "day"
    return out


def add_indicators(bars: pd.DataFrame, cfg: GoldChandelierConfig = GOLD_CHANDELIER_BASE) -> pd.DataFrame:
    """Attach the causal EMA(200) and ATR(14) to a copy of the M15 bars."""
    df = bars.copy()
    df["ema"] = ema(df["close"], cfg.ema_period)
    df["atr"] = wilder_atr(df["high"], df["low"], df["close"], cfg.atr_period)
    return df


def walk_chandelier_exit(idx: pd.DatetimeIndex, high: np.ndarray, low: np.ndarray,
                         close: np.ndarray, t: np.ndarray, days: np.ndarray,
                         start_pos: int, day: pd.Timestamp, direction: str,
                         entry_price: float, atr_at_entry: float,
                         cfg: GoldChandelierConfig = GOLD_CHANDELIER_BASE) -> dict:
    """Bar-by-bar exit walk for ONE trade, starting at `start_pos` (the first
    bar to monitor, normally signal_pos + 1), sharing the SAME day as `day`,
    stopping at the first bar whose time-of-day reaches cfg.force_close.

    This is the one function in the engine responsible for the Chandelier
    causality guarantee (see the module docstring) -- factored out on its own
    so it can be unit-tested directly with fully controlled bar sequences,
    without also having to construct a valid London-range breakout to get a
    trade started (see tests/strategies/test_gold_chandelier_breakout.py's
    dedicated adversarial tests).

    Returns a dict with exit_time/exit_price/exit_reason/trail_armed/
    max_favorable_extreme/found_exit (found_exit=False means the walk ran off
    the end of the day's data before any exit or force-close bar -- caller
    should treat this the same as "no room to enter").
    """
    stop_dist = cfg.stop_atr_mult * atr_at_entry
    trigger_dist = cfg.trail_trigger_atr_mult * atr_at_entry
    trail_dist = cfg.trail_dist_atr_mult * atr_at_entry
    current_stop = (entry_price - stop_dist) if direction == "long" else (entry_price + stop_dist)
    extreme = entry_price
    armed = False
    force_close_bar = None
    n = len(idx)

    for j in range(start_pos, n):
        if days[j] != day:
            break
        if t[j] >= cfg.force_close:
            break
        force_close_bar = j

        # 1) check bar j's own low/high against the stop level fixed BEFORE
        #    bar j (i.e. using only bars < j) -- this is what keeps the trail
        #    causal, see the module docstring.
        stop_hit = (low[j] <= current_stop) if direction == "long" else (high[j] >= current_stop)
        if stop_hit:
            return dict(exit_time=idx[j], exit_price=current_stop,
                       exit_reason=("trail" if armed else "stop"), trail_armed=armed,
                       max_favorable_extreme=extreme, found_exit=True)

        # 2) bar j survived -- NOW update the running extreme with bar j's OWN
        #    high/low, and possibly arm/ratchet the trail for bar j+1 onward.
        if direction == "long":
            extreme = max(extreme, high[j])
            if not armed and (extreme - entry_price) >= trigger_dist:
                armed = True
            if armed:
                current_stop = max(current_stop, extreme - trail_dist)
        else:
            extreme = min(extreme, low[j])
            if not armed and (entry_price - extreme) >= trigger_dist:
                armed = True
            if armed:
                current_stop = min(current_stop, extreme + trail_dist)

    if force_close_bar is None:
        return dict(found_exit=False)
    exit_time = idx[force_close_bar] + pd.Timedelta(minutes=cfg.bar_minutes)
    return dict(exit_time=exit_time, exit_price=close[force_close_bar], exit_reason="time",
               trail_armed=armed, max_favorable_extreme=extreme, found_exit=True)


def simulate(bars: pd.DataFrame, cfg: GoldChandelierConfig = GOLD_CHANDELIER_BASE) -> list[Trade]:
    if len(bars) == 0:
        return []
    london = compute_london_range(bars, cfg)
    ind = add_indicators(bars, cfg)
    n = len(ind)
    idx = ind.index
    t = idx.time
    days = idx.normalize()
    in_entry_window = (t >= cfg.entry_start) & (t < cfg.entry_end)

    high = ind["high"].to_numpy(dtype=float)
    low = ind["low"].to_numpy(dtype=float)
    close = ind["close"].to_numpy(dtype=float)
    ema_arr = ind["ema"].to_numpy(dtype=float)
    atr_arr = ind["atr"].to_numpy(dtype=float)

    trades: list[Trade] = []
    traded_days: set = set()

    for i in range(n - 1):  # need a bar i+1 to start exit monitoring on
        if not in_entry_window[i]:
            continue
        d = days[i]
        if d in traded_days or d not in london.index:
            continue
        lo_hi, lo_lo = london.at[d, "london_high"], london.at[d, "london_low"]
        if pd.isna(lo_hi) or pd.isna(lo_lo) or pd.isna(ema_arr[i]) or pd.isna(atr_arr[i]) or atr_arr[i] <= 0:
            continue

        direction = None
        if close[i] > lo_hi and close[i] > ema_arr[i]:
            direction = "long"
        elif close[i] < lo_lo and close[i] < ema_arr[i]:
            direction = "short"
        if direction is None:
            continue

        # entries fill at the signal bar's own close (see docstring: same
        # idealized convention as S024, kept for comparability)
        entry_price = close[i]
        atr_at_entry = atr_arr[i]
        stop_dist = cfg.stop_atr_mult * atr_at_entry

        if direction == "long":
            initial_stop = entry_price - stop_dist
        else:
            initial_stop = entry_price + stop_dist

        walk = walk_chandelier_exit(idx, high, low, close, t, days, i + 1, d, direction,
                                    entry_price, atr_at_entry, cfg)
        if not walk["found_exit"]:
            continue  # no room left in the session at all -- skip, doesn't consume the day

        exit_time, exit_price = walk["exit_time"], walk["exit_price"]
        exit_reason, armed, extreme = walk["exit_reason"], walk["trail_armed"], walk["max_favorable_extreme"]

        gross = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        cost = entry_price * cfg.cost_bps_roundtrip / BPS_DIVISOR + cfg.cost_price_roundtrip
        net = gross - cost
        r = net / stop_dist if stop_dist > 0 else np.nan

        trades.append(Trade(d, direction, idx[i], idx[i] + pd.Timedelta(minutes=cfg.bar_minutes),
                             entry_price, initial_stop, atr_at_entry, stop_dist,
                             exit_time, exit_price, exit_reason, armed, extreme,
                             lo_hi, lo_lo, ema_arr[i], gross, net, r))
        traded_days.add(d)
    return trades


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    """One row per trade; an empty list still yields the full column set."""
    return pd.DataFrame([t.__dict__ for t in trades], columns=[f.name for f in fields(Trade)])
