"""S011 setup 2/6 -- RSI(2) (Larry Connors, EOD mean-reversion pullback).

Mechanical rules (Connors & Alvarez, "Short Term Trading Strategies That
Work", 2008 -- widely published, public-domain-documented system; written
from general knowledge, not cross-checked against the project's own
`claude/research-2026-08-12-connors-strategies.md`, which this environment
cannot reach -- see the S011 handoff note).

  1. Trend filter: today's close must be above its own `trend_sma`-day SMA
     (same filter as Double Seven -- only trade pullbacks in an uptrend).
  2. Entry: buy at today's close if flat, rule 1 holds, and the `rsi_period`
     -day Wilder RSI of close is below `entry_threshold`.
  3. Exit: sell (go flat) at today's close once EITHER of two published exit
     rules fires (see `RSI2Config.exit_mode` -- the source material is not
     unanimous on which one Connors intended, so both are implemented as
     named presets rather than one being silently picked):
       - `"rsi_exit"`: RSI(rsi_period) rises above `rsi_exit_threshold`.
       - `"sma_exit"`: close rises above its `sma_exit_period`-day SMA (a
         faster, "back to the short-term mean" exit).
  4. Long-only, one open position at a time, no stop-loss / no profit
     target, 100% equity in/out (all-in / all-out), no pyramiding -- same
     position-sizing convention as Double Seven.

Which numbers are standard vs. tunable: `rsi_period=2` is fixed -- it IS
the strategy (not a parameter to search). `trend_sma=200` is fixed, same
status as in Double Seven. `entry_threshold` (published variants: 5
"aggressive", 10 "standard") and `exit_mode` (`rsi_exit` vs `sma_exit`) are
each a SMALL DISCRETE choice between published variants, not a continuous
parameter to grid-search -- handled here as a baseline plus two named
presets (see bottom of this module), gated on equal footing, per the
`strategy-modifiers` skill convention (never silently pick one and discard
the other).

Engine is a pure state machine reading `RSI2Config`; no magic numbers in
the logic. Signal convention matches `strategies/double_seven.py` and
`strategies/donchian.py`: this module returns the DECIDED position at bar t
using only information up to and including close[t]; the caller applies
`.shift(1)` before pairing with returns.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RSI2Config:
    """Single source of truth for RSI(2)'s constants.

    rsi_period         -- Wilder RSI lookback for both entry and (in
                           "rsi_exit" mode) exit. Fixed at 2 -- this is the
                           strategy's defining choice, not a search
                           parameter.
    trend_sma          -- SMA length for the long-term uptrend filter.
                           Fixed, same role as in Double Seven.
    entry_threshold    -- buy when RSI(rsi_period) < this value. Published
                           variants: 5 (aggressive) / 10 (standard).
    exit_mode          -- "rsi_exit" or "sma_exit" (see module docstring).
    rsi_exit_threshold -- used when exit_mode == "rsi_exit": sell when
                           RSI(rsi_period) > this value.
    sma_exit_period    -- used when exit_mode == "sma_exit": sell when
                           close > SMA(sma_exit_period).
    """
    rsi_period: int = 2
    trend_sma: int = 200
    entry_threshold: float = 10.0
    exit_mode: str = "rsi_exit"          # "rsi_exit" | "sma_exit"
    rsi_exit_threshold: float = 70.0
    sma_exit_period: int = 5

    def __post_init__(self):
        if self.exit_mode not in ("rsi_exit", "sma_exit"):
            raise ValueError(f"exit_mode must be 'rsi_exit' or 'sma_exit', got {self.exit_mode!r}")


BASELINE_RSI2 = RSI2Config()  # entry<10, exit RSI(2)>70 -- the "symmetric" published version
RSI2_AGGRESSIVE_ENTRY = replace(BASELINE_RSI2, entry_threshold=5.0)
RSI2_SMA_EXIT = replace(BASELINE_RSI2, exit_mode="sma_exit")

ALL_RSI2_PRESETS = {
    "baseline": BASELINE_RSI2,
    "aggressive_entry": RSI2_AGGRESSIVE_ENTRY,
    "sma_exit": RSI2_SMA_EXIT,
}


def wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    """Classic Wilder-smoothed RSI, trailing-only (no look-ahead).

    Seeded with a simple average of the first `period` gains/losses (the
    standard Wilder seeding), then recursively smoothed
    ``avg = (avg_prev * (period-1) + current) / period`` for every bar
    after the seed -- each value depends only on bars up to and including
    itself.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    n = len(close)
    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)
    if n > period:
        avg_gain[period] = gain.iloc[1:period + 1].mean()
        avg_loss[period] = loss.iloc[1:period + 1].mean()
        gain_v, loss_v = gain.to_numpy(), loss.to_numpy()
        for i in range(period + 1, n):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain_v[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss_v[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = np.where(avg_loss == 0.0, 100.0, rsi)          # no losses at all -> maximally overbought
    rsi = np.where((avg_gain == 0.0) & (avg_loss > 0.0), 0.0, rsi)  # no gains -> maximally oversold
    return pd.Series(rsi, index=close.index)


def compute_features(df: pd.DataFrame, cfg: RSI2Config = BASELINE_RSI2) -> pd.DataFrame:
    """Trailing-only feature columns used by the entry/exit rules."""
    close = df["close"]
    sma_trend = close.rolling(cfg.trend_sma, min_periods=cfg.trend_sma).mean()
    rsi = wilder_rsi(close, cfg.rsi_period)
    sma_exit = close.rolling(cfg.sma_exit_period, min_periods=cfg.sma_exit_period).mean()
    return pd.DataFrame({
        "close": close,
        "trend_ok": close > sma_trend,
        "rsi": rsi,
        "entry_ok": rsi < cfg.entry_threshold,
        "rsi_exit_ok": rsi > cfg.rsi_exit_threshold,
        "sma_exit_ok": close > sma_exit,
    }, index=df.index)


def rsi2_signal(df: pd.DataFrame, cfg: RSI2Config = BASELINE_RSI2) -> pd.Series:
    """Return the DECIDED position (1.0 long / 0.0 flat) for every bar t.

    Same state-machine / same-day-decision convention as
    `strategies.double_seven.double_seven_signal` -- see that function's
    docstring for the exact semantics of `position[t]`.
    """
    feats = compute_features(df, cfg)
    trend_ok = feats["trend_ok"].to_numpy()
    entry_ok = feats["entry_ok"].to_numpy()
    exit_ok = (feats["rsi_exit_ok"] if cfg.exit_mode == "rsi_exit" else feats["sma_exit_ok"]).to_numpy()

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
