"""S008 — crypto MTF + ML strategy engine (ported from Trading/tradingbot).

Parametric core in the S007 shape: a frozen config is the single source of
truth, pure indicator/context functions are vendored from the source bot and
regression-tested against it, and the engine reads config to stay a clean state
machine. See strategy-spec-S008 in the AlgoTrading project for the full plan.
"""
from .config import CryptoMTFConfig, BASELINE_S008

__all__ = ["CryptoMTFConfig", "BASELINE_S008"]
