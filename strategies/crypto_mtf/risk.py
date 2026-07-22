"""SL-first risk sizing for S008.

VENDORED from `Trading/tradingbot/bot/risk.py` (2026-07 snapshot). Same
SL-distance floors and fixed-USDT sizing; reads `CryptoMTFConfig`. Pure math,
no broker calls.
"""
from __future__ import annotations

import math

from .config import CryptoMTFConfig
from .indicators import atr


def floor_to_step(value: float, step: float) -> float:
    """Round value DOWN to the nearest multiple of step."""
    factor = round(1.0 / step)
    return math.floor(value * factor) / factor


def sl_distance(
    entry_price: float,
    side: str,
    m15_window: list[dict],
    cfg: CryptoMTFConfig,
) -> tuple[float, float]:
    """Return (sl_distance, swing_price) from the three SL floors on M15 candles.

    Floors: sl_pct×entry, ATR(period)×multiplier, |entry − swing(lookback)|.
    Mirrors bot._calc_sl_tp: ATR and swing come from the entry-timeframe (M15)
    candles available at decision time.
    """
    atr_value = 0.0
    swing_price = 0.0
    if len(m15_window) >= cfg.atr_period + 2:
        highs = [c["high"] for c in m15_window]
        lows = [c["low"] for c in m15_window]
        closes = [c["close"] for c in m15_window]
        arr = atr(highs, lows, closes, cfg.atr_period)
        valid = arr[~_isnan(arr)]
        if len(valid):
            atr_value = float(valid[-1])
        lookback = min(cfg.swing_lookback, len(m15_window))
        recent = m15_window[-lookback:]
        swing_price = (min(c["low"] for c in recent) if side == "Buy"
                       else max(c["high"] for c in recent))

    dist = entry_price * cfg.sl_pct
    if atr_value > 0:
        dist = max(dist, atr_value * cfg.atr_multiplier)
    if swing_price > 0:
        dist = max(dist, abs(entry_price - swing_price))
    return dist, swing_price


def calc_sl_tp_qty(
    entry_price: float,
    side: str,
    m15_window: list[dict],
    cfg: CryptoMTFConfig,
) -> tuple[float, float, float]:
    """(sl_price, tp_price, qty) with SL-first sizing. qty=0 → skip (SL too wide)."""
    if entry_price <= 0:
        return 0.0, 0.0, 0.0
    dist, _ = sl_distance(entry_price, side, m15_window, cfg)
    qty_raw = cfg.max_sl_loss_usdt / dist if dist > 0 else 0.0
    qty = floor_to_step(qty_raw, cfg.min_qty)
    if qty < cfg.min_qty:
        return 0.0, 0.0, 0.0
    tp_dist = dist * cfg.reward_ratio
    if side == "Buy":
        sl = entry_price - dist
        tp = entry_price + tp_dist
    else:
        sl = entry_price + dist
        tp = entry_price - tp_dist
    return sl, tp, qty


def _isnan(arr):
    import numpy as np
    return np.isnan(arr)
