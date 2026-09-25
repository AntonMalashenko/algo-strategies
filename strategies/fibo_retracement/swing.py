"""Non-repainting swing (ZigZag) detector for S020.

Direct reuse of the ATR-threshold ZigZag from strategies/s017_elliott.py
(ALGODEV-27) -- the only already-written, regression-verified (max|delta|=0
over 8 future-truncation points) no-look-ahead primitive in this project for
this kind of pivot detection. Per strategy-passport-S020.md section 5:
"прямая зависимость/импорт... не копия" -- this module is a thin adapter,
not a reimplementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pandas as pd

from strategies.s017_elliott import Pivot, PIVOT_HIGH, PIVOT_LOW, atr_series, zigzag

__all__ = ["Pivot", "PIVOT_HIGH", "PIVOT_LOW", "atr_series", "get_swings"]


@dataclass(frozen=True)
class _SwingParams:
    """Duck-typed stand-in for S017Config: zigzag() only ever reads these
    two fields, so S020 forwards just its own two tunables instead of
    depending on the (Elliott-specific) S017Config shape."""
    zz_atr_mult: float
    atr_period: int


def get_swings(df: pd.DataFrame, swing_atr_mult: float, atr_period: int) -> List[Pivot]:
    """Confirmed, non-repainting pivots on ``df`` (any single timeframe)."""
    return zigzag(df, _SwingParams(zz_atr_mult=swing_atr_mult, atr_period=atr_period))
