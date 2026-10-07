"""How long does one clean run through the prop phases take? (ALGODEV-62)

backtest/run_prop_portfolio.py reports `med_days_to_funded`, but that number
counts calendar progress across *restarts*: a path that blows two challenges
before passing carries both failures in its total. That is the right headline
for "what will this cost me", and the wrong one for "how long is a phase".

This script answers the second question. Per phase it measures the first day a
bootstrapped equity path reaches the target, keeping only the paths that got
there before hitting a breach -- i.e. the duration of a *clean* attempt -- and
reports the median plus the share of attempts that stay clean.

Two caveats that matter more than the medians:

  * the firm may require a MINIMUM number of trading days per phase; if it
    does, it -- not the strategy -- sets the floor, and these medians only say
    the target will not be the binding constraint;
  * a fast phase is a fast phase *conditional on surviving it*; read the median
    together with P(clean) and with the bust counts in run_s004_risk_grid.py.

Inputs are the ENGINE trade list (backtest/run_s004_intraday.py `run`) before
the cap, so every variant sees the same signals, and S021's Nasdaq leg, since
the two share one account and the phase target is counted on the account.

    python -m backtest.run_s004_phase_days [--oos] [--stress 1.15] [--nas-risk 2.0]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.run_prop_portfolio import (BALANCE, FX_ENGINE_TRADES, NAS_TRADES, RULES, SEED,
                                         TRADING_DAYS, TRUE_OOS_FROM, _boot_idx)
from backtest.run_s004_daily_cap import capped, daily_series

RULES_NAME = "FundingPips-like 8/5"
DEFAULT_NAS_RISK_PCT = 2.0       # S021's live risk per trade on the shared account
# (cap, S004 risk % per trade) -- both spend the same planned 2% worst day
VARIANTS = ((2, 1.0), (1, 2.0))
YEARS = TRADING_DAYS // 252


def phase_days(path: np.ndarray, idx: np.ndarray, target: float,
               daily_limit: float, max_loss: float) -> tuple[float, float]:
    """Median days to reach `target` %, and the share of paths that get there clean."""
    day_pl = path[idx]                                   # (days, sims) % of balance
    eq = np.cumsum(day_pl, axis=0)
    peak = np.maximum.accumulate(np.vstack([np.zeros((1, eq.shape[1])), eq]))[:-1]
    breach = (day_pl <= daily_limit) | ((peak - eq) >= max_loss)
    big = day_pl.shape[0] + 1                            # "never happened" sentinel
    hit = np.where(eq.max(axis=0) >= target, (eq >= target).argmax(axis=0) + 1, big)
    dead = np.where(breach.any(axis=0), breach.argmax(axis=0) + 1, big)
    clean = hit < dead
    return (float(np.median(hit[clean])) if clean.any() else float("nan")), float(clean.mean())


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

    p1, p2, daily_limit, max_loss = RULES[RULES_NAME]
    idx = _boot_idx(np.random.default_rng(SEED), len(calendar))
    print(f"calendar {calendar[0].date()}..{calendar[-1].date()} ({len(calendar)} weekdays), "
          f"stress x{args.stress}, S021 leg at {args.nas_risk:.2f}%, {RULES_NAME}, "
          f"${BALANCE:,.0f}, {YEARS}y x {idx.shape[1]:,} paths\n")
    print(f"{'cap':>3} {'risk%':>6} {'phase1 d':>9} {'P(clean)':>9} "
          f"{'phase2 d':>9} {'P(clean)':>9} {'both d':>7}")

    for cap, risk in VARIANTS:
        path = daily_series(capped(fx, cap), calendar) * risk + a_nas
        d1, c1 = phase_days(path, idx, p1, daily_limit, max_loss)
        d2, c2 = phase_days(path, idx, p2, daily_limit, max_loss)
        print(f"{cap:>3} {risk:>6.2f} {d1:>9.0f} {c1:>8.1%} {d2:>9.0f} {c2:>8.1%} {d1 + d2:>7.0f}")

    print("\nA clean run is the best case, not the expectation -- run_prop_portfolio.py's\n"
          "med_days_to_funded includes the failed attempts, and is the number to quote.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
