"""Runner for S011 setup 1/6 -- Double Seven (strategies/double_seven.py).

Pipeline: SPY daily OHLC -> decided position (double_seven_signal) ->
execution-shifted position -> daily strategy return -> equity -> per-year
walk-forward report (Gate 1) -> realistic-cost scenario (Gate 2) ->
metrics/report via utils/report.py.

Usage:
    python -m backtest.run_double_seven
    python -m backtest.run_double_seven --commission-bps 0.3 --spread-bps 1.0

Data expected: data/raw/SPY/SPYd1.csv (see scripts/fetch_spy_daily.py),
columns date,open,high,low,close,volume.

Gate 0 (no-look-ahead) lives in tests/strategies/test_double_seven.py, not
here -- this runner assumes Gate 0 already passed.

Gate 1 (walk-forward): the ONE parameter (window=7) is fixed at Connors'
published value, never fit on this data (see strategies/double_seven.py's
module docstring) -- so a per-calendar-year P&L breakdown here IS the
honest out-of-sample check ("is a value nobody tuned on this series
profitable in every year"), not "pick the best year's window". No IS/OOS
date split is used for this reason -- there is no parameter-fitting step to
protect a reserved tail from.

Gate 2 (costs): SPY is one of the most liquid ETFs traded (NBBO spread
typically 1 cent on ~$400-700/share -> well under 1 bp) and a typical
non-payment-for-order-flow US equity broker charges either $0 or a small
per-share commission (e.g. IBKR Pro: ~$0.005/share, $0.35 minimum -> a few
bps of notional per SIDE on a single-share-equivalent trade, less at larger
size). `--commission-bps` and `--spread-bps` default to a conservative
(overstated, not optimistic) round-trip guess; both are charged only on the
day a trade actually executes (turnover), not on every day a position is
merely held.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.double_seven import BASELINE_DOUBLE_SEVEN, DoubleSevenConfig, double_seven_signal
from utils.report import make_report

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "raw" / "SPY" / "SPYd1.csv"
REPORTS_DIR = ROOT / "reports"

PERIODS_PER_YEAR = 252  # trading days/year, daily EOD bars

# Conservative (overstated) Gate 2 defaults -- see module docstring.
DEFAULT_COMMISSION_BPS = 0.5   # round-trip, bps of notional
DEFAULT_SPREAD_BPS = 1.0       # round-trip, bps of notional


def load_spy() -> pd.DataFrame:
    df = pd.read_csv(DATA_FILE, parse_dates=["date"]).set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def compute_strategy_returns(df: pd.DataFrame, cfg: DoubleSevenConfig = BASELINE_DOUBLE_SEVEN,
                              commission_bps: float = 0.0, spread_bps: float = 0.0) -> pd.DataFrame:
    """Turn the decided position into a daily strategy-return series.

    `held[t] = position[t-1]` -- yesterday's DECIDED (post-decision) holding
    state earns today's close-to-close move (`daily_ret[t] =
    close[t]/close[t-1]-1`). This is `double_seven_signal`'s own convention
    (see its docstring) shifted exactly once here, matching
    `run_donchian.py`'s `pos = target.shift(1)`.

    `turnover[t] = |held[t] - held[t-1]|` is 1.0 on the day a NEW holding
    state first earns a return -- i.e. the day after a trade executed at the
    PREVIOUS day's close. Cost is charged against that day's return, which
    is the standard "cost reduces the first return period of the new
    position" convention (mirrors `strategies/fx_carry.py`'s turnover-cost
    treatment).
    """
    close = df["close"]
    position = double_seven_signal(df, cfg)
    daily_ret = close.pct_change()

    held = position.shift(1).fillna(0.0)
    turnover = held.diff().abs()
    turnover.iloc[0] = held.iloc[0]  # first bar: entering from "no prior state" counts as turnover
    cost = turnover * ((commission_bps + spread_bps) / 1e4)

    strat_ret = held * daily_ret - cost
    out = pd.DataFrame({
        "close": close, "position": position, "held": held,
        "daily_ret": daily_ret, "turnover": turnover, "cost": cost,
        "strat_ret": strat_ret,
    })
    return out.dropna(subset=["daily_ret"])


def yearly_walkforward(strat_ret: pd.Series) -> pd.DataFrame:
    """Gate 1: per-calendar-year net return and max drawdown, fixed params."""
    rows = []
    for year, grp in strat_ret.groupby(strat_ret.index.year):
        eq = (1 + grp).cumprod()
        year_ret = eq.iloc[-1] - 1.0
        dd = (eq / eq.cummax() - 1.0).min()
        rows.append({"year": year, "days": len(grp), "return": year_ret, "max_dd": dd})
    return pd.DataFrame(rows).set_index("year")


def run_scenario(df: pd.DataFrame, name: str, cfg: DoubleSevenConfig,
                  commission_bps: float, spread_bps: float) -> pd.DataFrame:
    result = compute_strategy_returns(df, cfg, commission_bps=commission_bps, spread_bps=spread_bps)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(REPORTS_DIR / f"double_seven_daily_{name}.csv")

    equity = (1 + result["strat_ret"]).cumprod() * 10_000.0
    equity.to_csv(REPORTS_DIR / f"double_seven_equity_{name}.csv", header=["equity"])

    n_trades = int(result["turnover"].sum())  # each entry+exit contributes 1.0 each to turnover sum
    print(f"\n--- {name}: full history {result.index.min().date()}..{result.index.max().date()}, "
          f"{len(result)} days, {n_trades} position changes ---")
    make_report(equity, name=f"S011_double_seven_{name}", periods_per_year=PERIODS_PER_YEAR,
                reports_dir=REPORTS_DIR)

    wf = yearly_walkforward(result["strat_ret"])
    wf.to_csv(REPORTS_DIR / f"double_seven_walkforward_{name}.csv")
    print(f"\n--- {name}: per-year walk-forward (Gate 1) ---")
    n_negative_years = int((wf["return"] < 0).sum())
    for year, row in wf.iterrows():
        flag = "  <-- NEGATIVE YEAR" if row["return"] < 0 else ""
        print(f"  {year}: {row['days']:4.0f} days  return {row['return']:+.2%}  "
              f"max_dd {row['max_dd']:.2%}{flag}")
    print(f"  Years with negative return: {n_negative_years} / {len(wf)}")

    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=BASELINE_DOUBLE_SEVEN.window)
    ap.add_argument("--trend-sma", type=int, default=BASELINE_DOUBLE_SEVEN.trend_sma)
    ap.add_argument("--commission-bps", type=float, default=DEFAULT_COMMISSION_BPS)
    ap.add_argument("--spread-bps", type=float, default=DEFAULT_SPREAD_BPS)
    args = ap.parse_args()

    cfg = DoubleSevenConfig(window=args.window, trend_sma=args.trend_sma)
    df = load_spy()
    print(f"Data: {DATA_FILE.relative_to(ROOT)} ({len(df)} bars, "
          f"{df.index.min().date()}..{df.index.max().date()})")
    print(f"Config: window={cfg.window}, trend_sma={cfg.trend_sma}")

    print("\n" + "=" * 70)
    print("SCENARIO 1/2: gross / frictionless")
    print("=" * 70)
    run_scenario(df, "gross", cfg, commission_bps=0.0, spread_bps=0.0)

    print("\n" + "=" * 70)
    print(f"SCENARIO 2/2: realistic costs (commission {args.commission_bps} bps + "
          f"spread {args.spread_bps} bps round-trip) -- Gate 2")
    print("=" * 70)
    run_scenario(df, "realistic", cfg, commission_bps=args.commission_bps, spread_bps=args.spread_bps)


if __name__ == "__main__":
    main()
