"""Technical indicators and the M15 feature vector for S008.

VENDORED from `Trading/tradingbot/ml/features.py` (2026-07 snapshot). Kept
byte-for-byte in behaviour so the S008 port reproduces the source bot's feature
vector exactly; a regression test asserts identical output against the original
(see backtest/verify_crypto_mtf.py). Do not "improve" these — any change here
breaks reproduction of the live model's inputs. Pure numpy, no project deps.
"""
from __future__ import annotations

import numpy as np


def sma(prices: list[float], period: int) -> np.ndarray:
    """Simple Moving Average."""
    arr = np.array(prices, dtype=float)
    result = np.full_like(arr, np.nan)
    for i in range(period - 1, len(arr)):
        result[i] = arr[i - period + 1 : i + 1].mean()
    return result


def ema(prices: list[float], period: int) -> np.ndarray:
    """Exponential Moving Average."""
    arr = np.array(prices, dtype=float)
    result = np.full_like(arr, np.nan)
    k = 2.0 / (period + 1)
    if len(arr) < period:
        return result
    result[period - 1] = arr[:period].mean()
    for i in range(period, len(arr)):
        result[i] = arr[i] * k + result[i - 1] * (1 - k)
    return result


def rsi(prices: list[float], period: int = 14) -> np.ndarray:
    """Relative Strength Index."""
    arr = np.array(prices, dtype=float)
    result = np.full_like(arr, np.nan)
    if len(arr) < period + 1:
        return result
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, len(arr) - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else np.inf
        result[i + 1] = 100 - 100 / (1 + rs)
    return result


def macd(
    prices: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """MACD line, signal line, histogram."""
    fast_ema = ema(prices, fast)
    slow_ema = ema(prices, slow)
    macd_line = fast_ema - slow_ema
    valid_macd = np.where(np.isnan(macd_line), 0.0, macd_line).tolist()
    signal_line = ema(valid_macd, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(
    prices: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger Bands (upper, middle, lower)."""
    arr = np.array(prices, dtype=float)
    middle = sma(prices, period)
    upper = np.full_like(arr, np.nan)
    lower = np.full_like(arr, np.nan)
    for i in range(period - 1, len(arr)):
        std = arr[i - period + 1 : i + 1].std(ddof=0)
        upper[i] = middle[i] + num_std * std
        lower[i] = middle[i] - num_std * std
    return upper, middle, lower


def atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> np.ndarray:
    """Average True Range."""
    h = np.array(highs, dtype=float)
    l = np.array(lows, dtype=float)
    c = np.array(closes, dtype=float)
    result = np.full(len(c), np.nan)
    if len(c) < 2:
        return result
    tr = np.maximum(h[1:] - l[1:],
         np.maximum(np.abs(h[1:] - c[:-1]),
                    np.abs(l[1:] - c[:-1])))
    if len(tr) < period:
        return result
    result[period] = tr[:period].mean()
    for i in range(period, len(tr)):
        result[i + 1] = (result[i] * (period - 1) + tr[i]) / period
    return result


def build_feature_vector(
    candles: list[dict],
    market_ctx=None,
) -> np.ndarray | None:
    """Build the 19 base (+8 MTF) feature vector from M15 OHLCV candles.

    Identical to the source bot's feature layout (see its docstring); the MTF
    block is appended when `market_ctx` is provided.
    """
    if len(candles) < 30:
        return None

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    opens  = [c["open"]  for c in candles]
    vols   = [c["volume"] for c in candles]

    last = closes[-1]
    if last == 0:
        return None

    sma5  = sma(closes, 5)[-1]
    sma20 = sma(closes, 20)[-1]
    e12   = ema(closes, 12)[-1]
    e26   = ema(closes, 26)[-1]
    rsi14 = rsi(closes, 14)[-1]
    ml, sl, hl = macd(closes)
    bbu, bbm, bbl = bollinger_bands(closes, 20)

    mean_vol = np.mean(vols) or 1.0

    def safe(v: float, default: float = 0.0) -> float:
        return float(v) if not np.isnan(v) else default

    base_features = np.array([
        last / closes[-2] if closes[-2] != 0 else 1.0,            # 0
        (highs[-1] - lows[-1]) / last,                             # 1
        (closes[-1] - opens[-1]) / opens[-1] if opens[-1] else 0,  # 2
        safe(sma5)  / last,                                         # 3
        safe(sma20) / last,                                         # 4
        safe(e12)   / last,                                         # 5
        safe(e26)   / last,                                         # 6
        safe(rsi14) / 100.0,                                        # 7
        safe(ml[-1]) / last,                                        # 8
        safe(sl[-1]) / last,                                        # 9
        safe(hl[-1]) / last,                                        # 10
        safe(bbu[-1]) / last,                                       # 11
        safe(bbl[-1]) / last,                                       # 12
        (safe(bbu[-1]) - safe(bbl[-1])) / (safe(bbm[-1]) or 1),    # 13
        vols[-1] / mean_vol,                                        # 14
        (closes[-1] / closes[-5]  - 1) if len(closes) > 5  else 0, # 15
        (closes[-1] / closes[-10] - 1) if len(closes) > 10 else 0, # 16
        (closes[-1] / closes[-2]  - 1) if len(closes) > 2  else 0, # 17
        (closes[-1] / closes[-4]  - 1) if len(closes) > 4  else 0, # 18
    ], dtype=np.float32)

    if market_ctx is not None:
        _trend_score = {"BULLISH": 1.0, "NEUTRAL": 0.0, "BEARISH": -1.0}

        d1 = market_ctx.daily
        h4 = market_ctx.h4
        h1 = market_ctx.h1

        mtf_features = np.array([
            float(market_ctx.bias),                                          # 19
            _trend_score.get(d1.trend.value, 0.0) if d1 else 0.0,           # 20
            float(d1.strength) if d1 else 0.0,                              # 21
            _trend_score.get(h4.trend.value, 0.0) if h4 else 0.0,           # 22
            float(h4.strength) if h4 else 0.0,                              # 23
            _trend_score.get(h1.trend.value, 0.0) if h1 else 0.0,           # 24
            float(h1.strength) if h1 else 0.0,                              # 25
            float(h1.rsi_value) / 100.0 if h1 else 0.5,                     # 26
        ], dtype=np.float32)

        features = np.concatenate([base_features, mtf_features])
    else:
        features = base_features

    features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)
    return features
