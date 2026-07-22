"""Multi-timeframe market context + stage machine for S008.

VENDORED and ADAPTED from `Trading/tradingbot/ml/context.py` (2026-07 snapshot).
Two deliberate changes from the source, nothing else:
  1. Reads tunables from `CryptoMTFConfig` instead of `ContextSettings`
     (field names were kept identical on purpose, so this is a rename only).
  2. Dropped the logger side-effects (backtest is silent / deterministic).
The numeric behaviour of `analyse_timeframe`, `detect_h4_poi`, `h1_confirm_side`
and `_detect_stage` is preserved byte-for-byte; a regression test asserts
identical `build_market_context` output vs the original. Constants that were
bare literals in the source are named here (code-architecture rule) with the
SAME values — they are frozen to match the reference, not tuned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np

from .config import CryptoMTFConfig, BASELINE_S008, TF_D1, TF_H4, TF_H1
from .indicators import sma, ema, rsi, atr

# --- Frozen trend-classifier constants (mirror the vendored source exactly) ---
SMA50_TREND_THRESHOLD = 0.005     # |price-SMA50|/SMA50 to score a side
SMA200_TREND_THRESHOLD = 0.01     # |price-SMA200|/SMA200 to score a side
EMA_SLOPE_THRESHOLD = 0.001       # EMA20 5-bar slope to score a side
RSI_BULL_THRESHOLD = 55.0
RSI_BEAR_THRESHOLD = 45.0
NET_TREND_THRESHOLD = 0.15        # |net| above → directional, else NEUTRAL
SCORE_SMA = 1.5                   # weight of each SMA agreement
SCORE_EMA = 1.0                   # weight of EMA-slope agreement
SCORE_HH = 1.5                    # weight of higher-highs / lower-lows
SCORE_HL = 1.0                    # weight of higher-lows / lower-highs
SCORE_RSI = 0.5                   # weight of RSI momentum
STRUCT_WINDOW = 20                # bars for HH/HL structure & S/R
POI_SWING_WINDOW = 50             # bars scanned for swing S/R zones
POI_OB_WINDOW = 30                # bars scanned for order blocks
POI_OB_IMPULSE_MULT = 1.5         # next-candle body vs current to flag an OB


class Trend(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class MarketStage(str, Enum):
    RANGE = "RANGE"
    EARLY_TREND = "EARLY_TREND"
    EXPANSION = "EXPANSION"
    LATE_TREND = "LATE_TREND"
    CONFLICT = "CONFLICT"


@dataclass
class TimeframeAnalysis:
    label: str
    trend: Trend = Trend.NEUTRAL
    strength: float = 0.0
    rsi_value: float = 50.0
    price_vs_sma50: float = 0.0
    price_vs_sma200: float = 0.0
    support: Optional[float] = None
    resistance: Optional[float] = None
    atr_value: float = 0.0
    candles_count: int = 0


@dataclass
class POIZone:
    price_low: float
    price_high: float
    kind: str
    strength: float = 1.0


@dataclass
class MarketContext:
    daily: Optional[TimeframeAnalysis] = None
    h4: Optional[TimeframeAnalysis] = None
    h1: Optional[TimeframeAnalysis] = None
    bias: float = 0.0
    allowed: set[str] = field(default_factory=lambda: {"Buy", "Sell"})
    summary: str = ""
    h4_poi: list[POIZone] = field(default_factory=list)
    h1_confirmed_side: Optional[str] = None
    market_stage: MarketStage = MarketStage.RANGE
    stage_side: Optional[str] = None
    stage_reason: str = ""
    fta_level: Optional[float] = None
    fta_room_pct: float = 0.0
    h4_nascent: bool = False
    d1_h1_conflict: bool = False


def analyse_timeframe(candles: list[dict], label: str) -> TimeframeAnalysis:
    """Trend, strength and key levels for one candle series."""
    result = TimeframeAnalysis(label=label, candles_count=len(candles))
    if len(candles) < 20:
        return result

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    last   = closes[-1]

    sma50_arr  = sma(closes, min(50,  len(closes)))
    sma200_arr = sma(closes, min(200, len(candles)))
    sma50  = float(sma50_arr[-1])  if not np.isnan(sma50_arr[-1])  else last
    sma200 = float(sma200_arr[-1]) if not np.isnan(sma200_arr[-1]) else last

    result.price_vs_sma50  = (last - sma50)  / sma50  if sma50  else 0.0
    result.price_vs_sma200 = (last - sma200) / sma200 if sma200 else 0.0

    ema20_arr = ema(closes, min(20, len(candles)))
    ema20_now  = float(ema20_arr[-1]) if not np.isnan(ema20_arr[-1]) else last
    ema20_prev = float(ema20_arr[-6]) if len(ema20_arr) >= 6 and not np.isnan(ema20_arr[-6]) else ema20_now
    ema_slope = (ema20_now - ema20_prev) / ema20_prev if ema20_prev else 0.0

    rsi_arr = rsi(closes, 14)
    result.rsi_value = float(rsi_arr[-1]) if not np.isnan(rsi_arr[-1]) else 50.0

    result.atr_value = float(atr(highs, lows, closes, 14)[-1])

    window = min(STRUCT_WINDOW, len(candles))
    recent_highs = highs[-window:]
    recent_lows  = lows[-window:]
    hh = sum(1 for i in range(1, len(recent_highs)) if recent_highs[i] > recent_highs[i - 1]) / max(len(recent_highs) - 1, 1)
    ll = sum(1 for i in range(1, len(recent_lows))  if recent_lows[i]  < recent_lows[i - 1])  / max(len(recent_lows) - 1, 1)
    hl = sum(1 for i in range(1, len(recent_lows))  if recent_lows[i]  > recent_lows[i - 1])  / max(len(recent_lows) - 1, 1)
    lh = sum(1 for i in range(1, len(recent_highs)) if recent_highs[i] < recent_highs[i - 1]) / max(len(recent_highs) - 1, 1)

    result.support    = float(min(recent_lows))
    result.resistance = float(max(recent_highs))

    bull_score = 0.0
    bear_score = 0.0
    if result.price_vs_sma50 > SMA50_TREND_THRESHOLD:
        bull_score += SCORE_SMA
    elif result.price_vs_sma50 < -SMA50_TREND_THRESHOLD:
        bear_score += SCORE_SMA
    if result.price_vs_sma200 > SMA200_TREND_THRESHOLD:
        bull_score += SCORE_SMA
    elif result.price_vs_sma200 < -SMA200_TREND_THRESHOLD:
        bear_score += SCORE_SMA
    if ema_slope > EMA_SLOPE_THRESHOLD:
        bull_score += SCORE_EMA
    elif ema_slope < -EMA_SLOPE_THRESHOLD:
        bear_score += SCORE_EMA
    bull_score += hh * SCORE_HH + hl * SCORE_HL
    bear_score += ll * SCORE_HH + lh * SCORE_HL
    if result.rsi_value > RSI_BULL_THRESHOLD:
        bull_score += SCORE_RSI
    elif result.rsi_value < RSI_BEAR_THRESHOLD:
        bear_score += SCORE_RSI

    total = bull_score + bear_score
    net = (bull_score - bear_score) / total if total > 0 else 0.0

    if net > NET_TREND_THRESHOLD:
        result.trend = Trend.BULLISH
    elif net < -NET_TREND_THRESHOLD:
        result.trend = Trend.BEARISH
    else:
        result.trend = Trend.NEUTRAL

    result.strength = min(abs(net), 1.0)
    return result


def detect_h4_poi(candles_h4: list[dict], atr_multiplier: float = 1.0) -> list[POIZone]:
    """Detect H4 points of interest (swing S/R + order blocks)."""
    if len(candles_h4) < 10:
        return []

    closes = [c["close"] for c in candles_h4]
    highs  = [c["high"]  for c in candles_h4]
    lows   = [c["low"]   for c in candles_h4]

    atr_arr = atr(highs, lows, closes, 14)
    atr_val = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0.0
    zone_half = atr_val * atr_multiplier

    zones: list[POIZone] = []

    window = min(POI_SWING_WINDOW, len(candles_h4))
    recent = candles_h4[-window:]
    r_highs = [c["high"] for c in recent]
    r_lows  = [c["low"]  for c in recent]

    for i in range(1, len(r_lows) - 1):
        if r_lows[i] < r_lows[i - 1] and r_lows[i] < r_lows[i + 1]:
            lvl = r_lows[i]
            zones.append(POIZone(lvl - zone_half, lvl + zone_half, "support", 0.8))

    for i in range(1, len(r_highs) - 1):
        if r_highs[i] > r_highs[i - 1] and r_highs[i] > r_highs[i + 1]:
            lvl = r_highs[i]
            zones.append(POIZone(lvl - zone_half, lvl + zone_half, "resistance", 0.8))

    ob_window = min(POI_OB_WINDOW, len(candles_h4))
    ob_candles = candles_h4[-ob_window:]
    for i in range(1, len(ob_candles) - 1):
        cur = ob_candles[i]
        nxt = ob_candles[i + 1]
        cur_body = abs(cur["close"] - cur["open"])
        nxt_body = abs(nxt["close"] - nxt["open"])
        if (cur["close"] < cur["open"] and nxt["close"] > nxt["open"]
                and nxt_body > cur_body * POI_OB_IMPULSE_MULT):
            zones.append(POIZone(min(cur["open"], cur["close"]),
                                 max(cur["open"], cur["close"]),
                                 "order_block_bull", 1.0))
        if (cur["close"] > cur["open"] and nxt["close"] < nxt["open"]
                and nxt_body > cur_body * POI_OB_IMPULSE_MULT):
            zones.append(POIZone(min(cur["open"], cur["close"]),
                                 max(cur["open"], cur["close"]),
                                 "order_block_bear", 1.0))

    return zones


def price_in_poi(price: float, zones: list[POIZone], side: str) -> Optional[POIZone]:
    """First POI zone containing `price` and relevant to `side`."""
    relevant_kinds = (
        {"support", "order_block_bull"} if side == "Buy"
        else {"resistance", "order_block_bear"}
    )
    for zone in zones:
        if zone.kind in relevant_kinds and zone.price_low <= price <= zone.price_high:
            return zone
    return None


def h1_confirm_side(h1: Optional[TimeframeAnalysis], cfg: CryptoMTFConfig) -> Optional[str]:
    """H1-confirmed direction, or None."""
    if h1 is None:
        return None
    if (h1.trend == Trend.BULLISH and h1.strength > cfg.h1_confirm_strength_min
            and cfg.h1_confirm_rsi_bull_min < h1.rsi_value < cfg.h1_confirm_rsi_bull_max):
        return "Buy"
    if (h1.trend == Trend.BEARISH and h1.strength > cfg.h1_confirm_strength_min
            and cfg.h1_confirm_rsi_bear_min < h1.rsi_value < cfg.h1_confirm_rsi_bear_max):
        return "Sell"
    return None


def _trend_side(tf: Optional[TimeframeAnalysis], cfg: CryptoMTFConfig) -> Optional[str]:
    if tf is None or tf.trend == Trend.NEUTRAL or tf.strength < cfg.stage_min_strength:
        return None
    return "Buy" if tf.trend == Trend.BULLISH else "Sell"


def _choose_reference_price(reference_price, candles_d1, candles_h4, candles_h1) -> float:
    if reference_price is not None and reference_price > 0:
        return reference_price
    for series in (candles_h1, candles_h4, candles_d1):
        if series:
            last = series[-1].get("close", 0.0)
            if last:
                return float(last)
    return 0.0


def _find_fta_level(ctx: MarketContext, side: str, reference_price: float):
    if reference_price <= 0:
        return None, 0.0, "reference price unavailable"

    candidates: list[float] = []
    for zone in ctx.h4_poi:
        if side == "Buy":
            if zone.price_low > reference_price:
                candidates.append(zone.price_low)
            elif zone.price_high > reference_price:
                candidates.append(zone.price_high)
        else:
            if zone.price_high < reference_price:
                candidates.append(zone.price_high)
            elif zone.price_low < reference_price:
                candidates.append(zone.price_low)

    if not candidates and ctx.h4 is not None:
        if side == "Buy" and ctx.h4.resistance and ctx.h4.resistance > reference_price:
            candidates.append(ctx.h4.resistance)
        elif side == "Sell" and ctx.h4.support and ctx.h4.support < reference_price:
            candidates.append(ctx.h4.support)

    if not candidates and ctx.daily is not None:
        if side == "Buy" and ctx.daily.resistance and ctx.daily.resistance > reference_price:
            candidates.append(ctx.daily.resistance)
        elif side == "Sell" and ctx.daily.support and ctx.daily.support < reference_price:
            candidates.append(ctx.daily.support)

    if not candidates:
        return None, 0.0, "no nearby FTA found"

    level = min(candidates) if side == "Buy" else max(candidates)
    room_pct = ((level - reference_price) / reference_price * 100.0) if side == "Buy" else ((reference_price - level) / reference_price * 100.0)
    return level, max(room_pct, 0.0), f"next H4 FTA via {len(candidates)} candidate(s)"


def _detect_stage(ctx: MarketContext, reference_price: float, cfg: CryptoMTFConfig):
    d1_side = _trend_side(ctx.daily, cfg)
    h4_side = _trend_side(ctx.h4, cfg)
    h1_side = ctx.h1_confirmed_side or _trend_side(ctx.h1, cfg)

    ctx.d1_h1_conflict = bool(
        d1_side and h1_side and d1_side != h1_side and ctx.daily and ctx.h1
        and ctx.daily.strength >= cfg.stage_min_strength and ctx.h1.strength >= cfg.h1_confirm_strength_min
    )
    if ctx.d1_h1_conflict:
        return MarketStage.CONFLICT, None, f"D1={d1_side} vs H1={h1_side}"

    stage_side: Optional[str] = None
    if h4_side is not None and h1_side is not None and h4_side == h1_side:
        stage_side = h1_side
    elif d1_side is not None and h1_side is not None and d1_side == h1_side:
        stage_side = h1_side

    if stage_side is None:
        return MarketStage.RANGE, None, "no clear HTF alignment"

    fta_level, room_pct, fta_reason = _find_fta_level(ctx, stage_side, reference_price)
    ctx.fta_level = fta_level
    ctx.fta_room_pct = room_pct
    ctx.h4_nascent = bool(
        ctx.h4 is not None
        and ctx.h4.trend != Trend.NEUTRAL
        and ctx.h4.strength < cfg.h4_nascent_max_strength
        and h4_side == stage_side
    )

    if room_pct <= 0.0:
        return MarketStage.LATE_TREND, stage_side, f"{fta_reason}; no room to FTA"
    if ctx.h4_nascent and room_pct >= cfg.fta_ready_min_room_pct:
        return MarketStage.EARLY_TREND, stage_side, f"H4 nascent {ctx.h4.strength:.2f}, room_to_FTA={room_pct:.2f}%"
    if ctx.h4 is not None and ctx.h4.strength >= cfg.h4_mature_min_strength:
        return MarketStage.LATE_TREND, stage_side, f"H4 mature {ctx.h4.strength:.2f}, room_to_FTA={room_pct:.2f}%"
    if room_pct <= cfg.fta_late_room_pct:
        return MarketStage.LATE_TREND, stage_side, f"room_to_FTA={room_pct:.2f}% is small"
    return MarketStage.EXPANSION, stage_side, f"{fta_reason}; room_to_FTA={room_pct:.2f}%"


_TREND_SCORE = {Trend.BULLISH: +1.0, Trend.NEUTRAL: 0.0, Trend.BEARISH: -1.0}


def build_market_context(
    candles_d1: list[dict],
    candles_h4: list[dict],
    candles_h1: list[dict],
    reference_price: float | None = None,
    cfg: CryptoMTFConfig = BASELINE_S008,
) -> MarketContext:
    """Aggregate D1/H4/H1 into bias, allowed sides, POI and market stage."""
    ctx = MarketContext()
    ref_price = _choose_reference_price(reference_price, candles_d1, candles_h4, candles_h1)

    ctx.daily = analyse_timeframe(candles_d1, "D1") if len(candles_d1) >= 20 else None
    ctx.h4    = analyse_timeframe(candles_h4, "H4") if len(candles_h4) >= 20 else None
    ctx.h1    = analyse_timeframe(candles_h1, "H1") if len(candles_h1) >= 20 else None

    weights = cfg.tf_weights
    bias = 0.0
    total_weight = 0.0
    for tf_result, key in [(ctx.daily, TF_D1), (ctx.h4, TF_H4), (ctx.h1, TF_H1)]:
        if tf_result is None:
            continue
        w = weights[key]
        score = _TREND_SCORE[tf_result.trend] * tf_result.strength
        bias += score * w
        total_weight += w
    ctx.bias = bias / total_weight if total_weight > 0 else 0.0

    ctx.h4_poi = detect_h4_poi(candles_h4)
    ctx.h1_confirmed_side = h1_confirm_side(ctx.h1, cfg)
    ctx.market_stage, ctx.stage_side, ctx.stage_reason = _detect_stage(ctx, ref_price, cfg)

    if ctx.market_stage in {MarketStage.EARLY_TREND, MarketStage.EXPANSION} and ctx.stage_side:
        ctx.allowed = {ctx.stage_side}
    elif ctx.market_stage == MarketStage.CONFLICT:
        ctx.allowed = set()
    elif ctx.bias > cfg.allowed_buy_bias:
        ctx.allowed = {"Buy"}
    elif ctx.bias < cfg.allowed_sell_bias:
        ctx.allowed = {"Sell"}
    else:
        ctx.allowed = {"Buy", "Sell"}

    return ctx
