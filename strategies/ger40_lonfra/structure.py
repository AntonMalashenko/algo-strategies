"""Swing / CHoCH structure and 1M FVG zones (no look-ahead).

A swing high at bar j needs its high strictly above the k bars on each side; it
becomes *known* only k bars later (pivot at t-k is confirmed at bar t). This lag
is what keeps the backtest honest. Copied semantically from
pyramid_v2.structure_levels so results reproduce exactly.
"""
from __future__ import annotations

import numpy as np


def structure_levels(highs: np.ndarray, lows: np.ndarray, k: int) -> dict:
    n = len(highs)
    is_sh = np.zeros(n, bool)
    is_sl = np.zeros(n, bool)
    for j in range(k, n - k):
        if (
            highs[j] == highs[j - k : j + k + 1].max()
            and (highs[j] > highs[j - k : j]).all()
            and (highs[j] > highs[j + 1 : j + k + 1]).all()
        ):
            is_sh[j] = True
        if (
            lows[j] == lows[j - k : j + k + 1].min()
            and (lows[j] < lows[j - k : j]).all()
            and (lows[j] < lows[j + 1 : j + k + 1]).all()
        ):
            is_sl[j] = True

    last_sl = np.full(n, np.nan)
    prev_sl = np.full(n, np.nan)
    last_sh = np.full(n, np.nan)
    prev_sh = np.full(n, np.nan)
    bull = np.full(n, np.nan)  # bullish 1M FVG lower edge (stop for longs)
    bear = np.full(n, np.nan)  # bearish 1M FVG upper edge (stop for shorts)
    sls: list[float] = []
    shs: list[float] = []
    cb = np.nan
    cr = np.nan
    for t in range(n):
        piv = t - k
        if piv >= 0:
            if is_sl[piv]:
                sls.append(lows[piv])
            if is_sh[piv]:
                shs.append(highs[piv])
        if sls:
            last_sl[t] = sls[-1]
        if len(sls) >= 2:
            prev_sl[t] = sls[-2]
        if shs:
            last_sh[t] = shs[-1]
        if len(shs) >= 2:
            prev_sh[t] = shs[-2]
        # 3-candle FVG with pivot i=t-1 confirmed at bar t (needs bar t).
        i = t - 1
        if i - 1 >= 0:
            if lows[t] > highs[i - 1]:
                cb = highs[i - 1]
            if highs[t] < lows[i - 1]:
                cr = lows[i - 1]
        bull[t] = cb
        bear[t] = cr
    return dict(
        is_sh=is_sh, is_sl=is_sl,
        last_sl=last_sl, prev_sl=prev_sl,
        last_sh=last_sh, prev_sh=prev_sh,
        bull=bull, bear=bear,
    )
