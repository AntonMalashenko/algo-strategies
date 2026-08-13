"""S011 setup 5/6 -- Multiple Days Up/Down (Larry Connors-style EOD
mean-reversion pullback).

Per ALGODEV-23's own text, this setup is "worse standardized in the
sources" than Double Seven / RSI(2) / ConnorsRSI -- there is no single
widely-agreed value for N (how many consecutive down days trigger entry).
Formalized here from general knowledge of this genre of Connors-style
pullback system, NOT cross-checked against the project's own research doc
(inaccessible from this session -- see the S011 handoff note).

  1. Trend filter: close > SMA(trend_sma) (same filter as the rest of S011).
  2. Entry: buy at today's close if flat, rule 1 holds, and today's close
     extends a run of `n_days` (or more) CONSECUTIVE lower closes
     (close[t] < close[t-1] < close[t-2] < ... for `n_days` steps back).
  3. Exit: sell (go flat) at today's close once close > SMA(exit_sma) -- a
     fast "back above the short-term mean" exit (same shape as
     `strategies.rsi2`'s `sma_exit` preset -- reused here as the ONE exit
     rule rather than adding a second free dimension on top of the
     already-uncertain entry N).
  4. Long-only, one position at a time, no stop/target, 100% equity
     in/out, no pyramiding -- same convention as the rest of S011.

Which numbers are standard vs. tunable: `n_days` is the specific thing
ALGODEV-23 flags as poorly standardized -- handled here as THREE named
presets (3/4/5 consecutive down days, the commonly-cited small range for
this genre), gated on equal footing, rather than a continuous grid search.
`trend_sma=200` and `exit_sma=5` are fixed, same role/value as elsewhere in
S011.

Engine is a pure state machine reading `MultiDayConfig`. Signal convention
matches the rest of S011: returns the DECIDED position at bar t using only
information up to and including close[t]; the caller shifts by one bar.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MultiDayConfig:
    """Single source of truth for Multiple Days Down's constants.

    n_days     -- number of consecutive lower closes required to trigger
                  entry. The one poorly-standardized parameter in this
                  setup (see module docstring) -- tested as 3 named presets.
    trend_sma  -- SMA length for the long-term uptrend filter.
    exit_sma   -- SMA length for the exit ("close rose back above its own
                  short-term mean").
    """
    n_days: int = 3
    trend_sma: int = 200
    exit_sma: int = 5


BASELINE_MULTIDAY = MultiDayConfig(n_days=3)
MULTIDAY_N4 = replace(BASELINE_MULTIDAY, n_days=4)
MULTIDAY_N5 = replace(BASELINE_MULTIDAY, n_days=5)

ALL_MULTIDAY_PRESETS = {
    "n3": BASELINE_MULTIDAY,
    "n4": MULTIDAY_N4,
    "n5": MULTIDAY_N5,
}


def compute_down_streak(close: pd.Series) -> pd.Series:
    """Count of consecutive prior bars (today included) where close fell
    strictly versus the prior close; resets to 0 on any non-decrease.
    Trailing-only: streak[t] depends only on close[<=t]."""
    diff = close.diff()
    down = (diff < 0).to_numpy()
    n = len(close)
    streak = np.zeros(n)
    for i in range(1, n):
        streak[i] = streak[i - 1] + 1 if down[i] else 0.0
    return pd.Series(streak, index=close.index)


def compute_features(df: pd.DataFrame, cfg: MultiDayConfig = BASELINE_MULTIDAY) -> pd.DataFrame:
    """Trailing-only feature columns used by the entry/exit rules."""
    close = df["close"]
    sma_trend = close.rolling(cfg.trend_sma, min_periods=cfg.trend_sma).mean()
    sma_exit = close.rolling(cfg.exit_sma, min_periods=cfg.exit_sma).mean()
    down_streak = compute_down_streak(close)
    return pd.DataFrame({
        "close": close,
        "trend_ok": close > sma_trend,
        "down_streak": down_streak,
        "entry_ok": down_streak >= cfg.n_days,
        "exit_ok": close > sma_exit,
    }, index=df.index)


def multi_day_signal(df: pd.DataFrame, cfg: MultiDayConfig = BASELINE_MULTIDAY) -> pd.Series:
    """Return the DECIDED position (1.0 long / 0.0 flat) for every bar t.

    Same state-machine / same-day-decision convention as the rest of S011
    (`double_seven_signal`, `rsi2_signal`, `connors_rsi_signal`, `rsi4_signal`).
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
