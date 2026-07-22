"""Frozen configuration for the S008 crypto MTF + ML strategy engine.

Single source of truth for every tunable. The engine is a pure state machine
that reads this config — no magic numbers live in engine logic (see the
`code-architecture` skill). Values here mirror the live defaults of the source
bot (`Trading/tradingbot`, `config/settings.py` + the module defaults its code
reads) so `BASELINE_S008` reproduces what the live bot actually did; every
research variant is a separate preset built with `.with_(...)`, never an edit of
the baseline.

Provenance: field defaults trace to tradingbot `RiskSettings`, `AgentSettings`,
`ContextSettings` (config/settings.py) and the stateful labeller
(`ml/stateful_dataset.py`) as of the 2026-07 snapshot studied for S008.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

# --- Fixed structural constants (not tuned; frozen to match the reference) ----
# Bybit interval codes for each timeframe role.
TF_M15 = "15"
TF_H1 = "60"
TF_H4 = "240"
TF_D1 = "D"

# Minutes per interval — used to align M15 entries with HTF context bars.
INTERVAL_MINUTES = {TF_M15: 15, TF_H1: 60, TF_H4: 240, TF_D1: 1440}

# ML three-class label encoding (matches the trained model's classes).
CLASS_SELL = 0
CLASS_HOLD = 1
CLASS_BUY = 2


@dataclass(frozen=True)
class CryptoMTFConfig:
    """All tunables for one backtest configuration of S008."""

    # ── Instrument ────────────────────────────────────────────────────────
    symbol: str = "ETHUSDT"                 # perp symbol; the study sweeps the basket

    # ── Timeframe bias weights (D1 dominates) ─────────────────────────────
    weight_d1: float = 0.50
    weight_h4: float = 0.30
    weight_h1: float = 0.20

    # ── Entry signal / ML threshold ───────────────────────────────────────
    feature_window: int = 30                # M15 candles fed to the feature vector
    confidence_threshold: float = 0.50      # base ML probability needed to act
    adaptive_threshold_min: float = 0.34    # floor when HTF strongly agrees

    # ── Directional bias gate (allowed side from bias) ────────────────────
    allowed_buy_bias: float = 0.08          # bias above → only Buy allowed
    allowed_sell_bias: float = -0.08        # bias below → only Sell allowed

    # ── H1 confirmation tuning ────────────────────────────────────────────
    h1_confirm_strength_min: float = 0.15
    h1_confirm_rsi_bull_min: float = 48.0
    h1_confirm_rsi_bull_max: float = 80.0
    h1_confirm_rsi_bear_min: float = 20.0
    h1_confirm_rsi_bear_max: float = 52.0

    # ── Market-stage machine tuning ───────────────────────────────────────
    stage_min_strength: float = 0.25        # min TF strength to count as a trend side
    h4_nascent_max_strength: float = 0.45   # below → H4 trend is "young"
    h4_mature_min_strength: float = 0.70    # above → H4 trend is "late"
    fta_ready_min_room_pct: float = 0.25    # room to first-trouble-area for EARLY_TREND
    fta_late_room_pct: float = 0.10         # room below this → LATE_TREND

    # ── Trend-overlay (fires only when ML says Hold) ──────────────────────
    trend_min_bias: float = 0.35            # |bias| must exceed this for overlay
    trend_rsi_bull_min: float = 57.0
    trend_rsi_bear_max: float = 43.0

    # ── Adaptive-threshold multipliers (applied to confidence_threshold) ──
    adaptive_multiplier_nascent: float = 0.72
    adaptive_multiplier_early: float = 0.80
    adaptive_multiplier_strong_80: float = 0.65
    adaptive_multiplier_strong_60: float = 0.73
    adaptive_multiplier_strong_40: float = 0.82
    adaptive_multiplier_no_h1: float = 0.78

    # ── Filter toggles ────────────────────────────────────────────────────
    context_enabled: bool = True            # False → ML-only, no HTF layer
    use_h1_confirm_filter: bool = False     # require H1 to confirm signal side
    use_h4_poi_filter: bool = False         # require price inside an H4 POI zone

    # ── Risk / position sizing (SL-first) ─────────────────────────────────
    max_sl_loss_usdt: float = 10.0          # fixed risk per trade in USDT
    sl_pct: float = 0.001                   # SL floor as fraction of entry
    reward_ratio: float = 2.0               # TP distance = SL distance × this
    atr_multiplier: float = 1.5             # SL floor = ATR × this
    atr_period: int = 14
    swing_lookback: int = 10                # structural SL reference window (candles)
    min_qty: float = 0.001                  # exchange lot step; below → skip trade
    max_open_positions: int = 4             # per side (hedge)
    allow_reverse: bool = True
    breakeven_on_reverse: bool = True
    min_profit_to_reverse: float = 0.002    # min favourable move before reacting to reverse
    hedge_mode: bool = False
    leverage: int = 100

    # ── ML labelling (walk-forward training; from ml/stateful_dataset.py) ──
    label_sl_pct: float = 0.003
    label_tp_pct: float = 0.0075
    label_max_forward: int = 16             # candles ahead the labeller simulates (4h)
    label_window: int = 30
    label_atr_period: int = 14

    # ── Costs (Gate 2 — the heart of the "profitable configuration" search) ─
    taker_fee_per_side: float = 0.00055     # Bybit perp taker ≈ 0.055% per side
    half_spread_frac: float = 0.0           # half bid/ask spread as fraction of price
    slippage_frac: float = 0.0              # extra slippage per side as fraction

    # ── Validation windows ────────────────────────────────────────────────
    # Everything on/after this date is the reserved true-OOS tail: not looked at
    # while tuning (mirrors the S004/S005 discipline).
    reserved_oos_start: str = "2025-07-20"

    def with_(self, **changes) -> "CryptoMTFConfig":
        """Return a copy with the given fields replaced (baseline stays frozen)."""
        return dataclasses.replace(self, **changes)

    @property
    def tf_weights(self) -> dict[str, float]:
        return {TF_D1: self.weight_d1, TF_H4: self.weight_h4, TF_H1: self.weight_h1}


# The frozen baseline: reproduces the live bot's default behaviour. Do not edit
# in place — add a named preset via `.with_(...)` for any experiment.
BASELINE_S008 = CryptoMTFConfig()
