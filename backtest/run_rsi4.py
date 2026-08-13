"""Runner for S011 setup 4/6 -- RSI4 "91% win rate" (strategies/rsi4.py).

⚠️ LOWEST-CONFIDENCE SETUP IN S011 -- see strategies/rsi4.py's module
docstring: the entry/exit rules here are a reconstruction, not a confirmed
source. Treat any number this runner prints as PROVISIONAL.

Mandatory per ALGODEV-23 (the same lesson as S006's unconfirmed 78% win
rate): a high win rate must not be taken at face value. This runner
computes and prints, UNCONDITIONALLY, not just on request:
  - win rate AND profit factor (a high win rate with a bad profit factor
    means many small wins funding a few large losses -- exactly the
    pattern worth catching before believing "91%"),
  - the worst single CALENDAR MONTH return,
  - max drawdown,
  - the standard per-year walk-forward (Gate 1) and realistic-cost
    scenario (Gate 2), same as the rest of S011.

Usage:
    python -m backtest.run_rsi4
    python -m backtest.run_rsi4 --commission-bps 0.3 --spread-bps 1.0
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.rsi4 import ALL_RSI4_PRESETS, RSI4Config, rsi4_signal
from utils.report import make_report

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "raw" / "SPY" / "SPYd1.csv"
REPORTS_DIR = ROOT / "reports"

PERIODS_PER_YEAR = 252
DEFAULT_COMMISSION_BPS = 0.5
DEFAULT_SPREAD_BPS = 1.0


def load_spy() -> pd.DataFrame:
    df = pd.read_csv(DATA_FILE, parse_dates=["date"]).set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def compute_strategy_returns(df: pd.DataFrame, cfg: RSI4Config,
                              commission_bps: float = 0.0, spread_bps: float = 0.0) -> pd.DataFrame:
    """Same held/turnover/cost convention as the rest of S011's runners."""
    close = df["close"]
    position = rsi4_signal(df, cfg)
    daily_ret = close.pct_change()

    held = position.shift(1).fillna(0.0)
    turnover = held.diff().abs()
    turnover.iloc[0] = held.iloc[0]
    cost = turnover * ((commission_bps + spread_bps) / 1e4)

    strat_ret = held * daily_ret - cost
    out = pd.DataFrame({
        "close": close, "position": position, "held": held,
        "daily_ret": daily_ret, "turnover": turnover, "cost": cost,
        "strat_ret": strat_ret,
    })
    return out.dropna(subset=["daily_ret"])


def trade_pnls(result: pd.DataFrame) -> pd.Series:
    """Per-trade P&L (sum of strat_ret over each contiguous held==1 segment)."""
    held = result["held"]
    seg_id = (held != held.shift(1)).cumsum()
    pnls = []
    for _, grp in result.groupby(seg_id):
        if grp["held"].iloc[0] == 1.0:
            pnls.append(grp["strat_ret"].sum())
    return pd.Series(pnls, dtype=float)


def win_rate_and_profit_factor(pnls: pd.Series) -> tuple[float, float]:
    if len(pnls) == 0:
        return float("nan"), float("nan")
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    wr = float((pnls > 0).mean())
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) else float("inf")
    return wr, pf


def worst_month(strat_ret: pd.Series) -> tuple[pd.Timestamp, float]:
    """Worst single calendar-month compounded return -- mandatory check
    before believing any high-win-rate claim (see module docstring)."""
    monthly = (1 + strat_ret).resample("ME").apply(lambda x: x.prod() - 1.0)
    worst_dt = monthly.idxmin()
    return worst_dt, float(monthly.loc[worst_dt])


def yearly_walkforward(strat_ret: pd.Series) -> pd.DataFrame:
    rows = []
    for year, grp in strat_ret.groupby(strat_ret.index.year):
        eq = (1 + grp).cumprod()
        year_ret = eq.iloc[-1] - 1.0
        dd = (eq / eq.cummax() - 1.0).min()
        rows.append({"year": year, "days": len(grp), "return": year_ret, "max_dd": dd})
    return pd.DataFrame(rows).set_index("year")


def run_preset(df: pd.DataFrame, preset_name: str, cfg: RSI4Config,
                commission_bps: float, spread_bps: float) -> dict:
    print("\n" + "=" * 70)
    print(f"PRESET: {preset_name}  (entry_threshold={cfg.entry_threshold}, "
          f"exit_mode={cfg.exit_mode})")
    print("=" * 70)

    result = compute_strategy_returns(df, cfg, commission_bps=commission_bps, spread_bps=spread_bps)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(REPORTS_DIR / f"rsi4_daily_{preset_name}.csv")

    equity = (1 + result["strat_ret"]).cumprod() * 10_000.0
    equity.to_csv(REPORTS_DIR / f"rsi4_equity_{preset_name}.csv", header=["equity"])

    n_trades_turnover = int(result["turnover"].sum())
    time_in_market = float(result["held"].mean())
    print(f"{result.index.min().date()}..{result.index.max().date()}, {len(result)} days, "
          f"{n_trades_turnover} position changes, {time_in_market:.1%} time in market")
    metrics = make_report(equity, name=f"S011_rsi4_{preset_name}", periods_per_year=PERIODS_PER_YEAR,
                          reports_dir=REPORTS_DIR)

    # MANDATORY per ALGODEV-23: win rate + profit factor + worst month, not just average.
    pnls = trade_pnls(result)
    wr, pf = win_rate_and_profit_factor(pnls)
    worst_dt, worst_ret = worst_month(result["strat_ret"])
    print(f"Trades (round-trip): {len(pnls)}, Win rate: {wr:.1%}, Profit factor: {pf:.2f}")
    print(f"WORST MONTH: {worst_dt.strftime('%Y-%m')}  {worst_ret:+.2%}")

    wf = yearly_walkforward(result["strat_ret"])
    wf.to_csv(REPORTS_DIR / f"rsi4_walkforward_{preset_name}.csv")
    n_negative = int((wf["return"] < 0).sum())
    worst_year = wf["return"].min()
    print(f"Negative years: {n_negative}/{len(wf)}, worst year {worst_year:+.2%}")

    metrics.update({"preset": preset_name, "n_round_trip_trades": len(pnls), "win_rate": wr,
                     "profit_factor": pf, "worst_month": worst_dt.strftime("%Y-%m"),
                     "worst_month_return": worst_ret, "negative_years": n_negative,
                     "total_years": len(wf), "worst_year": worst_year})
    return metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commission-bps", type=float, default=DEFAULT_COMMISSION_BPS)
    ap.add_argument("--spread-bps", type=float, default=DEFAULT_SPREAD_BPS)
    args = ap.parse_args()

    df = load_spy()
    print(f"Data: {DATA_FILE.relative_to(ROOT)} ({len(df)} bars, "
          f"{df.index.min().date()}..{df.index.max().date()})")
    print("\n*** RECONSTRUCTED, UNVERIFIED RULES -- see strategies/rsi4.py module docstring ***")

    summary_net = []
    for name, cfg in ALL_RSI4_PRESETS.items():
        run_preset(df, f"{name}_gross", cfg, commission_bps=0.0, spread_bps=0.0)
        summary_net.append(run_preset(df, f"{name}_realistic", cfg,
                                      commission_bps=args.commission_bps, spread_bps=args.spread_bps))

    print("\n" + "=" * 70)
    print("SUMMARY -- realistic costs, all presets")
    print("=" * 70)
    cols = ["preset", "CAGR", "Sharpe", "MaxDD", "win_rate", "profit_factor",
            "worst_month", "worst_month_return", "negative_years", "total_years", "worst_year"]
    summary_df = pd.DataFrame(summary_net)[cols]
    print(summary_df.to_string(index=False))
    summary_df.to_csv(REPORTS_DIR / "rsi4_preset_comparison.csv", index=False)


if __name__ == "__main__":
    main()
