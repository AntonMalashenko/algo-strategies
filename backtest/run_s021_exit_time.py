"""S021: what a later time exit would be worth (ALGODEV-61, asked 2026-10-06).

The frozen rule closes at 15:59 New York, one minute before the cash close. The
question was whether holding to the prop firm's day boundary instead (broker
midnight, EET) is better, since the firm's daily drawdown resets there anyway.

Levels, ADR14, direction and entry are taken from the frozen ORB_BASE run and are
NOT recomputed -- only the exit is replayed on the same broker bars, so the
comparison isolates the exit and nothing else. The stop keeps its original
distance; a later cutoff can only convert a "time" exit into a later "time" exit
or into a stop.

Read-only analysis, writes nothing. Run from the repo root:
    python -m backtest.run_s021_exit_time
"""
from __future__ import annotations

import os
from datetime import time
from pathlib import Path

import pandas as pd

from mt5.tools.s021_parity import load_broker_bars
from strategies.orb_intraday.config import ORB_BASE
from strategies.orb_intraday.engine import Trade, simulate

EXPORT = Path(
    os.path.expanduser(
        "~/Library/Application Support/net.metaquotes.wine.metatrader5/drive_c/users/user"
        "/AppData/Roaming/MetaQuotes/Terminal/Common/Files/AlgoTrading/exports"
        "/USTEC_M1_ICMarketsSC-Demo.csv"
    )
)
SERVER_RULE = "EET_US_DST"

# New York clock readings. 16:59 is broker midnight (EET = NY + 7) while US DST is
# on, i.e. the prop firm's day boundary; 23:59 is the whole calendar day.
CUTOFFS = [
    (time(15, 59), "frozen rule, cash close"),
    (time(16, 59), "broker midnight (prop day end, US DST)"),
    (time(17, 59), "+2 h"),
    (time(19, 59), "+4 h"),
    (time(21, 59), "+6 h"),
    (time(23, 59), "end of the NY calendar day"),
]


def replay_exit(m1: pd.DataFrame, trades: list[Trade], cutoff: time) -> pd.DataFrame:
    """Re-run each trade's exit on the same bars, up to `cutoff` on the entry day."""
    rows = []
    for t in trades:
        stop_dist = abs(t.entry_price - t.stop_price)
        end = pd.Timestamp.combine(t.entry_time.date(), cutoff)
        window = m1.loc[(m1.index >= t.entry_time) & (m1.index <= end)]
        if window.empty:
            continue
        exit_price, exit_reason = window["close"].iloc[-1], "time"
        exit_time = window.index[-1]
        for ts, bar in window.iterrows():
            hit = (bar["low"] <= t.stop_price if t.direction == "long"
                   else bar["high"] >= t.stop_price)
            if hit:
                exit_price, exit_reason, exit_time = t.stop_price, "stop", ts
                break
        gross = (exit_price - t.entry_price) if t.direction == "long" else (t.entry_price - exit_price)
        cost = ORB_BASE.cost_bps_roundtrip / 10_000.0 * t.entry_price
        net = gross - cost
        rows.append(
            {
                "day": t.day,
                "direction": t.direction,
                "exit_reason": exit_reason,
                "exit_time": exit_time,
                "net_pts": net,
                "r": net / stop_dist,
            }
        )
    return pd.DataFrame(rows)


def stats(df: pd.DataFrame) -> dict:
    curve = df["r"].cumsum()
    drawdown = (curve.cummax() - curve).max()
    return {
        "trades": len(df),
        "sum_r": df["r"].sum(),
        "mean_r": df["r"].mean(),
        "win_rate": (df["r"] > 0).mean(),
        "stops": (df["exit_reason"] == "stop").sum(),
        "max_dd_r": drawdown,
    }


def main() -> None:
    m1 = load_broker_bars(EXPORT, SERVER_RULE)
    trades = simulate(m1, ORB_BASE)
    print(f"bars {len(m1):,}  {m1.index[0].date()}..{m1.index[-1].date()}  trades {len(trades)}")

    baseline = replay_exit(m1, trades, CUTOFFS[0][0])
    engine_sum = sum(t.net_pts / abs(t.entry_price - t.stop_price) for t in trades)
    print(f"replay check: engine {engine_sum:+.2f} R vs replay {baseline['r'].sum():+.2f} R")

    print(f"\n{'cutoff (NY)':<13}{'sum R':>9}{'R/trade':>10}{'win%':>8}{'stops':>8}{'maxDD R':>10}  note")
    for cutoff, note in CUTOFFS:
        s = stats(replay_exit(m1, trades, cutoff))
        print(f"{cutoff.strftime('%H:%M'):<13}{s['sum_r']:>+9.2f}{s['mean_r']:>+10.4f}"
              f"{s['win_rate']*100:>7.1f}%{s['stops']:>8}{s['max_dd_r']:>10.2f}  {note}")

    held = baseline[baseline["exit_reason"] == "time"]
    print(f"\nonly {len(held)} of {len(baseline)} trades reach 15:59 open "
          f"({len(held)/len(baseline)*100:.0f}%) -- the rest are stopped out first "
          f"and are unaffected by a later cutoff")


if __name__ == "__main__":
    main()
