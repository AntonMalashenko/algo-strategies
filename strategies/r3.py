"""S011 setup 6/6 -- R3 (Larry Connors-style "gap capitulation" EOD
mean-reversion pullback).

⚠️ LOWEST-CONFIDENCE FORMALIZATION IN S011, EVEN MORE SO THAN RSI4. Per
ALGODEV-23's own text, R3's exact "gap capitulation" criterion is poorly
described even in the sources the project's own research pass had access
to -- this module is a best-effort reconstruction of the general shape of
this genre of setup (a sharp, gap-driven down move inside an uptrend, read
as short-term capitulation/exhaustion), NOT a confirmed rule set. Treat any
backtest result from this module as illustrative of the RECONSTRUCTED
rules, not as a validated result for "R3" as Connors may have actually
defined it.

Reconstruction:

  1. Trend filter: close > SMA(trend_sma) (same filter as the rest of
     S011) -- ASSUMED by analogy, not confirmed for this specific setup.
  2. Entry: buy at today's close if flat, rule 1 holds, today extends a
     run of `lower_low_days` (default 3) CONSECUTIVE lower LOWS (not
     closes -- `low[t] < low[t-1] < ... `, distinguishing this from
     Multiple Days Down's close-based streak), AND (if
     `require_gap_down`) today's open gapped down from yesterday's close
     (open[t] < close[t-1]) -- the "capitulation gap" ALGODEV-23's text
     points at as the under-specified part of this setup.
  3. Exit: sell (go flat) at today's close once close > SMA(exit_sma) --
     same fast mean-reversion exit as `strategies.multi_day`, for
     consistency and to avoid adding yet another free parameter on top of
     an already-uncertain entry.
  4. Long-only, one position at a time, no stop/target, 100% equity
     in/out, no pyramiding -- same convention as the rest of S011.

Two named presets test the one thing ALGODEV-23 flags as specifically
unclear -- whether the capitulation gap is actually required:
  - `gap_capitulation` (baseline): `require_gap_down=True`.
  - `no_gap_filter`: same lower-low streak, `require_gap_down=False` --
    isolates how much of any edge (or lack of one) comes from the gap
    condition specifically vs. the lower-low streak alone.

`trend_sma=200`, `exit_sma=5`, `lower_low_days=3` fixed at the same values
used elsewhere in S011 for consistency, NOT independently confirmed for R3.

Engine is a pure state machine reading `R3Config`. Signal convention
matches the rest of S011: returns the DECIDED position at bar t using only
information up to and including close[t]/open[t] (both known at bar t's
own close); the caller shifts by one bar.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class R3Config:
    """Single source of truth for R3's (reconstructed) constants.

    lower_low_days     -- consecutive lower-low streak length required to
                           enter (measured on `low`, not `close`).
    require_gap_down   -- whether today must also open below yesterday's
                           close (the "capitulation gap") to enter. The
                           specific thing this reconstruction tests.
    trend_sma          -- SMA length for the long-term uptrend filter.
    exit_sma           -- SMA length for the exit.
    """
    lower_low_days: int = 3
    require_gap_down: bool = True
    trend_sma: int = 200
    exit_sma: int = 5


BASELINE_R3 = R3Config()  # gap_capitulation: require_gap_down=True
R3_NO_GAP_FILTER = replace(BASELINE_R3, require_gap_down=False)

ALL_R3_PRESETS = {
    "gap_capitulation": BASELINE_R3,
    "no_gap_filter": R3_NO_GAP_FILTER,
}


def compute_lower_low_streak(low: pd.Series) -> pd.Series:
    """Count of consecutive prior bars (today included) where the low fell
    strictly versus the prior low; resets to 0 on any non-decrease.
    Trailing-only."""
    diff = low.diff()
    down = (diff < 0).to_numpy()
    n = len(low)
    streak = np.zeros(n)
    for i in range(1, n):
        streak[i] = streak[i - 1] + 1 if down[i] else 0.0
    return pd.Series(streak, index=low.index)


def compute_features(df: pd.DataFrame, cfg: R3Config = BASELINE_R3) -> pd.DataFrame:
    """Trailing-only feature columns used by the entry/exit rules."""
    close, low, open_ = df["close"], df["low"], df["open"]
    sma_trend = close.rolling(cfg.trend_sma, min_periods=cfg.trend_sma).mean()
    sma_exit = close.rolling(cfg.exit_sma, min_periods=cfg.exit_sma).mean()
    lower_low_streak = compute_lower_low_streak(low)
    gap_down = open_ < close.shift(1)

    entry_ok = lower_low_streak >= cfg.lower_low_days
    if cfg.require_gap_down:
        entry_ok = entry_ok & gap_down

    return pd.DataFrame({
        "close": close,
        "trend_ok": close > sma_trend,
        "lower_low_streak": lower_low_streak,
        "gap_down": gap_down,
        "entry_ok": entry_ok,
        "exit_ok": close > sma_exit,
    }, index=df.index)


def r3_signal(df: pd.DataFrame, cfg: R3Config = BASELINE_R3) -> pd.Series:
    """Return the DECIDED position (1.0 long / 0.0 flat) for every bar t.

    Same state-machine / same-day-decision convention as the rest of S011.
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
