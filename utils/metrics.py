"""Общие метрики для оценки equity-кривой бэктеста.

Все функции принимают pandas.Series доходностей (returns) с DatetimeIndex,
если не указано иное. periods_per_year задаёт частоту для годовой нормировки
(252 — дневки по акциям, 365 — крипта 24/7, для интрадей укажи число баров в году).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def cagr(equity: pd.Series, periods_per_year: int = 252) -> float:
    if len(equity) < 2:
        return float("nan")
    total = equity.iloc[-1] / equity.iloc[0]
    years = len(equity) / periods_per_year
    return total ** (1 / years) - 1 if years > 0 else float("nan")


def sharpe(returns: pd.Series, periods_per_year: int = 252, rf: float = 0.0) -> float:
    excess = returns - rf / periods_per_year
    std = excess.std()
    if std == 0 or np.isnan(std):
        return float("nan")
    return np.sqrt(periods_per_year) * excess.mean() / std


def sortino(returns: pd.Series, periods_per_year: int = 252, rf: float = 0.0) -> float:
    excess = returns - rf / periods_per_year
    downside = excess[excess < 0].std()
    if downside == 0 or np.isnan(downside):
        return float("nan")
    return np.sqrt(periods_per_year) * excess.mean() / downside


def max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    dd = equity / running_max - 1.0
    return dd.min()


def win_rate(trade_pnls: pd.Series) -> float:
    if len(trade_pnls) == 0:
        return float("nan")
    return (trade_pnls > 0).mean()


def summary(equity: pd.Series, returns: pd.Series, periods_per_year: int = 252) -> dict:
    return {
        "CAGR": cagr(equity, periods_per_year),
        "Sharpe": sharpe(returns, periods_per_year),
        "Sortino": sortino(returns, periods_per_year),
        "MaxDD": max_drawdown(equity),
    }
