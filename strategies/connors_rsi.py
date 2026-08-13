"""S011 setup 3/6 -- ConnorsRSI (Larry Connors, EOD mean-reversion pullback).

Mechanical rules (Connors & Alvarez, "Short Term Trading Strategies That
Work", 2008 and the standalone "ConnorsRSI" whitepaper -- widely published,
public-domain-documented indicator; written from general knowledge, not
cross-checked against the project's own research doc -- see the S011
handoff note, same caveat as double_seven.py and rsi2.py).

ConnorsRSI is a composite of three components, averaged:

    ConnorsRSI = ( RSI(close, rsi_period)
                 + RSI(streak, streak_rsi_period)
                 + PercentRank(1-day return, percent_rank_period) ) / 3

  - `RSI(close, rsi_period)`: classic Wilder RSI of price (period 3 in the
    published indicator -- shorter/twitchier than the standalone RSI(2)
    setup).
  - `streak`: the signed count of consecutive up (or down) closes -- +N
    after N consecutive higher closes, -N after N consecutive lower
    closes, resets to 0 on an unchanged close. `RSI(streak,
    streak_rsi_period)` is the same Wilder RSI FORMULA applied to this
    streak series instead of to price (period 2 in the published
    indicator) -- it measures how "stretched" the current up/down run is
    relative to its own recent history.
  - `PercentRank(1-day return, percent_rank_period)`: today's 1-day % close
    return's percentile rank within the trailing `percent_rank_period`
    days of 1-day returns (0-100) -- how large today's move is relative to
    the recent distribution of moves, independent of direction-of-trend
    bias.

Trading rules on top of the composite (same shape as Double Seven / RSI(2)):

  1. Trend filter: close > SMA(trend_sma).
  2. Entry: buy at today's close if flat, rule 1 holds, and ConnorsRSI <
     entry_threshold.
  3. Exit: sell (go flat) at today's close once ConnorsRSI > exit_threshold.
  4. Long-only, one position at a time, no stop/target, 100% equity
     in/out, no pyramiding -- same convention as the other S011 setups.

Which numbers are standard vs. tunable: `rsi_period=3`, `streak_rsi_period=2`,
`percent_rank_period=100` are the indicator's OWN definition -- fixed, not
strategy parameters to search (changing them would mean testing a
different indicator, not tuning this one). `trend_sma=200` fixed, same
role as elsewhere in S011. `entry_threshold`/`exit_threshold` are a
discrete choice between published pairs (10/70 "standard", 5/90
"aggressive") -- handled as two named presets, gated on equal footing
(strategy-modifiers convention), same approach as `strategies/rsi2.py`.

Engine is a pure state machine reading `ConnorsRSIConfig`; no magic numbers
in the logic. Signal convention matches `double_seven.py` / `rsi2.py` /
`donchian.py`: returns the DECIDED position at bar t using only
information up to and including close[t]; the caller shifts by one bar.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from strategies.rsi2 import wilder_rsi


@dataclass(frozen=True)
class ConnorsRSIConfig:
    """Single source of truth for ConnorsRSI's constants.

    rsi_period           -- Wilder RSI period of price (indicator default 3).
    streak_rsi_period     -- Wilder RSI period applied to the streak series
                              (indicator default 2).
    percent_rank_period   -- trailing lookback for the 1-day-return percentile
                              rank (indicator default 100).
    trend_sma             -- SMA length for the long-term uptrend filter.
    entry_threshold        -- buy when ConnorsRSI < this value.
    exit_threshold          -- sell when ConnorsRSI > this value.
    """
    rsi_period: int = 3
    streak_rsi_period: int = 2
    percent_rank_period: int = 100
    trend_sma: int = 200
    entry_threshold: float = 10.0
    exit_threshold: float = 70.0


BASELINE_CONNORS_RSI = ConnorsRSIConfig()  # entry<10, exit>70 -- "standard" published pair
CONNORS_RSI_AGGRESSIVE = replace(BASELINE_CONNORS_RSI, entry_threshold=5.0, exit_threshold=90.0)

ALL_CONNORS_RSI_PRESETS = {
    "baseline": BASELINE_CONNORS_RSI,
    "aggressive": CONNORS_RSI_AGGRESSIVE,
}


def compute_streak(close: pd.Series) -> pd.Series:
    """Signed consecutive-close-direction streak: +N after N consecutive
    higher closes, -N after N consecutive lower closes, 0 on the first bar
    and on any unchanged close. Trailing-only: streak[t] depends only on
    close[<=t]."""
    diff = close.diff()
    n = len(close)
    streak = np.zeros(n)
    diff_v = diff.to_numpy()
    for i in range(1, n):
        d = diff_v[i]
        if d > 0:
            streak[i] = streak[i - 1] + 1 if streak[i - 1] > 0 else 1.0
        elif d < 0:
            streak[i] = streak[i - 1] - 1 if streak[i - 1] < 0 else -1.0
        else:
            streak[i] = 0.0
    return pd.Series(streak, index=close.index)


def percent_rank(returns: pd.Series, period: int) -> pd.Series:
    """Trailing percentile rank (0-100) of today's value within the last
    `period` values (today included) -- fraction of the window <= today's
    value. Trailing-only (pandas rolling window ends at t)."""
    def _rank(window: np.ndarray) -> float:
        return float((window <= window[-1]).mean() * 100.0)

    return returns.rolling(period, min_periods=period).apply(_rank, raw=True)


def compute_features(df: pd.DataFrame, cfg: ConnorsRSIConfig = BASELINE_CONNORS_RSI) -> pd.DataFrame:
    """Trailing-only feature columns, including the three ConnorsRSI
    components and their average."""
    close = df["close"]
    sma_trend = close.rolling(cfg.trend_sma, min_periods=cfg.trend_sma).mean()

    rsi_price = wilder_rsi(close, cfg.rsi_period)
    streak = compute_streak(close)
    rsi_streak = wilder_rsi(streak, cfg.streak_rsi_period)
    daily_ret = close.pct_change()
    rank = percent_rank(daily_ret, cfg.percent_rank_period)

    connors_rsi = (rsi_price + rsi_streak + rank) / 3.0

    return pd.DataFrame({
        "close": close,
        "trend_ok": close > sma_trend,
        "rsi_price": rsi_price,
        "streak": streak,
        "rsi_streak": rsi_streak,
        "percent_rank": rank,
        "connors_rsi": connors_rsi,
        "entry_ok": connors_rsi < cfg.entry_threshold,
        "exit_ok": connors_rsi > cfg.exit_threshold,
    }, index=df.index)


def connors_rsi_signal(df: pd.DataFrame, cfg: ConnorsRSIConfig = BASELINE_CONNORS_RSI) -> pd.Series:
    """Return the DECIDED position (1.0 long / 0.0 flat) for every bar t.

    Same state-machine / same-day-decision convention as
    `strategies.double_seven.double_seven_signal` and
    `strategies.rsi2.rsi2_signal` -- see those docstrings for the exact
    semantics of `position[t]`.
    """
    feats = compute_features(df, cfg)
    trend_ok = feats["trend_ok"].to_numpy()
    entry_ok = feats["entry_ok"].to_numpy()
    exit_ok = feats["exit_ok"].to_numpy()

    position = np.zeros(len(df))
    in_position = False
    for i in range(len(df)):
        if in_position:
            if exit_ok[i]:
                in_position = False
        else:
            if trend_ok[i] and entry_ok[i]:
                in_position = True
        position[i] = 1.0 if in_position else 0.0
    return pd.Series(position, index=df.index, name="target_position")
