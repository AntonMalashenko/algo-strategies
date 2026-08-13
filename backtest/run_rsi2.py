"""Runner for S011 setup 2/6 -- RSI(2) (strategies/rsi2.py).

Pipeline identical to run_double_seven.py, run once per named preset (see
`strategies.rsi2.ALL_RSI2_PRESETS`) so the baseline and both published-rule
variants are gated on equal footing (strategy-modifiers convention: never
silently pick one and discard the other).

Usage:
    python -m backtest.run_rsi2
    python -m backtest.run_rsi2 --commission-bps 0.3 --spread-bps 1.0

Data expected: data/raw/SPY/SPYd1.csv (see scripts/fetch_spy_daily.py).

Gate 0 (no-look-ahead): tests/strategies/test_rsi2.py, all presets.

Gate 1 (walk-forward): per 2026-08-13 decision with Anton (recorded for
Double Seven, applies to the whole S011 EOD/equity family) -- the bar is
"no catastrophic year + positive CAGR/Sharpe over the full fixed-parameter
history", not "zero negative years" (that stricter bar is kept for
intraday R-based strategies). Printed here so the actual per-year numbers
are visible regardless of which bar is applied.

Gate 2 (costs): same conservative default assumption as Double Seven (SPY
is cheap and liquid to trade) -- see that runner's docstring for the
reasoning.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from strategies.rsi2 import ALL_RSI2_PRESETS, RSI2Config, rsi2_signal
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


def compute_strategy_returns(df: pd.DataFrame, cfg: RSI2Config,
                              commission_bps: float = 0.0, spread_bps: float = 0.0) -> pd.DataFrame:
    """Same held/turnover/cost convention as
    `backtest.run_double_seven.compute_strategy_returns` -- see that
    function's docstring for the exact pairing rationale."""
    close = df["close"]
    position = rsi2_signal(df, cfg)
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


def yearly_walkforward(strat_ret: pd.Series) -> pd.DataFrame:
    rows = []
    for year, grp in strat_ret.groupby(strat_ret.index.year):
        eq = (1 + grp).cumprod()
        year_ret = eq.iloc[-1] - 1.0
        dd = (eq / eq.cummax() - 1.0).min()
        rows.append({"year": year, "days": len(grp), "return": year_ret, "max_dd": dd})
    return pd.DataFrame(rows).set_index("year")


def run_preset(df: pd.DataFrame, preset_name: str, cfg: RSI2Config,
                commission_bps: float, spread_bps: float) -> dict:
    print("\n" + "=" * 70)
    print(f"PRESET: {preset_name}  (entry_threshold={cfg.entry_threshold}, "
          f"exit_mode={cfg.exit_mode})")
    print("=" * 70)

    result = compute_strategy_returns(df, cfg, commission_bps=commission_bps, spread_bps=spread_bps)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(REPORTS_DIR / f"rsi2_daily_{preset_name}.csv")

    equity = (1 + result["strat_ret"]).cumprod() * 10_000.0
    equity.to_csv(REPORTS_DIR / f"rsi2_equity_{preset_name}.csv", header=["equity"])

    n_trades = int(result["turnover"].sum())
    time_in_market = float(result["held"].mean())
    print(f"{result.index.min().date()}..{result.index.max().date()}, {len(result)} days, "
          f"{n_trades} position changes, {time_in_market:.1%} time in market")
    metrics = make_report(equity, name=f"S011_rsi2_{preset_name}", periods_per_year=PERIODS_PER_YEAR,
                          reports_dir=REPORTS_DIR)

    wf = yearly_walkforward(result["strat_ret"])
    wf.to_csv(REPORTS_DIR / f"rsi2_walkforward_{preset_name}.csv")
    n_negative = int((wf["return"] < 0).sum())
    worst_year = wf["return"].min()
    print(f"Negative years: {n_negative}/{len(wf)}, worst year {worst_year:+.2%}")

    metrics.update({"preset": preset_name, "n_trades": n_trades, "time_in_market": time_in_market,
                     "negative_years": n_negative, "total_years": len(wf), "worst_year": worst_year})
    return metrics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commission-bps", type=float, default=DEFAULT_COMMISSION_BPS)
    ap.add_argument("--spread-bps", type=float, default=DEFAULT_SPREAD_BPS)
    args = ap.parse_args()

    df = load_spy()
    print(f"Data: {DATA_FILE.relative_to(ROOT)} ({len(df)} bars, "
          f"{df.index.min().date()}..{df.index.max().date()})")

    summary_gross, summary_net = [], []
    for name, cfg in ALL_RSI2_PRESETS.items():
        summary_gross.append(run_preset(df, f"{name}_gross", cfg, commission_bps=0.0, spread_bps=0.0))
        summary_net.append(run_preset(df, f"{name}_realistic", cfg,
                                      commission_bps=args.commission_bps, spread_bps=args.spread_bps))

    print("\n" + "=" * 70)
    print("SUMMARY -- realistic costs, all presets")
    print("=" * 70)
    cols = ["preset", "CAGR", "Sharpe", "MaxDD", "n_trades", "time_in_market",
            "negative_years", "total_years", "worst_year"]
    summary_df = pd.DataFrame(summary_net)[cols]
    print(summary_df.to_string(index=False))
    summary_df.to_csv(REPORTS_DIR / "rsi2_preset_comparison.csv", index=False)


if __name__ == "__main__":
    main()
