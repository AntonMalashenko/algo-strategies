"""How do the two S004-intraday dials trade off against each other? (ALGODEV-62)

The EA exposes the portfolio daily cap (`max_trades_per_day`) and the risk per
trade (`risk_pct`) as inputs, because together they set both the return and how
often the prop account dies: the planned worst day is exactly `cap * risk%`
(prop rule 2 makes a full stop -1R), and the firm kills the account at -5% in a
day or -8% from the high-water mark.

backtest/run_s004_daily_cap.py answers "which cap spends a FIXED daily budget
best". This script asks the other question: across the whole (cap, risk) grid,
what does each dial cost in survival? The headline column is therefore busts --
the expected number of blown challenges in 3 years -- not the median payout.

Inputs are the ENGINE trade list (backtest/run_s004_intraday.py `run`) before
the cap, so every cell sees the same signals; the cap is a pure post-filter.
S021's Nasdaq leg is included at its live risk, since the two share one account
and the -5%/-8% limits are counted on the account, not per strategy.

    python -m backtest.run_s004_risk_grid [--oos] [--stress 1.15] [--nas-risk 2.0]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.run_prop_portfolio import (BALANCE, CHALLENGE_FEE, FX_ENGINE_TRADES, NAS_TRADES,
                                         RULES, SEED, TRADING_DAYS, TRUE_OOS_FROM, _boot_idx,
                                         emulate)
from backtest.run_s004_daily_cap import capped, daily_series

CAPS = (1, 2, 3)
RISKS_PCT = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0)
DEFAULT_NAS_RISK_PCT = 2.0      # S021's live risk per trade
RULES_NAME = "FundingPips-like 8/5"
YEARS = TRADING_DAYS // 252


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oos", action="store_true", help=f"only from {TRUE_OOS_FROM.date()}")
    parser.add_argument("--stress", type=float, default=1.0,
                        help="multiply every losing trade by this (slippage stress, e.g. 1.15)")
    parser.add_argument("--nas-risk", type=float, default=DEFAULT_NAS_RISK_PCT,
                        help="S021 risk %% per trade on the same account; 0 runs S004 alone")
    args = parser.parse_args(argv)

    fx = pd.read_csv(FX_ENGINE_TRADES, parse_dates=["day", "time_in"]).rename(columns={"r": "R"})
    nas_trades = pd.read_csv(NAS_TRADES, parse_dates=["day"])
    fx.loc[fx.R < 0, "R"] *= args.stress
    nas_trades.loc[nas_trades.R < 0, "R"] *= args.stress
    nas = nas_trades.groupby("day").R.sum()

    lo, hi = max(nas.index.min(), fx.day.min()), min(nas.index.max(), fx.day.max())
    if args.oos:
        lo = max(lo, TRUE_OOS_FROM)
    calendar = pd.bdate_range(lo, hi)
    fx = fx[(fx.day >= lo) & (fx.day <= hi)]
    a_nas = nas.reindex(calendar, fill_value=0.0).to_numpy() * args.nas_risk

    rules = RULES[RULES_NAME]
    idx = _boot_idx(np.random.default_rng(SEED), len(calendar))
    print(f"calendar {calendar[0].date()}..{calendar[-1].date()} ({len(calendar)} weekdays), "
          f"stress x{args.stress}, S021 leg at {args.nas_risk:.2f}%, {RULES_NAME}, "
          f"${BALANCE:,.0f}, ${CHALLENGE_FEE:.0f} a try, {YEARS}y x {len(idx[0]):,} paths\n")
    print(f"{'cap':>3} {'risk%':>6} {'worstDay%':>10} {'busts/3y':>9} {'P(loss)':>8} "
          f"{'funded<1y':>10} {'median$':>9} {'p5$':>9} {'p95$':>9} {'$/bust':>8}")

    best = None
    for cap in CAPS:
        taken = capped(fx, cap)
        day_r = daily_series(taken, calendar)
        for risk in RISKS_PCT:
            path = day_r * risk + a_nas
            res = emulate(path[idx], path[idx], rules)
            cum = res["cum"][-1]
            busts = res["attempts"] - 1.0
            median = float(np.median(cum))
            row = (cap, risk, path.min(), busts, float((cum < 0).mean()), res["p_funded_1y"],
                   median, float(np.percentile(cum, 5)), float(np.percentile(cum, 95)),
                   median / busts if busts > 0 else float("inf"))
            print(f"{row[0]:>3} {row[1]:>6.2f} {row[2]:>10.2f} {row[3]:>9.2f} {row[4]:>7.1%} "
                  f"{row[5]:>9.1%} {row[6]:>+9.0f} {row[7]:>+9.0f} {row[8]:>+9.0f} "
                  f"{row[9]:>+8.0f}")
            # the cell the maintainer asked for: survive first, earn second --
            # fewest busts among the cells that still make money at the 5th pct
            if row[7] > 0 and (best is None or row[3] < best[3]):
                best = row
        print()

    if best is None:
        print("no cell is profitable at the 5th percentile")
    else:
        print(f"fewest busts among cells profitable even at the 5th percentile: "
              f"cap {best[0]} x {best[1]:.2f}% -- {best[3]:.2f} busts/3y, "
              f"median {best[6]:+,.0f}, p5 {best[7]:+,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
