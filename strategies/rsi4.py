"""S011 setup 4/6 -- RSI4 "91% win rate" (Larry Connors-style EOD mean-reversion).

⚠️ LOWEST-CONFIDENCE SETUP IN THE S011 PACKAGE. Per ALGODEV-23's own text,
the 91% win-rate claim traces to a video whose website/article (the
methodology source) was unreachable even during the project's own research
pass -- meaning even `claude/research-2026-08-12-connors-strategies.md`
(inaccessible from this session, see the S011 handoff note) most likely
contains a RECONSTRUCTION of the rules, not the primary source. What
follows is this module's own best-effort reconstruction from the common
shape of this genre of short-RSI-period system, NOT a verified rule set:

  1. Trend filter: close > SMA(trend_sma) -- ASSUMED by analogy with the
     rest of the S011 package (Double Seven, RSI(2), ConnorsRSI all use
     it); not confirmed for this specific setup.
  2. Entry: buy at today's close if flat, rule 1 holds, and RSI(4) <
     entry_threshold (30 is the typical value for this genre of video;
     NOT confirmed).
  3. Exit -- TWO plausible reconstructions, implemented as separate named
     presets because the source does not make this unambiguous:
       - `"rsi_exit"`: RSI(4) > exit_threshold (70, symmetric to entry).
       - `"time_stop"`: exit after a fixed `time_stop_days` regardless of
         RSI -- this genre of "quick scalp, high win rate" video system
         commonly uses a short time-based exit instead of an indicator
         exit (a few days, not weeks) precisely BECAUSE a tight time stop
         is what inflates the win rate (most short mean-reversion bounces
         resolve within a few days; a time stop harvests the ones that
         haven't reversed yet as small losses before they become large
         ones, or as breakeven-ish exits, rather than letting them run).
  4. Long-only, one position at a time, no separate stop-loss, 100% equity
     in/out, no pyramiding -- same convention as the rest of S011.

Mandatory per ALGODEV-23: a high published win rate must NOT be taken at
face value (the same lesson as S006's unconfirmed 78% WR) -- worst month
and max drawdown MUST be computed and reported alongside win rate before
any claim about this setup, not just average R/CAGR. See
`backtest/run_rsi4.py`, which computes and prints both unconditionally.

Which numbers are standard vs. tunable: `rsi_period=4` is fixed (it is the
strategy's name). `trend_sma=200` fixed (assumed, see above).
`entry_threshold`/`exit_threshold`/`time_stop_days` are UNVERIFIED
reconstructed guesses, not published-and-confirmed constants like in the
other four setups -- treat any Gate 1/2 result here as provisional pending
a real source, not as validated the way Double Seven/RSI(2)/ConnorsRSI are.

Engine is a pure state machine reading `RSI4Config`; reuses
`strategies.rsi2.wilder_rsi` (single source of truth for RSI, no
duplication). Signal convention matches the rest of S011: returns the
DECIDED position at bar t using only information up to and including
close[t]; the caller shifts by one bar.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from strategies.rsi2 import wilder_rsi


@dataclass(frozen=True)
class RSI4Config:
    """Single source of truth for RSI4's constants -- see module docstring
    for the confidence level of each (all reconstructed, not confirmed).

    rsi_period       -- Wilder RSI lookback. Fixed at 4 (the strategy's name).
    trend_sma        -- SMA length for the long-term uptrend filter. Assumed.
    entry_threshold  -- buy when RSI(rsi_period) < this value.
    exit_mode        -- "rsi_exit" or "time_stop" (see module docstring).
    exit_threshold   -- used when exit_mode == "rsi_exit": sell when
                        RSI(rsi_period) > this value.
    time_stop_days   -- used when exit_mode == "time_stop": sell exactly
                        this many trading days after entry, regardless of RSI.
    """
    rsi_period: int = 4
    trend_sma: int = 200
    entry_threshold: float = 30.0
    exit_mode: str = "rsi_exit"          # "rsi_exit" | "time_stop"
    exit_threshold: float = 70.0
    time_stop_days: int = 3

    def __post_init__(self):
        if self.exit_mode not in ("rsi_exit", "time_stop"):
            raise ValueError(f"exit_mode must be 'rsi_exit' or 'time_stop', got {self.exit_mode!r}")


BASELINE_RSI4 = RSI4Config()  # entry<30, exit RSI(4)>70
RSI4_TIME_STOP = replace(BASELINE_RSI4, exit_mode="time_stop")  # entry<30, exit after 3 days fixed

ALL_RSI4_PRESETS = {
    "baseline": BASELINE_RSI4,
    "time_stop": RSI4_TIME_STOP,
}


def compute_features(df: pd.DataFrame, cfg: RSI4Config = BASELINE_RSI4) -> pd.DataFrame:
    """Trailing-only feature columns used by the entry/exit rules."""
    close = df["close"]
    sma_trend = close.rolling(cfg.trend_sma, min_periods=cfg.trend_sma).mean()
    rsi = wilder_rsi(close, cfg.rsi_period)
    return pd.DataFrame({
        "close": close,
        "trend_ok": close > sma_trend,
        "rsi": rsi,
        "entry_ok": rsi < cfg.entry_threshold,
        "rsi_exit_ok": rsi > cfg.exit_threshold,
    }, index=df.index)


def rsi4_signal(df: pd.DataFrame, cfg: RSI4Config = BASELINE_RSI4) -> pd.Series:
    """Return the DECIDED position (1.0 long / 0.0 flat) for every bar t.

    Same state-machine / same-day-decision convention as the rest of S011
    (`double_seven_signal`, `rsi2_signal`, `connors_rsi_signal`). The
    `"time_stop"` mode additionally tracks days-held so the exit can fire
    on a fixed schedule instead of an indicator condition -- still uses
    only information up to and including bar t (days-held is a pure
    counter of bars since entry, no forward reference).
    """
    feats = compute_features(df, cfg)
    trend_ok = feats["trend_ok"].to_numpy()
    entry_ok = feats["entry_ok"].to_numpy()
    rsi_exit_ok = feats["rsi_exit_ok"].to_numpy()

    position = np.zeros(len(df))
    in_position = False
    days_held = 0
    for i in range(len(df)):
        if in_position:
            days_held += 1
            if cfg.exit_mode == "rsi_exit":
                exit_now = rsi_exit_ok[i]
            else:
                exit_now = days_held >= cfg.time_stop_days
            if exit_now:
                in_position = False
                days_held = 0
        else:
            if trend_ok[i] and entry_ok[i]:
                in_position = True
                days_held = 1
        position[i] = 1.0 if in_position else 0.0
    return pd.Series(position, index=df.index, name="target_position")
