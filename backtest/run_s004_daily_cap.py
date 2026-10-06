"""Does one 2% entry a day beat two 1% entries? (ALGODEV-62, asked 2026-10-06)

S004-intraday caps the portfolio at `max_trades_per_day` entries across all 7
pairs, and the risk per trade is set so the planned worst day is the same -2%
whatever the cap is (prop rule 2 makes a full stop exactly -1R):

    cap 1 -> 2.00% per trade,  cap 2 -> 1.00%,  cap 3 -> 0.67%

So this is not a risk comparison -- every variant risks the same 2% a day. It
asks a different question: is it better to spend that budget on the first
signal of the day, or to split it over the first two (three)?

Inputs are the ENGINE trade list (backtest/run_s004_intraday.py `run`), before
the cap, so every variant sees exactly the same signals. The cap is a pure
post-filter -- a skipped signal consumes nothing -- so re-capping that list is
the same experiment the engine would run.

Reported per variant: standalone R economics, then the prop emulator
(FundingPips-like 8/5, $10k, $100 a try, 3 years) for S004 alone and next to
S021 at 2%.

    python -m backtest.run_s004_daily_cap [--oos]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.run_prop_portfolio import (BALANCE, FX_ENGINE_TRADES, NAS_TRADES, RULES, SEED,
                                         TRADING_DAYS, TRUE_OOS_FROM, _boot_idx, emulate)

WORST_PLANNED_DAY_PCT = 2.0      # the budget every variant must spend, see the docstring
CAPS = (1, 2, 3)
NAS_RISK_PCT = 2.0               # S021's leg when the two run on one account


def capped(trades: pd.DataFrame, cap: int) -> pd.DataFrame:
    ordered = trades.sort_values(["time_in", "symbol"])
    nth = ordered.groupby("day").cumcount() + 1
    return ordered[nth <= cap]


def daily_series(trades: pd.DataFrame, index: pd.DatetimeIndex) -> np.ndarray:
    return trades.groupby("day").R.sum().reindex(index, fill_value=0.0).to_numpy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oos", action="store_true", help=f"only from {TRUE_OOS_FROM.date()}")
    parser.add_argument("--stress", type=float, default=1.0,
                        help="multiply every losing trade by this (slippage stress, e.g. 1.15)")
    args = parser.parse_args(argv)

    fx = pd.read_csv(FX_ENGINE_TRADES, parse_dates=["day", "time_in"]).rename(columns={"r": "R"})
    nas_trades = pd.read_csv(NAS_TRADES, parse_dates=["day"])
    # a cap of 1 gives up intraday diversification, so the stress matters more to it
    fx.loc[fx.R < 0, "R"] *= args.stress
    nas_trades.loc[nas_trades.R < 0, "R"] *= args.stress
    nas = nas_trades.groupby("day").R.sum()
    start = TRUE_OOS_FROM if args.oos else None

    lo = max(nas.index.min(), fx.day.min())
    hi = min(nas.index.max(), fx.day.max())
    if start is not None:
        lo = max(lo, start)
    calendar = pd.bdate_range(lo, hi)
    fx = fx[(fx.day >= lo) & (fx.day <= hi)]
    a_nas = nas.reindex(calendar, fill_value=0.0).to_numpy()

    print(f"calendar {calendar[0].date()}..{calendar[-1].date()} ({len(calendar)} weekdays), "
          f"{len(fx)} S004 signals before the cap, stress x{args.stress}\n")
    print(f"{'cap':>3} {'risk%':>6} {'trades':>7} {'R/trade':>8} {'totalR':>8} {'R/yr':>7} "
          f"{'worstDayR':>10} {'days used':>10}")
    variants = []
    for cap in CAPS:
        taken = capped(fx, cap)
        risk = WORST_PLANNED_DAY_PCT / cap
        day_r = daily_series(taken, calendar)
        years = len(calendar) / 252.0
        print(f"{cap:>3} {risk:>6.2f} {len(taken):>7} {taken.R.mean():>+8.3f} {taken.R.sum():>+8.1f} "
              f"{taken.R.sum() / years:>+7.1f} {day_r.min():>+10.2f} "
              f"{(day_r != 0).sum():>9}")
        variants.append((cap, risk, day_r))

    rules = RULES["FundingPips-like 8/5"]
    idx = _boot_idx(np.random.default_rng(SEED), len(calendar))
    print(f"\nprop emulator, {rules} rules, ${BALANCE:,.0f}, {TRADING_DAYS // 252}y, median $ after 3y")
    print(f"{'cap':>3} {'risk%':>6} | {'S004 alone':>11} {'att':>5} {'P(loss)':>8} "
          f"| {'+S021 2%':>9} {'att':>5} {'P(loss)':>8} {'worst day%':>11}")
    for cap, risk, day_r in variants:
        alone = day_r * risk
        combo = alone + a_nas * NAS_RISK_PCT
        out = []
        for path in (alone, combo):
            res = emulate(path[idx], path[idx], rules)
            cum = res["cum"][-1]
            out.append((float(np.median(cum)), res["attempts"], float((cum < 0).mean())))
        print(f"{cap:>3} {risk:>6.2f} | {out[0][0]:>+11.0f} {out[0][1]:>5.2f} {out[0][2]:>7.1%} "
              f"| {out[1][0]:>+9.0f} {out[1][1]:>5.2f} {out[1][2]:>7.1%} {combo.min():>10.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
