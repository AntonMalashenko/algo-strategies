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

from .config import OrbConfig, REVERSAL_MODES, REV_FADE, REV_SAR, REV_FLIP_OPPOSITE, REV_FLIP_OPEN

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
    stop_price: float          # ORIGINAL stop distance (unchanged even if breakeven moved it)
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str          # "stop" | "time" | "breakeven" (stopped at entry_price after BE moved it)
    adr14: float
    gross_pts: float
    net_pts: float
    be_moved: bool = False    # True if cfg.breakeven_at_r triggered on this trade (always False for ORB_BASE)
    leg: int = 1              # 1 = primary trade; 2 = reversal leg (cfg.reversal_mode; never for ORB_BASE)


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


def _run_reversal_leg(sess: pd.DataFrame, start_ts: pd.Timestamp, direction: str,
                      entry_price: float, stop_dist: float) -> tuple[pd.Timestamp, float, str, float]:
    """Simulate one reversal leg from start_ts (inclusive -- the flip/stop bar itself is checked
    for the new leg's stop, conservative) to session close. Returns (exit_time, exit_price,
    exit_reason, stop_price). Fixed stop, no TP, time exit at the last session bar."""
    stop_price = (entry_price - stop_dist) if direction == "long" else (entry_price + stop_dist)
    rest = sess.loc[sess.index >= start_ts]
    for ts, bar in rest.iterrows():
        if direction == "long" and bar["low"] <= stop_price:
            return ts, stop_price, "stop", stop_price
        if direction == "short" and bar["high"] >= stop_price:
            return ts, stop_price, "stop", stop_price
    return rest.index[-1], rest["close"].iloc[-1], "time", stop_price


def _stop_reason(be_moved: bool, trail_moved: bool) -> str:
    if trail_moved:
        return "trail"
    return "breakeven" if be_moved else "stop"


def _opposite(direction: str) -> str:
    return "short" if direction == "long" else "long"


def simulate(m1: pd.DataFrame, cfg: OrbConfig) -> list[Trade]:
    if cfg.reversal_mode is not None:
        if cfg.reversal_mode not in REVERSAL_MODES:
            raise ValueError(f"unknown reversal_mode {cfg.reversal_mode!r}")
        if cfg.breakeven_at_r is not None:
            raise ValueError("reversal_mode and breakeven_at_r are not combinable")
        if cfg.trail_adr_mult is not None or cfg.stop_on_close or cfg.time_stop_minutes is not None:
            raise ValueError("reversal_mode is not combinable with trail/close/time stop variants")
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
        if cfg.reversal_mode == REV_FADE:
            direction = _opposite(direction)   # trade against the breakout, at the touched band
        stop_dist = cfg.stop_adr_mult * adr
        stop_price = (entry_price - stop_dist) if direction == "long" else (entry_price + stop_dist)

        # Breakeven-stop modifier (default off -- cfg.breakeven_at_r is None for ORB_BASE,
        # so be_trigger stays None and current_stop never leaves stop_price: the loop below
        # is then byte-identical to the pre-modifier code path. See config.py's field
        # comment and backtest/run_s021_breakeven.py's regression check.
        be_trigger = None
        if cfg.breakeven_at_r is not None:
            be_offset = cfg.breakeven_at_r * stop_dist
            be_trigger = (entry_price + be_offset) if direction == "long" else (entry_price - be_offset)
        current_stop = stop_price
        be_moved = False

        # Reversal modifier (default off -- cfg.reversal_mode is None for ORB_BASE, so
        # flip_level stays None and the flip check below never fires).
        rev_cutoff_ts = pd.Timestamp.combine(d.date(), cfg.reversal_cutoff or cfg.entry_cutoff)
        flip_level = None
        if cfg.reversal_mode == REV_FLIP_OPPOSITE:
            flip_level = L if direction == "long" else U
        elif cfg.reversal_mode == REV_FLIP_OPEN:
            flip_level = O

        # Stop-variant modifiers (all default off for ORB_BASE -- see config.py field comments).
        trail_dist = cfg.trail_adr_mult * adr if cfg.trail_adr_mult is not None else None
        trail_moved = False
        best_px = entry_price
        time_stop_ts = (entry_time + pd.Timedelta(minutes=cfg.time_stop_minutes)
                        if cfg.time_stop_minutes is not None else None)
        time_stop_done = False

        rest = sess.loc[sess.index >= entry_time]
        exit_time, exit_price, exit_reason = rest.index[-1], rest["close"].iloc[-1], "time"
        for ts, bar in rest.iterrows():
            if flip_level is not None and ts <= rev_cutoff_ts and (
                    (direction == "long" and bar["low"] <= flip_level)
                    or (direction == "short" and bar["high"] >= flip_level)):
                exit_time, exit_price, exit_reason = ts, flip_level, "flip"
                break
            if direction == "long":
                stop_probe = bar["close"] if cfg.stop_on_close else bar["low"]
                if stop_probe <= current_stop:
                    fill = bar["close"] if cfg.stop_on_close else current_stop
                    exit_time, exit_price, exit_reason = ts, fill, _stop_reason(be_moved, trail_moved)
                    break
                if be_trigger is not None and not be_moved and bar["high"] >= be_trigger:
                    current_stop, be_moved = entry_price, True
            else:
                stop_probe = bar["close"] if cfg.stop_on_close else bar["high"]
                if stop_probe >= current_stop:
                    fill = bar["close"] if cfg.stop_on_close else current_stop
                    exit_time, exit_price, exit_reason = ts, fill, _stop_reason(be_moved, trail_moved)
                    break
                if be_trigger is not None and not be_moved and bar["low"] <= be_trigger:
                    current_stop, be_moved = entry_price, True
            # Time-stop modifier (default off): one check, on the first bar at/after
            # entry + time_stop_minutes -- exit at that bar's close if the trade is not in profit.
            if time_stop_ts is not None and not time_stop_done and ts >= time_stop_ts:
                time_stop_done = True
                in_profit = bar["close"] > entry_price if direction == "long" else bar["close"] < entry_price
                if not in_profit:
                    exit_time, exit_price, exit_reason = ts, bar["close"], "time_stop"
                    break
            # Trailing-stop modifier (default off): ratchet AFTER this bar's stop check, so a
            # new level only applies from the next bar (no same-bar high-then-low optimism).
            if trail_dist is not None:
                if direction == "long":
                    best_px = max(best_px, bar["high"])
                    if best_px - trail_dist > current_stop:
                        current_stop, trail_moved = best_px - trail_dist, True
                else:
                    best_px = min(best_px, bar["low"])
                    if best_px + trail_dist < current_stop:
                        current_stop, trail_moved = best_px + trail_dist, True

        gross = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        cost = entry_price * cfg.cost_bps_roundtrip / 10_000.0
        trades.append(Trade(d, direction, entry_time, entry_price, stop_price,
                             exit_time, exit_price, exit_reason, adr, gross, gross - cost, be_moved))

        rev_start = None
        if exit_reason == "flip":
            rev_start = exit_time
        elif cfg.reversal_mode == REV_SAR and exit_reason == "stop" and exit_time <= rev_cutoff_ts:
            rev_start = exit_time
        if rev_start is not None:
            r_dir = _opposite(direction)
            r_entry = exit_price
            r_exit_time, r_exit_price, r_reason, r_stop = _run_reversal_leg(
                sess, rev_start, r_dir, r_entry, stop_dist)
            r_gross = (r_exit_price - r_entry) if r_dir == "long" else (r_entry - r_exit_price)
            r_cost = r_entry * cfg.cost_bps_roundtrip / 10_000.0
            trades.append(Trade(d, r_dir, rev_start, r_entry, r_stop, r_exit_time, r_exit_price,
                                 r_reason, adr, r_gross, r_gross - r_cost, False, leg=2))
    return trades


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame([t.__dict__ for t in trades])


# ============================================================================
# S021.1 -- NAS100 Opening-Range Squeeze (default-off preset, see OrbConfig's
# squeeze_preset_enabled / NAS_SQUEEZE_PRESET in config.py for the field set).
# Everything below this line is new; nothing above it was edited to add this
# preset -- see simulate_squeeze_preset()'s own docstring for why this is a
# sibling function rather than a branch inside simulate() above.
# ============================================================================

import numpy as np

from utils.histdata import load_histdata_m1_utc, resample_ohlc
from utils.indicators import wilder_atr

NSXUSD_SYMBOL = "NSXUSD"  # histdata symbol code, same instrument as the base ORB_BASE path


def load_nsxusd_m1_true_utc(data_dir: Path) -> pd.DataFrame:
    """S021.1-only loader: NSXUSD M1 in TRUE UTC (via utils.histdata.load_histdata_m1_utc),
    NOT the fixed-EST clock load_nsxusd_m1() above uses for the base ORB_BASE path. S021.1's
    spec times (13:30/13:45/16:00/20:00) are given in true UTC, so this preset needs the
    UTC-converted loader -- using the fixed-EST loader here would silently shift every
    session boundary by up to an hour in EDT months, exactly the seasonal ambiguity the base
    engine's module docstring above documents (and deliberately keeps, for ORB_BASE only)."""
    return load_histdata_m1_utc(NSXUSD_SYMBOL, data_dir)


def ema(close: pd.Series, period: int) -> pd.Series:
    """Standard recursive EMA (alpha = 2/(period+1)), causal. First period-1 values are
    NaN (warm-up). Same construction as strategies/gold_session_momentum.py's /
    strategies/gold_chandelier_breakout.py's ema() -- re-declared locally rather than
    imported cross-strategy, per this project's module-boundary convention."""
    return close.ewm(span=period, adjust=False, min_periods=period).mean()


def compute_daily_bars_utc(m1_utc: pd.DataFrame) -> pd.DataFrame:
    """Calendar-day OHLC bars in true UTC, used only to derive the squeeze-ATR14 and
    trend-EMA50 filters for the S021.1 preset (simulate_squeeze_preset below). NOT used by
    the base ADR-band simulate()/ORB_BASE path, which stays on the fixed-EST clock and its
    own intraday-session ADR14 (compute_adr14 above)."""
    daily = resample_ohlc(m1_utc, "1D")
    return daily.dropna(subset=["open", "high", "low", "close"])


def compute_squeeze_filters(daily: pd.DataFrame, cfg: OrbConfig) -> pd.DataFrame:
    """Daily ATR14 and Daily EMA(trend_ema_period) filter values for the squeeze preset,
    each SHIFTED BY ONE DAY so that trading day D only ever sees values computed from days
    strictly BEFORE D.

    Causal-shift judgment call (not specified in the P14 prompt, decided here): D's own
    daily bar is still open at every intraday decision time for this preset (the session
    starts at 13:30 UTC, hours before that calendar day's own daily bar closes at midnight
    UTC), so a filter reading D's own developing daily range/close would be looking into
    the future. This mirrors the base ORB_BASE path's own causal ADR14 (compute_adr14
    above, which likewise only ever uses trading days strictly before the current one)."""
    atr14 = wilder_atr(daily["high"], daily["low"], daily["close"], period=cfg.squeeze_atr_period)
    ema_trend = ema(daily["close"], cfg.trend_ema_period)
    out = pd.DataFrame({
        "prior_atr14": atr14.shift(1),
        "prior_ema": ema_trend.shift(1),
        "prior_close": daily["close"].shift(1),
    }, index=daily.index)
    out.index.name = "day"
    return out


def compute_orb_open_range(m15_utc: pd.DataFrame, cfg: OrbConfig) -> pd.DataFrame:
    """One row per UTC calendar day with the S021.1 opening-range window: the single M15
    candle in [orb_open_start, orb_open_end) -- the literal FIRST M15 candle of the cash
    session, not a rolling/ADR-derived band like the base ORB_BASE path. A day missing that
    candle (holiday, data gap) is simply absent -- no signal can fire for it."""
    if len(m15_utc) == 0:
        return pd.DataFrame(columns=["orb_high", "orb_low"])
    t = m15_utc.index.time
    in_range = (t >= cfg.orb_open_start) & (t < cfg.orb_open_end)
    sub = m15_utc.loc[in_range]
    if sub.empty:
        return pd.DataFrame(columns=["orb_high", "orb_low"])
    day = sub.index.normalize()
    out = pd.DataFrame({
        "orb_high": sub["high"].groupby(day).max(),
        "orb_low": sub["low"].groupby(day).min(),
    })
    out.index.name = "day"
    return out


@dataclass
class SqueezeTrade:
    day: pd.Timestamp
    direction: str            # "long" | "short"
    entry_time: pd.Timestamp
    entry_price: float
    stop_price: float
    tp_price: float
    exit_time: pd.Timestamp
    exit_price: float
    exit_reason: str          # "stop" | "tp" | "time"
    orb_high: float
    orb_low: float
    orb_width: float
    prior_atr14: float
    prior_close: float
    prior_ema: float
    stop_dist: float
    gross_pts: float
    net_pts: float
    r_multiple: float


def simulate_squeeze_preset(m1_utc: pd.DataFrame, cfg: OrbConfig) -> list[SqueezeTrade]:
    """S021.1 -- NAS100 Opening-Range Squeeze, a default-off PRESET tracked on the S021
    engine (claude/prompts-new-strategy-candidates.md P14, "S021.1"; own reformalization of
    an idea from an external review (Google Gemini) -- see the prompt's preamble for what
    was and wasn't taken from that review). This is a SIBLING function, not a branch inside
    simulate() above: the entry mechanics (a resting stop order + a fixed TP, vs simulate()'s
    touch-detection with no TP), the range definition (the literal first M15 candle of the
    cash session, vs simulate()'s O +/- k*ADR14 band), the clock convention (true UTC here,
    vs simulate()'s fixed-EST clock), and the added squeeze/trend filters are all different
    enough from the base engine that folding them into simulate() as a branch would make
    that function's control flow read like two unrelated strategies glued together. Sharing
    the SAME OrbConfig dataclass (new fields, additive, default-off, unused by ORB_BASE) is
    what keeps this a preset in the strategy-modifiers sense rather than a fork of the
    engine: the base ORB_BASE/simulate() path is untouched, byte for byte, by this addition
    -- see backtest/run_s021_squeeze_preset.py's regression check reproducing the frozen
    baseline (n=1518, sum_net_pts=16220.614) after this function was added to the file.

    Rules: NAS100 (NSXUSD), M15, true UTC (load_nsxusd_m1_true_utc above, NOT
    load_nsxusd_m1's fixed-EST clock). ORB_High/ORB_Low = the single M15 candle in
    [orb_open_start, orb_open_end) -- the first M15 candle of the cash session (13:30-13:45
    UTC baseline). Squeeze filter: (ORB_High - ORB_Low) < squeeze_atr_mult * Daily_ATR14,
    Daily ATR14 read from the PRIOR completed daily bar only (see compute_squeeze_filters).
    Trend filter: Daily EMA(trend_ema_period) -- long only if the PRIOR daily close > prior
    EMA, short only if prior daily close < prior EMA (a day where they're exactly equal, a
    degenerate case not addressed by the prompt, allows neither side). Entry: a resting stop
    order at ORB_High + orb_entry_buffer_pts (long) or ORB_Low - orb_entry_buffer_pts
    (short) -- only the ONE direction the trend filter allows is armed each day, never both
    sides straddled -- active during [orb_entry_start, orb_entry_end), idealized fill
    exactly at the stop price on first touch (same no-slippage convention as simulate()'s
    own breakout fill above). SL = the OPPOSITE ORB boundary, UNCAPPED -- the prompt
    explicitly calls out S023's ATR-capped stop as a mistake not to repeat here, so no ATR
    cap is applied even though ATR14 is already being computed for the squeeze filter. TP =
    orb_tp_r_mult * stop_dist (ORB_BASE has no TP at all; this preset adds one). Forced
    close at orb_force_close if neither SL nor TP has been hit by then, reason "time" -- same
    convention as simulate()'s forced-close rule. Same-bar SL/TP tie-break: SL assumed hit
    first (conservative, project-wide convention -- see e.g.
    strategies/asia_sweep_reversion.py, strategies/gold_session_momentum.py). Max one
    trade/day (the day is consumed once its opening range/filters are evaluated, whether or
    not the stop order ever triggers).

    No look-ahead: the opening range for day D uses only D's own
    [orb_open_start, orb_open_end) bars; the squeeze/trend filters use only daily bars
    STRICTLY before D (compute_squeeze_filters' one-day shift); the stop order is armed no
    earlier than orb_entry_start and evaluated bar-by-bar forward from there in strict time
    order; SL/TP are frozen at entry and checked in strict time order, SL-first on a tie.
    The Gate 0 tests in tests/strategies/test_orb_squeeze_preset.py assert this by
    truncating the input and checking already-emitted trades are unchanged -- for the NEW
    elements introduced here only; the base engine's own no-look-ahead proof
    (compute_adr14/simulate above) is unaffected by this addition and is not re-proven here.
    """
    if len(m1_utc) == 0:
        return []
    m15 = resample_ohlc(m1_utc, "15min")
    daily = compute_daily_bars_utc(m1_utc)
    filt = compute_squeeze_filters(daily, cfg)
    orb = compute_orb_open_range(m15, cfg)

    idx = m15.index
    t = idx.time
    days = idx.normalize()
    high = m15["high"].to_numpy(dtype=float)
    low = m15["low"].to_numpy(dtype=float)
    close = m15["close"].to_numpy(dtype=float)

    trades: list[SqueezeTrade] = []

    for d in orb.index:
        if d not in filt.index:
            continue
        orb_hi, orb_lo = orb.at[d, "orb_high"], orb.at[d, "orb_low"]
        if pd.isna(orb_hi) or pd.isna(orb_lo):
            continue
        atr14 = filt.at[d, "prior_atr14"]
        prior_ema = filt.at[d, "prior_ema"]
        prior_close = filt.at[d, "prior_close"]
        if pd.isna(atr14) or pd.isna(prior_ema) or pd.isna(prior_close):
            continue  # warm-up / data-gap day -- not tradable

        orb_width = orb_hi - orb_lo
        if not (orb_width < cfg.squeeze_atr_mult * atr14):
            continue  # squeeze filter: range too wide -- no trade today

        if prior_close > prior_ema:
            direction = "long"
        elif prior_close < prior_ema:
            direction = "short"
        else:
            continue  # exactly equal -- degenerate, no side allowed

        entry_price = (orb_hi + cfg.orb_entry_buffer_pts) if direction == "long" \
            else (orb_lo - cfg.orb_entry_buffer_pts)
        stop_price = orb_lo if direction == "long" else orb_hi
        stop_dist = (entry_price - stop_price) if direction == "long" else (stop_price - entry_price)
        if stop_dist <= 0:
            continue  # degenerate ORB (buffer inverted the risk unit) -- skip, no valid risk unit

        tp_price = entry_price + cfg.orb_tp_r_mult * stop_dist if direction == "long" \
            else entry_price - cfg.orb_tp_r_mult * stop_dist

        window_mask = (days == d) & (t >= cfg.orb_entry_start) & (t < cfg.orb_entry_end)
        window_pos = np.nonzero(window_mask)[0]
        if len(window_pos) == 0:
            continue

        entry_pos = None
        for j in window_pos:
            touched = (high[j] >= entry_price) if direction == "long" else (low[j] <= entry_price)
            if touched:
                entry_pos = j
                break
        if entry_pos is None:
            continue  # stop order never triggered in the window -- no trade today

        entry_time = idx[entry_pos]
        rest_mask = (idx >= entry_time) & (days == d) & (t < cfg.orb_force_close)
        rest_pos = np.nonzero(rest_mask)[0]
        if len(rest_pos) == 0:
            continue

        exit_pos = rest_pos[-1]
        exit_time, exit_price, exit_reason = idx[exit_pos], close[exit_pos], "time"
        for j in rest_pos:
            if direction == "long":
                stop_hit, tp_hit = low[j] <= stop_price, high[j] >= tp_price
            else:
                stop_hit, tp_hit = high[j] >= stop_price, low[j] <= tp_price
            if stop_hit:  # conservative ordering: stop assumed hit before TP within the same bar
                exit_time, exit_price, exit_reason = idx[j], stop_price, "stop"
                break
            if tp_hit:
                exit_time, exit_price, exit_reason = idx[j], tp_price, "tp"
                break

        gross = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        cost = entry_price * cfg.cost_bps_roundtrip / 10_000.0
        net = gross - cost
        r = net / stop_dist if stop_dist > 0 else np.nan

        trades.append(SqueezeTrade(d, direction, entry_time, entry_price, stop_price, tp_price,
                                    exit_time, exit_price, exit_reason, orb_hi, orb_lo, orb_width,
                                    atr14, prior_close, prior_ema, stop_dist, gross, net, r))
    return trades


def squeeze_trades_to_frame(trades: list[SqueezeTrade]) -> pd.DataFrame:
    return pd.DataFrame([t.__dict__ for t in trades])
