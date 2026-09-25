"""S024 -- Gold Session Momentum, XAU/USD, M15, 07:00-17:00 UTC.

Spec (claude/strategies-registry.md S024; Bot 3 of Anton's three-bot
"Multi-Factor Portfolio" brief, siblings S022/S023, spec doc
claude/strategy-spec-S022-S024-multifactor-portfolio.md sec 3): XAU/USD, M15,
07:00-17:00 UTC. Breakout of Donchian(20) channel + EMA(200) filter + ATR
Expansion filter. SL=1.5xATR(14), TP=2.5xATR(14).

Design choices made to turn this into mechanical, testable rules (documented
here per the project's convention of recording judgment calls, not silently
guessing -- see strategies/orb_intraday/engine.py's seasonal-anchor note and
strategies/asia_mean_reversion.py's module docstring for the pattern this
follows):

* Donchian(20), causal (turtle-style). donchian_high[i] = max(High[i-20:i]),
  donchian_low[i] = min(Low[i-20:i]) -- the PRIOR 20 M15 bars only; the bar
  being tested is NOT part of the channel it is breaking. (Including it would
  make a close-breakout impossible by construction, since Close[i] <= High[i],
  and any "off by one" variant would let the breakout bar partly define its own
  trigger level.) Long breakout at bar i when Close[i] > donchian_high[i];
  short breakout when Close[i] < donchian_low[i]. Same shift(1)+rolling
  construction as strategies/donchian.py (S001-S003); that module only exposes
  a stop-and-reverse position series (donchian_signal), not a reusable
  channel-level helper, so the two-line channel is computed here directly.
* EMA(200) trend filter: pandas ewm(span=200, adjust=False) of Close -- the
  standard recursive EMA, alpha = 2/(200+1) -- computed continuously over the
  whole M15 series (never reset per session). The first ema_period-1 values
  are masked to NaN as warm-up (min_periods=ema_period), so no trade can use
  an EMA that has seen fewer than 200 bars. LONG breakouts are taken only when
  Close[i] > EMA200[i]; SHORT breakouts only when Close[i] < EMA200[i]. A
  breakout against the filter is simply not taken -- it is never flipped into
  a trade in the opposite direction.
* ATR Expansion filter -- DELIBERATE, DOCUMENTED INTERPRETATION. The spec says
  only "ATR Expansion filter" with no threshold or comparison window. This
  engine implements the standard retail-TA reading "volatility is expanding
  relative to its own recent average":

      ATR(14)[i] > mean(ATR(14)[i-19 .. i])     (i.e. ATR.rolling(20).mean()[i])

  ATR(14) is Wilder's (utils.indicators.wilder_atr); the trailing 20-bar mean
  is a plain SMA of that ATR series and, as specified, includes bar i itself
  (still fully causal -- only bars <= i). No multiplier k > 1 is applied
  (equivalently k = 1.0; the spec doc's "ATR(14) > k x SMA(ATR(14), N)" form
  with k = atr_expansion_mult, N = atr_expansion_window). This is a choice,
  not something the spec pins down -- any other reading (a k > 1 threshold,
  a different N, ATR vs its own value n bars ago) is a separate variant, not
  this baseline.
* Session gate: all indicators are computed continuously across the full M15
  series (same principle as S022's RSI/ATR), but a breakout only counts as an
  ENTRY trigger if its bar's timestamp (= bar OPEN time, utils.histdata
  resample label=left) falls in [07:00, 17:00) UTC. At most ONE trade per UTC
  calendar day: once a trade has fired on day D, no further entries on D,
  whether the first trade is still open or already closed (same "one setup
  per session" convention as S022/S023).
* Entry price = the breakout bar's CLOSE. The signal (Close[i] crossing the
  channel) is only known once bar i closes, and filling at that same close is
  an EXPLICITLY IDEALIZED fill (no latency, no spread-crossing, no slippage on
  a momentum bar, where real slippage tends to be adverse). A live bot would
  fill at best at bar i+1's open, a few seconds later. Treat this as an
  optimistic simplification when reading backtest numbers (same honesty
  convention as S022's docstring).
* SL/TP frozen at entry: stop distance = stop_atr_mult (1.5) x ATR(14)[i],
  target distance = tp_atr_mult (2.5) x ATR(14)[i], using the signal bar's
  ATR (frozen-at-entry risk unit, same convention as strategies/orb_intraday's
  ADR14-at-entry stop and S022's ATR-at-entry stop). Gross reward:risk at the
  target is therefore 2.5/1.5 = 1.667R.
* Exit monitoring starts at bar i+1 (bar i's own range happened BEFORE the
  close-fill, so it cannot hit this trade's SL/TP). Each subsequent M15 bar:
  if the bar's range reaches BOTH the SL and the TP, the SL is assumed to have
  been hit first (conservative, same worst-case ordering as this project's
  other engines). SL/TP fill at their exact levels (no gap-through slippage,
  same convention as S022/S021 -- another optimistic simplification).
* Forced time exit at the SAME UTC day's last in-session bar (the bar opening
  at 16:45, closing at the 17:00 boundary; or the last in-session bar present
  in the data on a short day), at that bar's close, reason "time". This keeps
  risk bounded to one session like S022/S023 -- a deliberate choice for a
  "session momentum" bot, not letting a breakout run overnight or for days.
* Consequence of the two rules above: a breakout on the day's LAST in-session
  bar (16:45) has no later bar left to monitor and would be entered and
  time-exited at the same price, so it is skipped (and does not consume the
  day's one trade -- there is no later in-session bar that day anyway), same
  "no room to enter" rule as S022's last-bar signal.
* Timestamps on Trade: signal_time = the breakout bar's label (open time);
  entry_time = signal_time + one bar (= the bar's close, the actual fill
  clock time); exit_time = the exiting bar's label for "stop"/"tp" (exit
  occurs somewhere inside that bar) and that bar's close (label + one bar,
  normally 17:00) for "time".
* Costs: cost_bps_roundtrip (bps of entry price) plus cost_price_roundtrip
  (absolute, in price units = USD/oz for XAU/USD), both charged once per
  trade. R = net / stop_distance (same R convention as strategies/orb_intraday
  and S022).

Warm-up: EMA200 needs 200 M15 bars (~2 trading days) before the first
possible trade; ATR expansion needs 14 + 20 - 1 bars; Donchian 20.

No look-ahead: every indicator at bar i is a function of bars <= i (Donchian
of bars < i); entries decide on bar i's closed OHLC and fill at that close;
exits are checked from bar i+1 onward, bar by bar. The Gate 0 tests in
tests/strategies/test_gold_session_momentum.py assert this by truncating the
input and checking already-emitted trades are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import time

import numpy as np
import pandas as pd

from utils.indicators import wilder_atr

SESSION_START = time(7, 0)   # UTC, inclusive (bar open time)
SESSION_END = time(17, 0)    # UTC, exclusive (bar open time); forced exit at this boundary
BPS_DIVISOR = 10_000.0


@dataclass(frozen=True)
class GoldMomentumConfig:
    donchian_period: int = 20           # channel = extremes of the PRIOR N bars (current excluded)
    ema_period: int = 200               # trend filter: long only above EMA(N), short only below
    atr_period: int = 14                # Wilder ATR for SL/TP sizing and the expansion filter
    atr_expansion_window: int = 20      # expansion: ATR[i] > mult * SMA(ATR, window)[i]
    atr_expansion_mult: float = 1.0     # 1.0 = plain "ATR above its trailing mean" (documented)
    stop_atr_mult: float = 1.5          # SL distance = mult * ATR(14) at the signal bar, frozen
    tp_atr_mult: float = 2.5            # TP distance = mult * ATR(14) at the signal bar, frozen
    session_start: time = SESSION_START  # entry window start (UTC, inclusive)
    session_end: time = SESSION_END      # entry window end (UTC, exclusive) + forced time exit
    bar_minutes: int = 15               # bar length; fill clock time = signal bar label + this
    cost_bps_roundtrip: float = 1.0     # round-trip cost, bps of entry price, charged once/trade
    cost_price_roundtrip: float = 0.0   # extra round-trip cost in price units (USD/oz), once/trade

    def with_(self, **overrides) -> "GoldMomentumConfig":
        return replace(self, **overrides)


# Frozen baseline -- matches the spec exactly, never edit in place.
GOLD_MOMENTUM_BASE = GoldMomentumConfig()


@dataclass
class Trade:
    day: pd.Timestamp          # UTC calendar day of the signal bar
    direction: str             # "long" | "short"
    signal_time: pd.Timestamp  # breakout bar label (its open time)
    entry_time: pd.Timestamp   # breakout bar close = fill clock time
    entry_price: float
    stop_price: float
    tp_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str           # "stop" | "tp" | "time"
    channel_level: float       # the Donchian level that was broken
    ema_at_entry: float
    atr_at_entry: float
    stop_dist: float
    gross: float
    net: float
    r_multiple: float


def ema(close: pd.Series, period: int) -> pd.Series:
    """Standard recursive EMA (alpha = 2/(period+1), seeded at the first close),
    causal. First period-1 values are NaN (warm-up)."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def add_indicators(bars: pd.DataFrame,
                   cfg: GoldMomentumConfig = GOLD_MOMENTUM_BASE) -> pd.DataFrame:
    """Attach the causal Donchian channel, EMA, ATR, ATR-expansion flag, raw
    breakout flags and filtered entry signals to a copy of the M15 bars."""
    df = bars.copy()
    df["donchian_high"] = df["high"].shift(1).rolling(cfg.donchian_period).max()
    df["donchian_low"] = df["low"].shift(1).rolling(cfg.donchian_period).min()
    df["ema"] = ema(df["close"], cfg.ema_period)
    df["atr"] = wilder_atr(df["high"], df["low"], df["close"], cfg.atr_period)
    df["atr_mean"] = df["atr"].rolling(cfg.atr_expansion_window).mean()
    df["atr_expanding"] = df["atr"] > cfg.atr_expansion_mult * df["atr_mean"]

    # NaN comparisons are False, so warm-up bars can never signal.
    df["breakout_long"] = df["close"] > df["donchian_high"]
    df["breakout_short"] = df["close"] < df["donchian_low"]
    df["trend_long"] = df["close"] > df["ema"]
    df["trend_short"] = df["close"] < df["ema"]
    valid_atr = df["atr"] > 0
    df["long_signal"] = df["breakout_long"] & df["trend_long"] & df["atr_expanding"] & valid_atr
    df["short_signal"] = df["breakout_short"] & df["trend_short"] & df["atr_expanding"] & valid_atr

    t = df.index.time
    df["in_session"] = (t >= cfg.session_start) & (t < cfg.session_end)
    df["day"] = df.index.normalize()
    return df


def _last_session_bar_pos(df: pd.DataFrame) -> np.ndarray:
    """For each bar, the positional index of the last in-session bar of its UTC
    day (-1 if that day has no in-session bar)."""
    pos = np.arange(len(df))
    sess = pd.Series(pos[df["in_session"].to_numpy()],
                     index=df["day"].to_numpy()[df["in_session"].to_numpy()])
    last_by_day = sess.groupby(level=0).max()
    return df["day"].map(last_by_day).fillna(-1).astype(int).to_numpy()


def simulate(bars: pd.DataFrame, cfg: GoldMomentumConfig = GOLD_MOMENTUM_BASE) -> list[Trade]:
    df = add_indicators(bars, cfg)
    n = len(df)
    if n == 0:
        return []
    idx = df.index
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    long_sig = df["long_signal"].to_numpy()
    short_sig = df["short_signal"].to_numpy()
    in_session = df["in_session"].to_numpy()
    days = df["day"].to_numpy()
    last_sess = _last_session_bar_pos(df)
    bar_len = pd.Timedelta(minutes=cfg.bar_minutes)

    trades: list[Trade] = []
    traded_days: set = set()
    for i in range(n):
        if not in_session[i] or days[i] in traded_days:
            continue
        if long_sig[i]:
            direction = "long"
        elif short_sig[i]:
            direction = "short"
        else:
            continue
        end = last_sess[i]
        if end <= i:
            continue  # breakout on the day's last in-session bar -- nothing left to monitor

        entry_price = close[i]
        atr_at_entry = float(df["atr"].iat[i])
        stop_dist = cfg.stop_atr_mult * atr_at_entry
        tp_dist = cfg.tp_atr_mult * atr_at_entry
        if direction == "long":
            stop_price, tp_price = entry_price - stop_dist, entry_price + tp_dist
            channel_level = float(df["donchian_high"].iat[i])
        else:
            stop_price, tp_price = entry_price + stop_dist, entry_price - tp_dist
            channel_level = float(df["donchian_low"].iat[i])

        exit_time, exit_price, exit_reason = idx[end] + bar_len, close[end], "time"
        for j in range(i + 1, end + 1):
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
        cost = entry_price * cfg.cost_bps_roundtrip / BPS_DIVISOR + cfg.cost_price_roundtrip
        net = gross - cost
        r = net / stop_dist if stop_dist > 0 else np.nan

        trades.append(Trade(pd.Timestamp(days[i]), direction, idx[i], idx[i] + bar_len,
                            entry_price, stop_price, tp_price, exit_time, exit_price,
                            exit_reason, channel_level, float(df["ema"].iat[i]),
                            atr_at_entry, stop_dist, gross, net, r))
        traded_days.add(days[i])
    return trades


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    """One row per trade; an empty list still yields the full column set."""
    return pd.DataFrame([t.__dict__ for t in trades], columns=[f.name for f in fields(Trade)])
