"""S019 -- gap-fade strategy, FROZEN rules (variant A: paper-trade as-is).

S-number assigned 2026-09-01 (was an unnumbered candidate); paper trading
not started yet as of that date (tracking: ALGODEV-35).

Research trail: docs/HANDOFF_GAP_FADE_RESEARCH.md (experiments E1-E5) and
passport docs/STRATEGY_GAP_FADE.md. Parameters below were frozen on
2026-08-30 and must NOT be tuned while paper trading is running.

Rule (all times US/Eastern, regular trading hours):
  Universe : 64 liquid US large caps (scripts/fetch_stock_universe.py).
  Setup    : today's official open is 2-5% BELOW yesterday's close,
             AND no earnings announcement today or yesterday,
             AND SPY 20-day realized vol (through yesterday) < 25% annualized,
             AND trailing 20-day median dollar volume >= $50M.
  Selection: if more than MAX_TRADES_PER_DAY setups, take the LARGEST |gap|.
  Entry    : long at the first bar at/after 09:45 (open + 15 min).
  Stop     : -1% from entry price, intrabar (fill at stop, worse if gapped).
  Exit     : 12:00 bar close, if not stopped.
  Costs    : assume 6 bps round-trip in evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class GapFadeConfig:
    gap_min_pct: float = 2.0          # |gap| lower bound (down gaps only)
    gap_max_pct: float = 5.0          # |gap| upper bound
    calm_vol_max_pct: float = 25.0    # SPY 20d realized vol, annualized
    min_dollar_vol: float = 50e6      # trailing 20d median dollar volume
    entry_delay_min: int = 15         # minutes after the 09:30 open
    exit_time: str = "12:00"          # ET
    stop_pct: float = 1.0             # stop distance below entry
    max_trades_per_day: int = 4
    cost_bps: float = 6.0             # round-trip, for evaluation only


FROZEN_GAP_FADE = GapFadeConfig()


def replay_gap_fade_trade(day_bars: pd.DataFrame,
                          cfg: GapFadeConfig = FROZEN_GAP_FADE) -> dict | None:
    """Replay the frozen entry/stop/exit on one day's 1-minute RTH bars.

    Returns a dict with entry/exit prices, timestamps, exit reason and gross
    return, or None if the session lacks usable bars.
    """
    if len(day_bars) < 100:  # half-day or broken session
        return None
    session_open = day_bars.index[0]
    entry_ts = session_open + pd.Timedelta(minutes=cfg.entry_delay_min)
    window = day_bars.loc[day_bars.index >= entry_ts]
    if window.empty:
        return None
    entry_price = float(window.iloc[0]["open"])
    entry_time = window.index[0]

    exit_ts = session_open.normalize() + pd.Timedelta(f"{cfg.exit_time}:00")
    window = window.loc[window.index <= exit_ts]
    if window.empty:
        return None

    stop_price = entry_price * (1 - cfg.stop_pct / 100.0)
    hit = window["low"] <= stop_price
    if hit.any():
        first = window.loc[hit].iloc[0]
        fill = min(float(first["open"]), stop_price)  # gap-through fills worse
        exit_time, exit_price, reason = hit.idxmax(), fill, "stop"
    else:
        exit_time, exit_price, reason = window.index[-1], float(window.iloc[-1]["close"]), "time"

    return {
        "entry_time": entry_time, "entry_price": entry_price,
        "exit_time": exit_time, "exit_price": exit_price, "exit_reason": reason,
        "gross_ret": exit_price / entry_price - 1.0,
    }

