"""Near the overall drawdown limit: trade on, stop, or shrink the size? (ALGODEV-62)

The daily limit needs no machinery -- `risk_pct * max_trades_per_day` is a hard
planned worst day, and both legs are sized so their sum clears the firm's -5%.
The OVERALL limit (-10% from the high-water mark) is the one arithmetic says
nothing about: five quiet -2% days breach nothing daily and still kill the
account.

The obvious guard -- stop trading when the remaining room is smaller than the
planned worst day -- has an obvious objection: a frozen account never recovers,
and in a challenge the entry fee is sunk either way, so stopping buys nothing.
This script tests that objection against the alternative: keep trading, but cap
the size of the day to whatever room is left.

Three policies x two thresholds, same bootstrapped paths, same seed:

    none        what the account does today: always the full planned risk
    halt_*      stop for good once the remaining room is under the threshold
    cap_*       scale the day down to `room / threshold`, floor at MIN_SCALE
                (below it the broker's minimum lot makes the trade impossible)

    *_day       the threshold is a whole planned worst day (fires at a 6%
                drawdown of a 10% limit)
    *_trade     the threshold is a single trade's risk, which is what S021's
                live guard does (fires at 8%)

`cap` scales wins as well as losses, because it IS a smaller lot -- the point is
to spend the last few percent gradually rather than in one coin flip.

Read the result with the stress run too (--stress 1.15): scaling is done on the
PLANNED worst day, which is exactly what the EA knows, so a stressed loss can
still overshoot the room it was sized into.

    python -m backtest.run_s004_guard_modes [--oos] [--stress 1.15]
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.run_prop_portfolio import (BALANCE, CHALLENGE_FEE, FX_ENGINE_TRADES, NAS_TRADES,
                                         PAYOUT_CYCLE_DAYS, PAYOUT_SPLIT, RULES, SEED,
                                         TRADING_DAYS, TRUE_OOS_FROM, _boot_idx)
from backtest.run_s004_daily_cap import capped, daily_series

RULES_NAME = "FundingPips-like 8/5"
NAS_RISK_PCT = 2.0               # S021's leg, one trade a day
S004_CAP, S004_RISK_PCT = 2, 1.0  # the exported default: 2 entries x 1%
MIN_SCALE = 0.25                 # below this the minimum lot makes the trade impossible
# The threshold matters more than the policy: "a whole planned day" of room
# fires at a 6% drawdown, which a challenge visits routinely, while "one trade"
# fires at 8% and is rare. S021's live guard uses the latter.
SINGLE_TRADE_PCT = max(NAS_RISK_PCT, S004_RISK_PCT)
MODES = ("none", "halt_day", "halt_trade", "cap_day", "cap_trade")
YEARS = TRADING_DAYS // 252


def emulate(day_pl: np.ndarray, rules, worst_day: float, mode: str) -> dict:
    """run_prop_portfolio.emulate, plus a size policy for the overall limit.

    Phases are kept, so a breach costs another fee and restarts the challenge --
    which is the whole reason freezing is not obviously better than busting.
    """
    p1, p2, daily_limit, max_loss = rules
    n_days, n_sim = day_pl.shape
    phase = np.ones(n_sim, dtype=int)
    eq = np.zeros(n_sim)
    last_pay = np.zeros(n_sim)
    since = np.zeros(n_sim, dtype=int)
    fees = np.full(n_sim, CHALLENGE_FEE)
    paid = np.zeros(n_sim)
    first_funded = np.full(n_sim, -1)
    ever = np.zeros(n_sim, dtype=bool)
    frozen_days = np.zeros(n_sim, dtype=int)
    cum = np.zeros((n_days, n_sim), dtype=np.float32)
    for day in range(n_days):
        room = max_loss - (last_pay - eq)        # % left before the overall limit
        threshold = worst_day if mode.endswith("_day") else SINGLE_TRADE_PCT
        if mode == "none":
            scale = np.ones(n_sim)
        elif mode.startswith("halt"):
            scale = (room >= threshold).astype(float)
        else:
            scale = np.clip(room / threshold, 0.0, 1.0)
            scale = np.where(scale < MIN_SCALE, 0.0, scale)
        frozen_days += scale == 0.0
        today = day_pl[day] * scale
        eq = eq + today
        busted = (today <= daily_limit) | ((last_pay - eq) >= max_loss)
        target = np.where(phase == 1, p1, p2)
        passed = (eq >= target) & (phase != 3) & ~busted
        funded = phase == 3
        since = np.where(funded, since + 1, since)
        do_pay = funded & (since >= PAYOUT_CYCLE_DAYS) & ~busted & (eq > last_pay)
        paid = np.where(do_pay, paid + (eq - last_pay) * PAYOUT_SPLIT / 100.0 * BALANCE, paid)
        last_pay = np.where(do_pay, eq, last_pay)
        since = np.where(do_pay, 0, since)
        fees = np.where(busted, fees + CHALLENGE_FEE, fees)
        phase = np.where(busted, 1, phase)
        eq = np.where(busted, 0.0, eq)
        last_pay = np.where(busted, 0.0, last_pay)
        since = np.where(busted, 0, since)
        to_funded = passed & (phase == 2)
        first_funded = np.where(to_funded & ~ever, day, first_funded)
        ever |= to_funded
        phase = np.where(passed & (phase == 1), 2, phase)
        phase = np.where(to_funded, 3, phase)
        eq = np.where(passed, 0.0, eq)
        last_pay = np.where(to_funded, 0.0, last_pay)
        since = np.where(to_funded, 0, since)
        cum[day] = paid - fees
    final = cum[-1]
    return dict(busts=float((fees / CHALLENGE_FEE).mean()) - 1.0,
                p_funded_1y=float(((first_funded >= 0) & (first_funded < 252)).mean()),
                median=float(np.median(final)), p5=float(np.percentile(final, 5)),
                p95=float(np.percentile(final, 95)), p_loss=float((final < 0).mean()),
                frozen=float(frozen_days.mean() / n_days))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--oos", action="store_true", help=f"only from {TRUE_OOS_FROM.date()}")
    parser.add_argument("--stress", type=float, default=1.0,
                        help="multiply every losing trade by this (slippage stress, e.g. 1.15)")
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

    path = (daily_series(capped(fx, S004_CAP), calendar) * S004_RISK_PCT
            + nas.reindex(calendar, fill_value=0.0).to_numpy() * NAS_RISK_PCT)
    worst_day = S004_CAP * S004_RISK_PCT + NAS_RISK_PCT
    idx = _boot_idx(np.random.default_rng(SEED), len(calendar))
    day_pl = path[idx]
    rules = RULES[RULES_NAME]

    print(f"calendar {calendar[0].date()}..{calendar[-1].date()} ({len(calendar)} weekdays), "
          f"stress x{args.stress}, {RULES_NAME}, ${BALANCE:,.0f}, {YEARS}y x {day_pl.shape[1]:,} "
          f"paths\nS021 {NAS_RISK_PCT:.1f}% + S004 {S004_CAP}x{S004_RISK_PCT:.1f}% -> planned "
          f"worst day {worst_day:.1f}%, overall limit {rules[3]:.0f}%, worst actual day "
          f"{path.min():+.2f}%\n")
    print(f"{'mode':>10} {'busts/3y':>9} {'funded<1y':>10} {'median$':>9} {'p5$':>9} {'p95$':>9} "
          f"{'P(loss)':>8} {'dead days':>10}")
    for mode in MODES:
        row = emulate(day_pl, rules, worst_day, mode)
        print(f"{mode:>10} {row['busts']:>9.2f} {row['p_funded_1y']:>9.1%} {row['median']:>+9.0f} "
              f"{row['p5']:>+9.0f} {row['p95']:>+9.0f} {row['p_loss']:>7.1%} "
              f"{row['frozen']:>9.1%}")
    print("\n'dead days' is the share of days the policy traded nothing. Any policy that\n"
          "stops sizing is ABSORBING: with no trades the equity cannot move, so the room\n"
          "never reopens and the account is finished without ever breaching anything.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
