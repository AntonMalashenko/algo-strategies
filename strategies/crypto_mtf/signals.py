"""HTF filter, trend-overlay and adaptive threshold for S008.

VENDORED and ADAPTED from `Trading/tradingbot/bot/filters/htf.py` (2026-07
snapshot). Changes from the source, nothing else: reads `CryptoMTFConfig`
instead of `ContextSettings` (identical field names) and drops the logger. The
directional/stage/momentum logic is preserved. `effective_bias` uses all three
timeframes with the config weights, which equals `ctx.bias` for the baseline
(the CONTEXT_FILTERS subset feature is not used in the S008 study).
"""
from __future__ import annotations

from typing import Optional

from .config import CryptoMTFConfig, BASELINE_S008
from .context import MarketContext, MarketStage, price_in_poi
from .indicators import rsi, macd, ema

# Trend-overlay confidence shaping (mirror the vendored source exactly).
TREND_CONF_CAP = 0.75            # max confidence for the main trend-overlay branch
TREND_CONF_BIAS_SLOPE = 0.85     # conf = |bias|*slope + base (capped)
TREND_CONF_BIAS_BASE = 0.10
EARLY_CONF_CAP = 0.72            # max confidence for the early-trend branch
EARLY_CONF_BASE = 0.50          # base + room%/100, capped
STRONG_BIAS_80 = 0.80
STRONG_BIAS_60 = 0.60
STRONG_BIAS_40 = 0.40
STRONG_BIAS_70 = 0.70


class HTFFilter:
    """Directional gate + trend overlay + adaptive threshold over HTF context."""

    def __init__(
        self,
        ctx: Optional[MarketContext] = None,
        cfg: CryptoMTFConfig = BASELINE_S008,
        use_h1_confirm: bool | None = None,
        use_h4_poi: bool | None = None,
    ) -> None:
        self._ctx = ctx
        self._cfg = cfg
        self._use_h1_confirm = cfg.use_h1_confirm_filter if use_h1_confirm is None else use_h1_confirm
        self._use_h4_poi = cfg.use_h4_poi_filter if use_h4_poi is None else use_h4_poi

    def update(self, ctx: Optional[MarketContext]) -> None:
        self._ctx = ctx

    def _effective_bias(self) -> float:
        return self._ctx.bias if self._ctx is not None else 0.0

    @property
    def effective_bias(self) -> float:
        return self._effective_bias()

    def _effective_allowed(self) -> set[str]:
        if self._ctx is not None:
            stage = self._ctx.market_stage
            stage_side = self._ctx.stage_side
            if stage in {MarketStage.EARLY_TREND, MarketStage.EXPANSION} and stage_side in {"Buy", "Sell"}:
                return {stage_side}
        b = self._effective_bias()
        if b > self._cfg.allowed_buy_bias:
            return {"Buy"}
        if b < self._cfg.allowed_sell_bias:
            return {"Sell"}
        return {"Buy", "Sell"}

    def allows(self, side: str, price: float = 0.0) -> bool:
        """True if a signal in `side` passes the bias / stage / (opt.) POI+H1 gates."""
        if self._ctx is None:
            return True
        ctx = self._ctx
        stage = ctx.market_stage
        stage_side = ctx.stage_side
        fta_room_pct = float(ctx.fta_room_pct or 0.0)

        if stage in {MarketStage.EARLY_TREND, MarketStage.EXPANSION}:
            if stage_side is None or stage_side != side:
                return False
            if fta_room_pct <= 0.0:
                return False
        elif stage == MarketStage.LATE_TREND:
            return False

        if side not in self._effective_allowed():
            return False

        if self._use_h4_poi and price > 0 and ctx.h4_poi:
            if price_in_poi(price, ctx.h4_poi, side) is None:
                return False

        if self._use_h1_confirm:
            h1_side = ctx.h1_confirmed_side
            if h1_side is not None and h1_side != side:
                return False

        return True

    def adaptive_threshold(self, base: float, min_threshold: float | None = None) -> float:
        """Lower the ML confidence threshold when HTF strongly agrees."""
        if self._ctx is None:
            return base
        cfg = self._cfg
        if min_threshold is None:
            min_threshold = cfg.adaptive_threshold_min
        ctx = self._ctx
        stage = ctx.market_stage
        stage_side = ctx.stage_side
        eff_bias = self._effective_bias()
        bias_abs = abs(eff_bias)
        h1_side = ctx.h1_confirmed_side
        if stage in {MarketStage.EARLY_TREND, MarketStage.EXPANSION} and stage_side == h1_side:
            if ctx.h4_nascent:
                return max(min_threshold, base * cfg.adaptive_multiplier_nascent)
            return max(min_threshold, base * cfg.adaptive_multiplier_early)
        if h1_side is not None:
            if bias_abs >= STRONG_BIAS_80:
                return max(min_threshold, base * cfg.adaptive_multiplier_strong_80)
            if bias_abs >= STRONG_BIAS_60:
                return max(min_threshold + 0.02, base * cfg.adaptive_multiplier_strong_60)
            if bias_abs >= STRONG_BIAS_40:
                return max(min_threshold + 0.05, base * cfg.adaptive_multiplier_strong_40)
            return base
        if bias_abs >= STRONG_BIAS_70 and ctx.h1 is not None:
            if (eff_bias < 0) == (ctx.h1.trend.value == "BEARISH"):
                return max(min_threshold + 0.05, base * cfg.adaptive_multiplier_no_h1)
        return base

    def trend_signal(
        self,
        candles: list[dict],
        min_bias: float | None = None,
        rsi_period: int = 14,
        ema_period: int = 20,
    ) -> tuple[Optional[str], float]:
        """Trend-following overlay (the deterministic entry when ML is Hold)."""
        cfg = self._cfg
        if self._ctx is None or len(candles) < 30:
            return None, 0.0
        if min_bias is None:
            min_bias = cfg.trend_min_bias
        ctx = self._ctx
        stage = ctx.market_stage
        stage_side = ctx.stage_side
        eff_bias = self._effective_bias()
        h1_side = ctx.h1_confirmed_side

        if stage in {MarketStage.EARLY_TREND, MarketStage.EXPANSION} and stage_side == h1_side:
            closes = [c["close"] for c in candles]
            rsi_val = float(rsi(closes, rsi_period)[-1])
            _, _, hist = macd(closes)
            macd_hist = float(hist[-1]) if hist is not None and len(hist) > 0 else 0.0
            ema_val = float(ema(closes, ema_period)[-1])
            close_now = closes[-1]
            bullish_m15 = rsi_val > cfg.trend_rsi_bull_min and macd_hist >= 0 and close_now >= ema_val
            bearish_m15 = rsi_val < cfg.trend_rsi_bear_max and macd_hist <= 0 and close_now <= ema_val
            room_pct = float(ctx.fta_room_pct or 0.0)
            conf = min(EARLY_CONF_CAP, EARLY_CONF_BASE + max(0.0, room_pct) / 100.0)
            if stage_side == "Buy" and bullish_m15:
                return "Buy", round(conf, 3)
            if stage_side == "Sell" and bearish_m15:
                return "Sell", round(conf, 3)

        if abs(eff_bias) < min_bias:
            return None, 0.0
        if h1_side is None:
            return None, 0.0

        closes = [c["close"] for c in candles]
        rsi_val = float(rsi(closes, rsi_period)[-1])
        _, _, hist = macd(closes)
        macd_hist = float(hist[-1]) if hist is not None and len(hist) > 0 else 0.0
        ema_val = float(ema(closes, ema_period)[-1])
        close_now = closes[-1]
        bearish_m15 = rsi_val < cfg.trend_rsi_bear_max and macd_hist < 0 and close_now < ema_val
        bullish_m15 = rsi_val > cfg.trend_rsi_bull_min and macd_hist > 0 and close_now > ema_val
        if h1_side == "Sell" and eff_bias < -min_bias and bearish_m15:
            conf = min(TREND_CONF_CAP, abs(eff_bias) * TREND_CONF_BIAS_SLOPE + TREND_CONF_BIAS_BASE)
            return "Sell", round(conf, 3)
        if h1_side == "Buy" and eff_bias > min_bias and bullish_m15:
            conf = min(TREND_CONF_CAP, abs(eff_bias) * TREND_CONF_BIAS_SLOPE + TREND_CONF_BIAS_BASE)
            return "Buy", round(conf, 3)
        return None, 0.0
