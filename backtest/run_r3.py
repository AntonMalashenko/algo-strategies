"""Runner for S011 setup 6/6 -- R3, reconstructed (strategies/r3.py).

⚠️ LOWEST-CONFIDENCE SETUP IN S011 -- see strategies/r3.py's module
docstring. Treat any number here as illustrative of the RECONSTRUCTED
rules, not a validated result for Connors' actual "R3".

Pipeline identical to the rest of S011's runners, run once per named preset
(`strategies.r3.ALL_R3_PRESETS`: `gap_capitulation` vs `no_gap_filter` --
isolates whether the gap-down condition matters at all).

Usage:
    python -m backtest.run_r3
    python -m backtest.run_r3 --commission-bps 0.3 --spread-bps 1.0
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from strategies.r3 import ALL_R3_PRESETS, R3Config, r3_signal
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


def compute_strategy_returns(df: pd.DataFrame, cfg: R3Config,
                              commission_bps: float = 0.0, spread_bps: float = 0.0) -> pd.DataFrame:
    """Same held/turnover/cost convention as the rest of S011's runners."""
    close = df["close"]
    position = r3_signal(df, cfg)
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
    monthly = (1 + strat_ret).resample("ME").apply(lambda x: x.prod() - 1.0)
    worst_dt = monthly.idxmin()
    return worst_dt, float(monthly.loc[worst_dt])


def worst_drawdown_trade_count(result: pd.DataFrame) -> int:
    equity = (1 + result["strat_ret"]).cumprod()
    dd = equity / equity.cummax() - 1.0
    trough = dd.idxmin()
    peak = equity.loc[:trough].idxmax()
    span = result.loc[peak:trough]
    return int(span["turnover"].sum())


def yearly_walkforward(strat_ret: pd.Series) -> pd.DataFrame:
    rows = []
    for year, grp in strat_ret.groupby(strat_ret.index.year):
        eq = (1 + grp).cumprod()
        year_ret = eq.iloc[-1] - 1.0
        dd = (eq / eq.cummax() - 1.0).min()
        rows.append({"year": year, "days": len(grp), "return": year_ret, "max_dd": dd})
    return pd.DataFrame(rows).set_index("year")


def run_preset(df: pd.DataFrame, preset_name: str, cfg: R3Config,
                commission_bps: float, spread_bps: float) -> dict:
    print("\n" + "=" * 70)
    print(f"PRESET: {preset_name}  (lower_low_days={cfg.lower_low_days}, "
          f"require_gap_down={cfg.require_gap_down})")
    print("=" * 70)

    result = compute_strategy_returns(df, cfg, commission_bps=commission_bps, spread_bps=spread_bps)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    result.to_csv(REPORTS_DIR / f"r3_daily_{preset_name}.csv")

    equity = (1 + result["strat_ret"]).cumprod() * 10_000.0
    equity.to_csv(REPORTS_DIR / f"r3_equity_{preset_name}.csv", header=["equity"])

    n_trades_turnover = int(result["turnover"].sum())
    time_in_market = float(result["held"].mean())
    print(f"{result.index.min().date()}..{result.index.max().date()}, {len(result)} days, "
          f"{n_trades_turnover} position changes, {time_in_market:.1%} time in market")

    pnls = trade_pnls(result)
    if len(pnls) < 10:
        print(f"⚠️  Only {len(pnls)} round-trip trades -- too few for a reliable statistical read; "
              f"reporting anyway per S011's honesty convention, but treat with extra caution.")

    metrics = make_report(equity, name=f"S011_r3_{preset_name}", periods_per_year=PERIODS_PER_YEAR,
                          reports_dir=REPORTS_DIR)

    wr, pf = win_rate_and_profit_factor(pnls)
    worst_dt, worst_ret = worst_month(result["strat_ret"])
    worst_dd_trades = worst_drawdown_trade_count(result)
    print(f"Trades (round-trip): {len(pnls)}, Win rate: {wr:.1%}, Profit factor: {pf:.2f}")
    print(f"WORST MONTH: {worst_dt.strftime('%Y-%m')}  {worst_ret:+.2%}")
    print(f"Trades inside worst drawdown span: {worst_dd_trades} "
          f"({'single stuck position' if worst_dd_trades <= 2 else 'multi-trade stretch'})")

    wf = yearly_walkforward(result["strat_ret"])
    wf.to_csv(REPORTS_DIR / f"r3_walkforward_{preset_name}.csv")
    n_negative = int((wf["return"] < 0).sum())
    worst_year = wf["return"].min()
    print(f"Negative years: {n_negative}/{len(wf)}, worst year {worst_year:+.2%}")

    metrics.update({"preset": preset_name, "n_round_trip_trades": len(pnls), "win_rate": wr,
                     "profit_factor": pf, "worst_month": worst_dt.strftime("%Y-%m"),
                     "worst_month_return": worst_ret, "worst_dd_trades": worst_dd_trades,
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
    print("\n*** RECONSTRUCTED, UNVERIFIED RULES -- see strategies/r3.py module docstring ***")

    summary_net = []
    for name, cfg in ALL_R3_PRESETS.items():
        run_preset(df, f"{name}_gross", cfg, commission_bps=0.0, spread_bps=0.0)
        summary_net.append(run_preset(df, f"{name}_realistic", cfg,
                                      commission_bps=args.commission_bps, spread_bps=args.spread_bps))

    print("\n" + "=" * 70)
    print("SUMMARY -- realistic costs, all presets")
    print("=" * 70)
    cols = ["preset", "CAGR", "Sharpe", "MaxDD", "win_rate", "profit_factor", "worst_month",
            "worst_month_return", "worst_dd_trades", "negative_years", "total_years", "worst_year"]
    summary_df = pd.DataFrame(summary_net)[cols]
    print(summary_df.to_string(index=False))
    summary_df.to_csv(REPORTS_DIR / "r3_preset_comparison.csv", index=False)


if __name__ == "__main__":
    main()
