"""S021 engine -- pure state machine over OrbConfig, no strategy math outside it.

No look-ahead: ADR14 and the data-gap guard use only sessions strictly before the
current trading day; within a day, the entry scan and the stop check both walk
forward bar by bar and never consult a bar later than the one being evaluated.

Session-anchor investigation (2026-09-21, Cowork independent verification): the
passport originally specified the session as "09:30-15:59 America/New_York, with
DST" -- i.e. genuine DST-aware NY wall-clock time. An independent backtest built
strictly to that literal spec (see load_nsxusd_m1_true_dst_local below) reproduces
a real but materially weaker edge than the passport's claimed sec 4 numbers: 1712
trades, +8.71 pt/trade, only 5/8 positive years, maxDD -2351.8 (claimed: 1518
trades, +11.06 pt/trade, 8/8 positive years, maxDD -2659).

Root cause, confirmed by splitting both runs' trades on whether the NY calendar
date falls under EST or EDT: histdata's DAT_ASCII_NSXUSD_M1_*.csv is timestamped
in a FIXED UTC-5 (EST) clock year-round (matching the repo's own established
convention for this index source -- scripts/convert_histdata_indices.py never
applies DST). In EST months (~Nov-Mar) that fixed clock reading equals true NY
local time, so both runs agree exactly (603 trades, +12.75 pt/trade, both). In EDT
months (~Mar-Nov) it does not: true NY local = fixed-clock reading + 1h. Anchoring
the opening range at fixed-clock "09:30" therefore anchors it at the TRUE local
09:30 open in winter and at the TRUE local 10:30 (one hour into the session) in
summer -- a seasonal-conditional anchor, not a DST bug. Anton confirmed (chat,
2026-09-21) this seasonal switch is the intended design going forward, not an
artifact to fix: use the fixed-EST-clock reading of 09:30 as the anchor year-round
(equivalently: true NY open in EST months, true NY 10:30 in EDT months), rather
than a uniformly DST-aware 09:30 anchor. This is also simpler to run live -- the
bot never needs America/New_York DST-conversion logic, only the fixed-EST clock
histdata (and cTrader, which reports in a fixed server-side offset) already gives.

load_nsxusd_m1 (the canonical loader used by simulate()) therefore does NOT
DST-convert -- this is deliberate, not an oversight; see the note above. The
DST-aware loader is kept as load_nsxusd_m1_true_dst_local purely as the
diagnostic/reference implementation used to find and confirm this, and for any
future re-audit of the same question -- it is not used by the frozen strategy.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .config import OrbConfig

# histdata.com's DAT_ASCII_NSXUSD_M1_*.csv files are timestamped in a fixed
# UTC-5 (EST) clock year-round, never DST-adjusted (matching
# scripts/convert_histdata_indices.py's established convention for this index
# source). That fixed-clock reading is exactly what S021's frozen session
# anchor is defined against -- see the module docstring above for why this is
# the deliberate, canonical rule and not a bug.
HISTDATA_FIXED_OFFSET = "Etc/GMT+5"
SESSION_TZ = "America/New_York"


def load_nsxusd_m1(data_dir: Path) -> pd.DataFrame:
    """Load+concat DAT_ASCII_NSXUSD_M1_*.csv, indexed on histdata's native fixed-EST
    clock (NOT DST-converted). This is the canonical loader for S021: the frozen
    session anchor (09:30 fixed-EST clock) is true NY local time in EST months and
    true NY 10:30 in EDT months, by design -- see the module docstring.
    """
    files = sorted(Path(data_dir).glob("DAT_ASCII_NSXUSD_M1_*.csv"))
    if not files:
        raise FileNotFoundError(f"No DAT_ASCII_NSXUSD_M1_*.csv under {data_dir}")
    frames = []
    for p in files:
        df = pd.read_csv(p, sep=";", header=None,
                          names=["dt", "open", "high", "low", "close", "vol"])
        df["dt"] = pd.to_datetime(df["dt"], format="%Y%m%d %H%M%S")
        frames.append(df)
    m1 = pd.concat(frames).drop_duplicates(subset="dt").set_index("dt").sort_index()
    return m1[["open", "high", "low", "close"]].sort_index()


def load_nsxusd_m1_true_dst_local(data_dir: Path) -> pd.DataFrame:
    """Diagnostic/reference loader ONLY -- indexed in genuine DST-aware America/New_York
    local time. NOT used by simulate()/the frozen S021 rules. Kept so the seasonal-anchor
    finding in the module docstring can be re-audited later without redoing the timezone
    plumbing from scratch.
    """
    files = sorted(Path(data_dir).glob("DAT_ASCII_NSXUSD_M1_*.csv"))
    if not files:
        raise FileNotFoundError(f"No DAT_ASCII_NSXUSD_M1_*.csv under {data_dir}")
    frames = []
    for p in files:
        df = pd.read_csv(p, sep=";", header=None,
                          names=["dt", "open", "high", "low", "close", "vol"])
        df["dt"] = pd.to_datetime(df["dt"], format="%Y%m%d %H%M%S")
        frames.append(df)
    m1 = pd.concat(frames).drop_duplicates(subset="dt").set_index("dt").sort_index()
    idx = m1.index.tz_localize(HISTDATA_FIXED_OFFSET).tz_convert(SESSION_TZ)
    m1.index = idx.tz_localize(None)
    return m1[["open", "high", "low", "close"]].sort_index()


@dataclass
class Trade:
    day: pd.Timestamp
    direction: str            # "long" | "short"
    entry_time: pd.Timestamp
    entry_price: float
    stop_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str          # "stop" | "time"
    adr14: float
    gross_pts: float
    net_pts: float


def _day_session(day_bars: pd.DataFrame, d: pd.Timestamp, cfg: OrbConfig) -> pd.DataFrame | None:
    lo = pd.Timestamp.combine(d.date(), cfg.session_open)
    hi = pd.Timestamp.combine(d.date(), cfg.session_close)
    sess = day_bars.loc[(day_bars.index >= lo) & (day_bars.index <= hi)]
    if sess.empty or sess.index[0] != lo:
        return None
    if len(sess) < cfg.min_session_bars:
        return None
    return sess


def compute_daily_sessions(m1: pd.DataFrame, cfg: OrbConfig) -> pd.DataFrame:
    """One row per valid trading day (has a 09:30 bar, >= min_session_bars in-session)."""
    rows = []
    dates = m1.index.normalize()
    for d, day_bars in m1.groupby(dates):
        sess = _day_session(day_bars, d, cfg)
        if sess is None:
            continue
        rows.append({
            "date": d,
            "session_open": sess["open"].iloc[0],
            "session_high": sess["high"].max(),
            "session_low": sess["low"].min(),
            "session_range": sess["high"].max() - sess["low"].min(),
            "n_bars": len(sess),
        })
    return pd.DataFrame(rows).set_index("date").sort_index()


def compute_adr14(daily: pd.DataFrame, cfg: OrbConfig) -> pd.Series:
    """Causal ADR14 with a data-gap guard on the lookback window (NaN = day not tradable)."""
    dates = daily.index
    ranges = daily["session_range"].to_numpy()
    out = pd.Series(index=dates, dtype=float)
    for i in range(len(dates)):
        if i < cfg.adr_window:
            continue
        window = ranges[i - cfg.adr_window:i]
        span_days = (dates[i - 1] - dates[i - cfg.adr_window]).days
        if span_days > cfg.max_gap_days_per_session * cfg.adr_window:
            continue
        out.iloc[i] = window.mean()
    return out


def simulate(m1: pd.DataFrame, cfg: OrbConfig) -> list[Trade]:
    daily = compute_daily_sessions(m1, cfg)
    adr14 = compute_adr14(daily, cfg)
    dates_norm = m1.index.normalize()
    trades: list[Trade] = []

    for d, day_bars in m1.groupby(dates_norm):
        if d not in daily.index:
            continue
        adr = adr14.loc[d]
        if pd.isna(adr):
            continue
        sess = _day_session(day_bars, d, cfg)
        if sess is None:
            continue

        O = daily.loc[d, "session_open"]
        U = O + cfg.k_range * adr
        L = O - cfg.k_range * adr
        entry_hi = pd.Timestamp.combine(d.date(), cfg.entry_cutoff)
        scan = sess.loc[sess.index <= entry_hi]

        direction = None
        entry_time = None
        for ts, bar in scan.iterrows():
            hit_u = bar["high"] >= U
            hit_l = bar["low"] <= L
            if hit_u and hit_l:
                direction = "skip"
                break
            if hit_u:
                direction, entry_time = "long", ts
                break
            if hit_l:
                direction, entry_time = "short", ts
                break
        if direction is None or direction == "skip":
            continue

        entry_price = U if direction == "long" else L
        stop_dist = cfg.stop_adr_mult * adr
        stop_price = (entry_price - stop_dist) if direction == "long" else (entry_price + stop_dist)

        rest = sess.loc[sess.index >= entry_time]
        exit_time, exit_price, exit_reason = rest.index[-1], rest["close"].iloc[-1], "time"
        for ts, bar in rest.iterrows():
            if direction == "long" and bar["low"] <= stop_price:
                exit_time, exit_price, exit_reason = ts, stop_price, "stop"
                break
            if direction == "short" and bar["high"] >= stop_price:
                exit_time, exit_price, exit_reason = ts, stop_price, "stop"
                break

        gross = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        cost = entry_price * cfg.cost_bps_roundtrip / 10_000.0
        trades.append(Trade(d, direction, entry_time, entry_price, stop_price,
                             exit_time, exit_price, exit_reason, adr, gross, gross - cost))
    return trades


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([t.__dict__ for t in trades])
