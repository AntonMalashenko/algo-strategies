"""Движко-независимый отчёт по бэктесту.

Принимает equity-кривую (pandas.Series, индекс — время) и, опционально,
серию P&L по сделкам. Считает стандартные метрики и рисует единый отчёт
(equity, drawdown, распределение доходностей) в reports/.

Зависит только от pandas / numpy / matplotlib (предустановлены).

Пример:
    from utils.report import make_report
    make_report(equity, trade_pnls, name="S001_ma_crossover", periods_per_year=252)
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


def _metrics(equity: pd.Series, ppy: int) -> dict:
    rets = equity.pct_change().dropna()
    total = equity.iloc[-1] / equity.iloc[0]
    years = len(equity) / ppy
    cagr = total ** (1 / years) - 1 if years > 0 else float("nan")
    sharpe = np.sqrt(ppy) * rets.mean() / rets.std() if rets.std() else float("nan")
    downside = rets[rets < 0].std()
    sortino = np.sqrt(ppy) * rets.mean() / downside if downside else float("nan")
    maxdd = (equity / equity.cummax() - 1).min()
    calmar = cagr / abs(maxdd) if maxdd else float("nan")
    return {
        "CAGR": cagr, "Sharpe": sharpe, "Sortino": sortino,
        "MaxDD": maxdd, "Calmar": calmar, "TotalReturn": total - 1,
    }


def make_report(
    equity: pd.Series,
    trade_pnls: pd.Series | None = None,
    name: str = "backtest",
    periods_per_year: int = 252,
    reports_dir: Path | None = None,
) -> dict:
    """Считает метрики, печатает сводку, сохраняет PNG-отчёт. Возвращает метрики."""
    out_dir = Path(reports_dir) if reports_dir else REPORTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    m = _metrics(equity, periods_per_year)
    if trade_pnls is not None and len(trade_pnls):
        m["WinRate"] = float((trade_pnls > 0).mean())
        m["Trades"] = int(len(trade_pnls))
        wins, losses = trade_pnls[trade_pnls > 0], trade_pnls[trade_pnls < 0]
        m["ProfitFactor"] = float(wins.sum() / abs(losses.sum())) if len(losses) else float("nan")

    rets = equity.pct_change().dropna()
    dd = equity / equity.cummax() - 1.0

    fig, ax = plt.subplots(3, 1, figsize=(11, 10), gridspec_kw={"height_ratios": [3, 1.4, 1.4]})
    ax[0].plot(equity.index, equity.values, lw=1.3)
    ax[0].set_title(f"{name} — equity"); ax[0].grid(alpha=0.3)
    ax[1].fill_between(dd.index, dd.values, 0, color="crimson", alpha=0.4)
    ax[1].set_title("Drawdown"); ax[1].grid(alpha=0.3)
    ax[2].hist(rets.values, bins=60, color="steelblue", alpha=0.8)
    ax[2].set_title("Распределение доходностей"); ax[2].grid(alpha=0.3)
    fig.tight_layout()
    png = out_dir / f"{name}.png"
    fig.savefig(png, dpi=120); plt.close(fig)

    pct_keys = {"CAGR", "MaxDD", "TotalReturn", "WinRate"}
    print(f"=== {name} ===")
    for k, v in m.items():
        if k == "Trades":
            print(f"{k:<12}: {v}")
        elif k in pct_keys:
            print(f"{k:<12}: {v:.2%}")
        else:
            print(f"{k:<12}: {v:.2f}")
    print(f"Отчёт: {png}")
    return m


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    idx = pd.date_range("2023-01-01", periods=252 * 2, freq="B")
    eq = (1 + pd.Series(rng.normal(0.0005, 0.01, len(idx)), index=idx)).cumprod() * 10000
    make_report(eq, name="demo")
