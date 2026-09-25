"""S022 -- Asia Mean Reversion, EUR/USD, M5, 22:00-06:00 UTC.

Spec (claude/strategies-registry.md S022; Bot 1 in Anton's original three-bot
brief, claude/prompts-new-strategy-candidates.md P13): VWAP + Z-Score =
(Close-VWAP)/StdDev. BUY: Z<=-2.2 AND RSI(14)<=30. SELL: Z>=+2.2 AND
RSI(14)>=70. Exit: touch of VWAP or SL=1.5xATR(14).

Design choices made to turn this into mechanical, testable rules (documented
here per the project's convention of recording judgment calls, not silently
guessing -- see strategies/orb_intraday/engine.py's seasonal-anchor note and
strategies/eia_calendar.py's holiday-shift table for the pattern this follows):

* VWAP proxy. histdata's OTC FX feed carries no real trade volume (the M1
  files' volume column is always 0 -- verified across the EUR/USD file set),
  so a genuine dollar-VWAP cannot be computed. This engine uses the
  session-anchored cumulative average of typical price (H+L+C)/3 since
  session open as the VWAP proxy -- the closest available stand-in given the
  data, weighted equally per M5 bar rather than by volume. StdDev is the
  sample std (ddof=1) of that same typical-price series since session open.
  Both are running/causal: the value at bar i uses only bars 0..i of the
  current session.
* RSI(14) and ATR(14) are computed CONTINUOUSLY over the whole M5 series
  (utils.indicators.wilder_rsi/wilder_atr), not reset per session -- an
  indicator needing history doesn't stop existing between sessions, and this
  matches how a live bot would run them. The session window only gates which
  bars are eligible to generate an ENTRY signal.
* Entry timing. The Z-Score/RSI condition is evaluated on bar i's fully
  closed OHLC; the trade is entered at bar i+1's OPEN (the earliest a real
  bot could act on a just-closed bar), not at bar i's own close -- avoids an
  idealized same-bar-close fill. SL distance is frozen at entry using ATR(14)
  as of the signal bar i (frozen-at-entry risk unit, same convention as
  strategies/orb_intraday's ADR14-at-entry stop).
* VWAP-touch exit is checked from the entry bar onward: long exits when a
  bar's high reaches that bar's own (still-running) VWAP, short when a bar's
  low reaches it. If a bar's range would touch BOTH the VWAP level and the
  frozen SL, the SL is assumed to have been hit first (conservative,
  worst-case ordering -- consistent with this project's other engines).
* One trade per session at most: the first valid signal bar in the session
  window fires; no new entries in that session afterward, whether the first
  trade is still open or already closed (Asia session mean-reversion is a
  single anchored-VWAP construction per session, not a re-armable signal).
* Forced time exit at the session's last in-window bar if neither VWAP nor
  SL has been hit by then (reason "time", same convention as
  strategies/orb_intraday.engine.simulate's end-of-session close).

Session 22:00-06:00 UTC crosses midnight; a "session" is keyed by its 22:00
UTC open date D and spans [D 22:00, D+1 06:00) UTC.

No look-ahead: entries use only bar i's closed OHLC to decide, and act no
earlier than bar i+1's open; VWAP/RSI/ATR at bar i use only bars <= i; the
Gate 0 tests in tests/strategies/test_asia_mean_reversion.py assert this by
truncating the input and checking already-emitted signals/trades are
unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time

import numpy as np
import pandas as pd

from utils.indicators import wilder_atr, wilder_rsi

SESSION_OPEN = time(22, 0)   # UTC
SESSION_CLOSE = time(6, 0)   # UTC, next calendar day


@dataclass(frozen=True)
class AsiaMrConfig:
    z_entry: float = 2.2                # |Z| threshold to arm a signal
    rsi_oversold: float = 30.0          # BUY requires RSI(14) <= this
    rsi_overbought: float = 70.0        # SELL requires RSI(14) >= this
    stop_atr_mult: float = 1.5          # SL distance = stop_atr_mult * ATR(14), frozen at entry
    rsi_period: int = 14
    atr_period: int = 14
    min_session_bars_for_stats: int = 6  # need >=N session bars of history before Z/VWAP is trusted
    cost_bps_roundtrip: float = 1.0     # round-trip cost, bps of entry price, charged once/trade

    def with_(self, **overrides) -> "AsiaMrConfig":
        return replace(self, **overrides)


ASIA_MR_BASE = AsiaMrConfig()  # frozen baseline -- matches the spec exactly, never edit in place


@dataclass
class Trade:
    session: pd.Timestamp     # session key = the 22:00 UTC open date
    direction: str            # "long" | "short"
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry_price: float
    stop_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str          # "vwap" | "stop" | "time"
    entry_z: float
    entry_rsi: float
    atr_at_entry: float
    gross: float
    net: float
    r_multiple: float


def _session_key(ts: pd.Timestamp) -> pd.Timestamp:
    """The 22:00 UTC session-open date this bar belongs to."""
    d = ts.normalize()
    return d if ts.time() >= SESSION_OPEN else d - pd.Timedelta(days=1)


def add_indicators(m5: pd.DataFrame, cfg: AsiaMrConfig) -> pd.DataFrame:
    """Attach RSI(14), ATR(14) (continuous) and per-session running VWAP/StdDev/Z
    (reset at each session's first bar) to a copy of m5. Purely causal."""
    df = m5.copy()
    df["rsi"] = wilder_rsi(df["close"], cfg.rsi_period)
    df["atr"] = wilder_atr(df["high"], df["low"], df["close"], cfg.atr_period)
    df["session"] = df.index.map(_session_key)
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0

    vwap = np.full(len(df), np.nan)
    stddev = np.full(len(df), np.nan)
    bar_in_session = np.full(len(df), 0, dtype=int)
    for _, idx in df.groupby("session").groups.items():
        pos = df.index.get_indexer(idx)
        typ = df["typical"].to_numpy()[pos]
        running_mean = np.cumsum(typ) / np.arange(1, len(typ) + 1)
        vwap[pos] = running_mean
        for k in range(1, len(typ)):
            stddev[pos[k]] = typ[: k + 1].std(ddof=1)
        bar_in_session[pos] = np.arange(len(typ))
    df["vwap"] = vwap
    df["stddev"] = stddev
    df["bar_in_session"] = bar_in_session
    df["z"] = (df["close"] - df["vwap"]) / df["stddev"].replace(0.0, np.nan)
    return df


def _in_window(ts: pd.Timestamp) -> bool:
    t = ts.time()
    return t >= SESSION_OPEN or t < SESSION_CLOSE


def simulate(m5: pd.DataFrame, cfg: AsiaMrConfig = ASIA_MR_BASE) -> list[Trade]:
    df = add_indicators(m5, cfg)
    trades: list[Trade] = []
    traded_sessions: set[pd.Timestamp] = set()

    n = len(df)
    idx = df.index
    for i in range(n - 1):  # need a bar i+1 to enter on
        row = df.iloc[i]
        ts = idx[i]
        if not _in_window(ts):
            continue
        session = row["session"]
        if session in traded_sessions:
            continue
        if row["bar_in_session"] < cfg.min_session_bars_for_stats:
            continue
        if pd.isna(row["z"]) or pd.isna(row["rsi"]) or pd.isna(row["atr"]) or row["atr"] <= 0:
            continue

        direction = None
        if row["z"] <= -cfg.z_entry and row["rsi"] <= cfg.rsi_oversold:
            direction = "long"
        elif row["z"] >= cfg.z_entry and row["rsi"] >= cfg.rsi_overbought:
            direction = "short"
        if direction is None:
            continue

        entry_row = df.iloc[i + 1]
        entry_time = idx[i + 1]
        if _session_key(entry_time) != session or not _in_window(entry_time):
            continue  # signal fired on the session's last in-window bar -- no room to enter

        entry_price = entry_row["open"]
        atr_at_entry = row["atr"]
        stop_dist = cfg.stop_atr_mult * atr_at_entry
        stop_price = entry_price - stop_dist if direction == "long" else entry_price + stop_dist

        rest = df.loc[(df.index >= entry_time) & (df["session"] == session)]
        exit_time, exit_price, exit_reason = rest.index[-1], rest["close"].iloc[-1], "time"
        for ets, ebar in rest.iterrows():
            stop_hit = (ebar["low"] <= stop_price) if direction == "long" else (ebar["high"] >= stop_price)
            vwap_hit = (ebar["high"] >= ebar["vwap"]) if direction == "long" else (ebar["low"] <= ebar["vwap"])
            if stop_hit:  # conservative ordering: stop assumed to hit before VWAP within the same bar
                exit_time, exit_price, exit_reason = ets, stop_price, "stop"
                break
            if vwap_hit:
                exit_time, exit_price, exit_reason = ets, ebar["vwap"], "vwap"
                break

        gross = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        cost = entry_price * cfg.cost_bps_roundtrip / 10_000.0
        net = gross - cost
        r = net / stop_dist if stop_dist > 0 else np.nan

        trades.append(Trade(session, direction, ts, entry_time, entry_price, stop_price,
                             exit_time, exit_price, exit_reason, row["z"], row["rsi"],
                             atr_at_entry, gross, net, r))
        traded_sessions.add(session)
    return trades


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([t.__dict__ for t in trades])
