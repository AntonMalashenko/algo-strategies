"""S011 setup 1/6 -- "Double Seven" (Larry Connors, EOD mean-reversion pullback).

Mechanical rules (Connors & Alvarez, "Short Term Trading Strategies That
Work", 2008 -- a widely published, public-domain-documented system). This
description is written from general knowledge of the published system, not
from the project's own research doc: ALGODEV-23 points to
`claude/research-2026-08-12-connors-strategies.md` in the AlgoTrading Claude
Project, which this session could not reach (no tool access to that Project
from this environment -- see the S011 handoff note). Cross-check against
that doc is still owed before promoting past `prototype`.

  1. Trend filter: today's close must be above its own `trend_sma`-day SMA
     (only trade pullbacks inside a long-term uptrend).
  2. Entry: buy at today's close if today's close is the LOWEST close of the
     trailing `window` trading days (today included), AND rule 1 holds.
  3. Exit: sell (go flat) at today's close once today's close is the
     HIGHEST close of the trailing `window` trading days (today included).
  4. Long-only, one open position at a time, no stop-loss / no profit
     target -- the N-day-high close IS the exit rule.
  5. Position size: 100% of equity in or out (all-in / all-out) -- Connors'
     original description. No pyramiding, no partial sizing, no shorts.

Which numbers are standard vs. tunable (per ALGODEV-23's own instruction --
"the 7-day window is the only parameter, don't grid-search beyond that
necessity"):
  - `window` (default 7) is the ONE parameter this project treats as
    walk-forward-checked. It is a FIXED published value here, not fit on
    this data -- walk-forward (backtest/run_double_seven.py) means "is this
    one fixed value profitable in every calendar year", not "pick the best
    window per fold" (that would be curve-fitting 30+ years of a single
    instrument on itself).
  - `trend_sma` (default 200) is a fixed standard constant (Connors'
    published default, also the market's generic long-term-trend
    convention) -- not walk-forwarded. If Gate 1 fails, sensitivity to this
    value is a documented follow-up, not silently folded into the baseline.

Engine is a pure state machine reading `DoubleSevenConfig`; no magic numbers
in the logic (code-architecture skill). Signal convention matches
`strategies/donchian.py`: this module returns the DECIDED position at each
bar t using only information up to and including close[t] -- no execution
shift baked in. The caller applies `.shift(1)` before multiplying by
returns (see `backtest/run_double_seven.py::compute_strategy_returns`),
exactly like `run_donchian.py`'s `pos = target.shift(1)`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class DoubleSevenConfig:
    """Single source of truth for Double Seven's two constants.

    window     -- lookback in trading days (today included) for both the
                  entry "N-day low close" and the exit "N-day high close".
                  Connors' published default; this project's ONE
                  walk-forward-checked parameter (see module docstring).
    trend_sma  -- SMA length in trading days for the long-term uptrend
                  filter. Connors' published default; treated as fixed,
                  not walk-forwarded.
    """
    window: int = 7
    trend_sma: int = 200


BASELINE_DOUBLE_SEVEN = DoubleSevenConfig()


def compute_features(df: pd.DataFrame, cfg: DoubleSevenConfig = BASELINE_DOUBLE_SEVEN) -> pd.DataFrame:
    """Trailing-only feature columns used by the entry/exit rules.

    Every column at row t is a function of close[t] and earlier closes only
    (`rolling(...)` windows are trailing and inclusive of t, `SMA` is
    trailing) -- no `.shift(-k)` or any other forward reference anywhere in
    this function. This is what `tests/strategies/test_double_seven.py`'s
    no-look-ahead check (Gate 0) verifies by truncating the input and
    diffing the overlap.
    """
    close = df["close"]
    sma = close.rolling(cfg.trend_sma, min_periods=cfg.trend_sma).mean()
    roll_min = close.rolling(cfg.window, min_periods=cfg.window).min()
    roll_max = close.rolling(cfg.window, min_periods=cfg.window).max()
    return pd.DataFrame({
        "close": close,
        "sma": sma,
        "trend_ok": close > sma,
        "n_day_low": close <= roll_min,
        "n_day_high": close >= roll_max,
    }, index=df.index)


def double_seven_signal(df: pd.DataFrame, cfg: DoubleSevenConfig = BASELINE_DOUBLE_SEVEN) -> pd.Series:
    """Return the DECIDED position (1.0 long / 0.0 flat) for every bar t.

    State machine: enter at close[t] when flat AND trend_ok[t] AND
    n_day_low[t]; exit at close[t] when long AND n_day_high[t]. `position[t]`
    is the holding state AFTER today's decision (i.e. it already reflects a
    same-day entry/exit) -- the caller shifts this by one bar before pairing
    it with returns, so `position[t]` earning `return[t+1]` is what "bought
    at today's close, participate in tomorrow's move" means in this EOD
    accounting (see the caller's docstring for the exact pairing).
    """
    feats = compute_features(df, cfg)
    trend_ok = feats["trend_ok"].to_numpy()
    n_day_low = feats["n_day_low"].to_numpy()
    n_day_high = feats["n_day_high"].to_numpy()

    position = np.zeros(len(df))
    in_position = False
    for i in range(len(df)):
        if in_position:
            if n_day_high[i]:
                in_position = False
        else:
            if trend_ok[i] and n_day_low[i]:
                in_position = True
        position[i] = 1.0 if in_position else 0.0
    return pd.Series(position, index=df.index, name="target_position")
