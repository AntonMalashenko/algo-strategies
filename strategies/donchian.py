"""Стратегия пробоя канала Дончиана (Donchian breakout), long/short.

Идея (классика trend-following, система «черепах»):
  - верхний канал = максимум high за последние N баров (НЕ включая текущий);
  - нижний канал  = минимум low за последние N баров (НЕ включая текущий);
  - close пробивает верхний канал -> лонг; пробивает нижний -> шорт;
  - позиция удерживается до противоположного сигнала.

Важно про look-ahead: канал строится по high/low, СДВИНУТЫМ на 1 бар назад
(shift(1)) — на баре t мы используем только информацию до t-1 включительно.
Само исполнение сдвигается ещё в раннере (позиция t применяется к доходности t+1).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def donchian_signal(df: pd.DataFrame, n_entry: int = 55, allow_short: bool = True) -> pd.Series:
    """Вернуть серию целевой позиции {-1, 0, +1} по каждому бару (без сдвига исполнения)."""
    high, low, close = df["high"], df["low"], df["close"]
    upper = high.shift(1).rolling(n_entry).max()
    lower = low.shift(1).rolling(n_entry).min()

    long_sig = close > upper
    short_sig = close < lower

    raw = pd.Series(np.nan, index=df.index)
    raw[long_sig] = 1.0
    raw[short_sig] = -1.0 if allow_short else 0.0
    pos = raw.ffill().fillna(0.0)
    return pos.rename("target_position")
