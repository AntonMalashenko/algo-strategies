"""Раннер бэктеста Donchian breakout на EUR/USD.

Конвейер: данные -> целевая позиция -> исполнение со сдвигом (без look-ahead)
-> учёт издержек -> equity -> метрики и отчёт.

Запуск:
    python -m backtest.run_donchian                 # реальные данные (нужна сеть: yfinance)
    python -m backtest.run_donchian --synthetic     # синтетика, без сети

Издержки заданы реалистично для EUR/USD спота: спред ~0.6 пункта в одну сторону
(cost_bps на сделку в один конец) — при желании подстрой под своего брокера.
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from strategies.donchian import donchian_signal
from utils.data import load_yf, load_csv, synthetic_ohlc, load_fred
from utils.report import make_report

PERIODS_PER_YEAR = 252  # дневки


def backtest(df: pd.DataFrame, n_entry: int = 55, cost_bps: float = 0.6,
             allow_short: bool = True, initial: float = 10_000.0) -> dict:
    """Прогнать бэктест. cost_bps — издержки в базисных пунктах на смену позиции в один конец."""
    target = donchian_signal(df, n_entry=n_entry, allow_short=allow_short)

    # ИСПОЛНЕНИЕ БЕЗ LOOK-AHEAD: позицию, решённую на баре t, применяем к доходности t+1
    pos = target.shift(1).fillna(0.0)

    ret = df["close"].pct_change().fillna(0.0)
    gross = pos * ret

    # издержки: списываем при изменении позиции, пропорционально обороту |Δpos|
    turnover = pos.diff().abs().fillna(0.0)
    costs = turnover * (cost_bps / 1e4)
    net = gross - costs

    equity = (1 + net).cumprod() * initial

    # P&L по сделкам (сегменты постоянной позиции) — для win rate / profit factor
    trade_pnls = _segment_pnls(pos, net)

    metrics = make_report(equity, trade_pnls, name=f"S001_donchian_eurusd_N{n_entry}",
                          periods_per_year=PERIODS_PER_YEAR)
    metrics["N_entry"] = n_entry
    metrics["cost_bps"] = cost_bps
    return {"equity": equity, "net": net, "pos": pos, "metrics": metrics}


def _segment_pnls(pos: pd.Series, net: pd.Series) -> pd.Series:
    """Суммарная доходность внутри каждого отрезка неизменной ненулевой позиции."""
    seg_id = (pos != pos.shift(1)).cumsum()
    pnls = []
    for _, grp in net.groupby(seg_id):
        if pos.loc[grp.index].iloc[0] != 0:
            pnls.append(grp.sum())
    return pd.Series(pnls) if pnls else pd.Series(dtype=float)


def split_is_oos(df: pd.DataFrame, oos_frac: float = 0.3):
    """Разбить на in-sample / out-of-sample по времени (последние oos_frac — OOS)."""
    cut = int(len(df) * (1 - oos_frac))
    return df.iloc[:cut], df.iloc[cut:]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="синтетические данные (без сети)")
    ap.add_argument("--csv", type=str, default=None, help="путь к локальному CSV")
    ap.add_argument("--fred", type=str, nargs="?", const="", default=None,
                    help="FRED DEXUSEU CSV (EUR/USD close-only); без значения — data/raw/DEXUSEU.csv")
    ap.add_argument("--n", type=int, default=55, help="длина канала входа")
    ap.add_argument("--cost-bps", type=float, default=0.6)
    args = ap.parse_args()

    if args.synthetic:
        df = synthetic_ohlc()
        print(f"Данные: синтетика ({len(df)} баров)")
    elif args.fred is not None:
        df = load_fred(args.fred or None)
        print(f"Данные: FRED EUR/USD close-only ({len(df)} баров)")
    elif args.csv:
        df = load_csv(args.csv)
        print(f"Данные: {args.csv} ({len(df)} баров)")
    else:
        df = load_yf("EURUSD=X", start="2005-01-01", interval="1d")
        print(f"Данные: EURUSD=X ({len(df)} баров)")

    is_df, oos_df = split_is_oos(df)
    print(f"\n--- IN-SAMPLE ({is_df.index.min().date()}..{is_df.index.max().date()}) ---")
    backtest(is_df, n_entry=args.n, cost_bps=args.cost_bps)
    print(f"\n--- OUT-OF-SAMPLE ({oos_df.index.min().date()}..{oos_df.index.max().date()}) ---")
    backtest(oos_df, n_entry=args.n, cost_bps=args.cost_bps)


if __name__ == "__main__":
    main()
