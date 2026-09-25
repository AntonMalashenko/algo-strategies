"""Shared technical-indicator helpers: Wilder's RSI and Wilder's ATR.

Used by strategies/asia_mean_reversion.py (S022), strategies/us_index_breakout.py
(S023) and strategies/gold_session_momentum.py (S024) -- all three bots' specs
name "RSI(14)" / "ATR(14)" without further qualification, which conventionally
means Welles Wilder's original 1978 definitions (smoothed average gain/loss for
RSI, smoothed true range for ATR) -- the formulas most retail charting platforms
use for an indicator named just "RSI(14)"/"ATR(14)". Both use the textbook
SMA-seeded warm-up (first value = simple average of the first `period` true
ranges / gains-losses, not an EWM seeded from bar 0), so they match a reference
platform's numbers exactly, not just asymptotically. Both are strictly causal:
the value at index i is a function of bars 0..i only, never i+1..n -- see the
no-look-ahead tests in tests/strategies/test_indicators.py.

Deliberately NOT reusing strategies/crypto_mtf/indicators.py's rsi(): that
module is an intentionally frozen, byte-for-byte vendor snapshot locked to
reproducing a specific live model's inputs (see its own module docstring,
"Do not improve these") -- importing it here would create an accidental
coupling to a file whose contract is "never change", for an unrelated purpose.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI, SMA-seeded. First `period` values are NaN (warm-up)."""
    n = len(close)
    out = np.full(n, np.nan)
    if n <= period:
        return pd.Series(out, index=close.index)

    c = close.to_numpy(dtype=float)
    delta = np.diff(c)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = gain[:period].mean()
    avg_loss = loss[:period].mean()
    out[period] = _rsi_from_avgs(avg_gain, avg_loss)
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gain[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i]) / period
        out[i + 1] = _rsi_from_avgs(avg_gain, avg_loss)
    return pd.Series(out, index=close.index)


def _rsi_from_avgs(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0  # flat window: no gains or losses
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ATR, SMA-seeded. First `period` values are NaN (warm-up); the
    first true-range bar (index 0, no previous close) is excluded from the seed."""
    n = len(close)
    out = np.full(n, np.nan)
    if n <= period:
        return pd.Series(out, index=close.index)

    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    tr = np.empty(n)
    tr[0] = h[0] - l[0]  # no previous close available at bar 0
    prev_close = c[:-1]
    tr[1:] = np.maximum(h[1:] - l[1:],
                        np.maximum(np.abs(h[1:] - prev_close), np.abs(l[1:] - prev_close)))

    atr = tr[1:period + 1].mean()  # seed excludes bar 0's incomplete TR
    out[period] = atr
    for i in range(period, n - 1):
        atr = (atr * (period - 1) + tr[i + 1]) / period
        out[i + 1] = atr
    return pd.Series(out, index=close.index)
