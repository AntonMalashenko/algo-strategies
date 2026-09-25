"""S020 variant (a) -- classic swing retracement. Engine.

Two-timeframe state machine, fully causal:

  higher_tf (default H4): the non-repainting ZigZag (swing.py) finds
  confirmed impulse legs; a new zone (zones.py) is (re)built the instant a
  fresh pivot confirms, gated by the higher_tf EMA trend filter. A zone
  becomes usable starting the FIRST entry_tf bar strictly after the
  higher_tf bar that confirmed it -- never the same bar, so the zone is
  never used before it is actually knowable.

  entry_tf (default H1): on each closed bar, if flat and an active zone
  exists, a touch of the zone's "near" edge fills a limit entry there (next
  bar's open). Once filled, SL/TP are managed exactly like
  s017_elliott.run_backtest (same-bar double-touch resolves to SL,
  conservative). A zone is discarded once an entry_tf bar closes beyond its
  stop level without having filled, or once a newer higher_tf pivot
  confirms and replaces it (the fresher leg wins; any still-pending limit
  from the stale zone is cancelled, not carried over).

TREND FILTER NOTE (deviation from strategy-passport-S020.md 4.1): the
passport suggests a D1 EMA200 filter with the impulse detected on H4. This
first implementation runs the EMA200 filter on higher_tf (H4) closes
directly instead, avoiding a second daily-bar alignment step -- a second
place a cross-timeframe look-ahead bug could hide -- in this first cut.
Comparing a true D1-filter variant is a natural Gate 1 walk-forward axis,
not implemented here.

OPTIONAL TREND MODE -- "structure" (Anton, 2026-09-19, "а если тренд по 4Н
не по ема а по структуре?"): trend_filter_mode="structure" replaces the
EMA200 read with a pure price-structure check on the SAME higher_tf ZigZag
pivots already computed for the impulse/zone (swing.py -- no new detector,
per code-architecture "reuse before you add"). Long is allowed only when
the fresh pivot B (a HIGH) is higher than the prior HIGH two pivots back,
AND the LOW in between (A) is higher than the LOW before that (classic
higher-high + higher-low). Short is the mirror (lower-low + lower-high).
Needs at least 4 confirmed pivots of history to evaluate; with fewer, it
fails closed (no trade) rather than passing open -- see
_structure_trend_ok. Off by default (mode stays "ema200"); see
STRUCTURE_TREND_SWEEP_SWING_S020 in config.py.

R accounting matches project convention: net_r = gross_r - spread_pts/risk,
one position at a time, SL priority on a same-bar double touch.

OPTIONAL MODIFIER -- fvg_confluence (config.require_fvg_confluence, off by
default, see fvg.py): when on, a zone touch only arms a pending entry if
an active entry_tf fair-value gap (matching direction) overlaps the
retracement zone at that instant. Base behaviour (flag off) is bit-for-bit
unchanged -- the confluence check short-circuits to True.

OPTIONAL SUB-MODIFIERS -- fvg_require_unmitigated / fvg_sweep_mode (off by
default, see fvg.py / sweep.py): only read when require_fvg_confluence is
also True. fvg_require_unmitigated additionally rejects an already fully-
filled ("empty") gap. fvg_sweep_mode additionally requires the gap to have
formed off a liquidity sweep of a prior entry_tf swing and/or inducement
level (see sweep.py); an FVG with no such anchor ("в воздухе") is treated
as no FVG at all -- the touch is skipped. Both default off, so
FVG_CONFLUENCE_S020 (E2) stays bit-for-bit reproducible.
"""
from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

from .config import FiboRetracementConfig
from .fvg import fvg_formed_at, fvg_levels, fvg_overlaps_zone
from .swing import Pivot, get_swings
from .sweep import fractal_pivots, swept_level
from .zones import LONG, SHORT, Zone, build_zone


def resample_ohlc(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    agg = dict(open="first", high="max", low="min", close="last")
    return df.resample(tf).agg(agg).dropna(subset=["close"])


def _zones_by_confirm(pivots: List[Pivot], cfg: FiboRetracementConfig) -> Dict[int, Zone]:
    """One zone candidate per higher_tf bar index where a pivot confirms."""
    out: Dict[int, Zone] = {}
    for k in range(1, len(pivots)):
        b = pivots[k]
        zone = build_zone(pivots[: k + 1], cfg.entry_level_min, cfg.entry_level_max,
                          cfg.stop_level)
        if zone is not None:
            out[b.confirm_idx] = zone
    return out


def _trend_ok(side: int, close: float, ema: float, mode: str) -> bool:
    if mode == "none":
        return True
    return close > ema if side == LONG else close < ema


def _structure_trend_ok(pivots: List[Pivot], k: int, side: int) -> bool:
    """Pure price-structure trend check on the higher_tf ZigZag pivots
    (reuses the same confirmed, non-repainting pivots the impulse/zone is
    built from -- pivots[k] is B, the fresh pivot that just confirmed).
    Long needs higher-high (B > prior same-kind pivot) + higher-low (A >
    prior same-kind pivot); short is the mirror. Fails closed (False) when
    fewer than 4 confirmed pivots exist yet -- not enough structure to
    judge, so no trade rather than an unfiltered one."""
    if k < 3:
        return False
    p0, p1, p2, p3 = pivots[k - 3], pivots[k - 2], pivots[k - 1], pivots[k]
    if side == LONG:
        return p3.price > p1.price and p2.price > p0.price
    return p3.price < p1.price and p2.price < p0.price


def _fvg_confluence_ok(cfg: FiboRetracementConfig, fvg_lv, idx: int, zone: Zone,
                       entry_highs, entry_lows,
                       sweep_pivots_swing: List[Pivot],
                       sweep_pivots_induce: List[Pivot]) -> bool:
    """Full fvg_confluence gate: overlap + optional unmitigated + optional
    sweep precondition. Short-circuits to True the instant the flag is off,
    so base/E2 behaviour is untouched."""
    if not cfg.require_fvg_confluence:
        return True
    if not fvg_overlaps_zone(fvg_lv, idx, zone.side, zone.near, zone.far,
                             require_unmitigated=cfg.fvg_require_unmitigated):
        return False
    if cfg.fvg_sweep_mode is None:
        return True
    formed_at = fvg_formed_at(fvg_lv, idx, zone.side,
                              require_unmitigated=cfg.fvg_require_unmitigated)
    if cfg.fvg_sweep_mode in ("swing", "either"):
        if swept_level(entry_highs, entry_lows, sweep_pivots_swing, zone.side,
                       formed_at, cfg.fvg_sweep_lookback_bars, cfg.fvg_sweep_max_age_bars):
            return True
    if cfg.fvg_sweep_mode in ("inducement", "either"):
        if swept_level(entry_highs, entry_lows, sweep_pivots_induce, zone.side,
                       formed_at, cfg.fvg_sweep_lookback_bars, cfg.fvg_sweep_max_age_bars):
            return True
    return False


def _session_ok(cfg: FiboRetracementConfig, ts: pd.Timestamp) -> bool:
    """True if `ts` (the bar the fill would execute at) is inside an allowed
    session window. cfg.session_windows is None -> no restriction (default,
    base/all prior presets untouched). Hour is read straight off the
    timestamp -- same convention strategies/fvg_mtf.py::session_of already
    uses for this exact data pipeline (broker/local hour, no separate tz
    conversion layer exists here, documented there as "typically
    GMT+2/+3" -- treated as Anton's Kyiv-hour request without adding a new
    conversion step). cfg.session_skip_window, when set, is checked FIRST
    and always excludes that range even if it overlaps an allowed window --
    models the session-boundary buffer from Anton's request."""
    if cfg.session_windows is None:
        return True
    hour = ts.hour
    if cfg.session_skip_window is not None:
        skip_s, skip_e = cfg.session_skip_window
        if skip_s <= hour < skip_e:
            return False
    return any(s <= hour < e for s, e in cfg.session_windows)


def run_backtest(df_higher_full: pd.DataFrame, df_entry_full: pd.DataFrame,
                 cfg: FiboRetracementConfig, end_i: Optional[int] = None) -> pd.DataFrame:
    """Event-driven backtest. ``end_i`` truncates df_entry_full (exclusive)
    for Gate 0; df_higher_full is truncated to the same wall-clock point so
    the higher_tf never sees bars beyond what end_i allows on entry_tf."""
    if cfg.variant != "classic_swing":
        raise ValueError(f"engine.run_backtest only implements classic_swing, got {cfg.variant!r}")

    entry = df_entry_full if end_i is None else df_entry_full.iloc[:end_i]
    n = len(entry)
    if n < 2:
        return pd.DataFrame()
    higher = df_higher_full[df_higher_full.index <= entry.index[-1]]

    pivots = get_swings(higher, cfg.swing_atr_mult, cfg.atr_period)
    zone_by_confirm = _zones_by_confirm(pivots, cfg)
    ema = higher["close"].ewm(span=cfg.ema_period, adjust=False).mean()

    entry_highs_v, entry_lows_v = entry["high"].values, entry["low"].values
    fvg_lv = fvg_levels(entry_highs_v, entry_lows_v) if cfg.require_fvg_confluence else None

    # Sweep-precondition structure (only computed when actually needed --
    # cfg.fvg_sweep_mode is None for E2/base, so this is a no-op there).
    sweep_pivots_swing: List[Pivot] = []
    sweep_pivots_induce: List[Pivot] = []
    if cfg.require_fvg_confluence and cfg.fvg_sweep_mode in ("swing", "either"):
        sweep_pivots_swing = get_swings(entry, cfg.fvg_sweep_swing_atr_mult, cfg.atr_period)
    if cfg.require_fvg_confluence and cfg.fvg_sweep_mode in ("inducement", "either"):
        sweep_pivots_induce = fractal_pivots(entry_highs_v, entry_lows_v,
                                             cfg.fvg_sweep_inducement_fractal_bars)

    # Map each higher_tf confirm event to the first entry_tf bar strictly
    # after that higher_tf bar's close time. The trend-filter reading is
    # taken at the SAME higher_tf bar that confirmed the pivot (already
    # closed information at that instant); only the *usability* of the
    # resulting zone is deferred to the next entry_tf bar.
    confirm_idx_to_k = {p.confirm_idx: k for k, p in enumerate(pivots)}

    entry_times = entry.index
    events: List[tuple] = []
    for confirm_i, zone in zone_by_confirm.items():
        if cfg.trend_filter_mode == "structure":
            ok = _structure_trend_ok(pivots, confirm_idx_to_k[confirm_i], zone.side)
        else:
            ok = _trend_ok(zone.side, higher["close"].iloc[confirm_i], ema.iloc[confirm_i],
                           cfg.trend_filter_mode)
        if not ok:
            continue
        bar_time = higher.index[confirm_i]
        pos = entry_times.searchsorted(bar_time, side="right")
        if pos < n:
            events.append((pos, zone))
    events.sort(key=lambda e: e[0])
    events_by_i: Dict[int, Zone] = {}
    for i, z in events:
        events_by_i[i] = z  # later event at the same i overwrites -- freshest wins

    opens, highs, lows, closes = (entry["open"].values, entry["high"].values,
                                  entry["low"].values, entry["close"].values)

    trades = []
    pos_state = None        # dict(side, entry, sl, tp, entry_i, risk)
    active_zone: Optional[Zone] = None
    pending_fill: Optional[float] = None
    pending_side = 0

    for i in range(1, n):
        if i in events_by_i:
            active_zone = events_by_i[i]
            pending_fill, pending_side = None, 0  # fresher leg cancels a stale pending order

        # 1) open a pending fill at this bar's open -- gated by the session
        # filter (cfg.session_windows). A fill outside an allowed session is
        # simply not taken (pending cleared, no position); the zone itself
        # stays active (unless invalidated below by step 3), so a LATER
        # touch -- same zone or a fresher one -- can still arm and fill at a
        # valid hour. This is what "пропускаем и ищем модель после" means in
        # code: skip this specific fill, keep watching.
        if pending_fill is not None and pos_state is None:
            if _session_ok(cfg, entry.index[i]):
                z = active_zone
                entry_px = opens[i]
                sl = z.stop
                if cfg.tp_mode == "rr_multiple":
                    risk_dist = abs(entry_px - sl)
                    tp = entry_px + pending_side * cfg.tp_rr * risk_dist
                else:
                    tp = entry_px + pending_side * cfg.tp_fib * z.impulse_height
                risk = pending_side * (entry_px - sl)
                if risk > 0:
                    pos_state = dict(side=pending_side, entry=entry_px, sl=sl, tp=tp,
                                     entry_i=i, risk=risk)
            pending_fill, pending_side = None, 0

        # 2) manage open position
        if pos_state is not None:
            s = pos_state["side"]
            hit_sl = lows[i] <= pos_state["sl"] if s == LONG else highs[i] >= pos_state["sl"]
            hit_tp = highs[i] >= pos_state["tp"] if s == LONG else lows[i] <= pos_state["tp"]
            exit_price = reason = None
            if hit_sl:
                exit_price, reason = pos_state["sl"], "sl"
            elif hit_tp:
                exit_price, reason = pos_state["tp"], "tp"
            if exit_price is not None:
                gross_r = s * (exit_price - pos_state["entry"]) / pos_state["risk"]
                net_r = gross_r - cfg.spread_pts / pos_state["risk"]
                trades.append(dict(
                    entry_time=entry.index[pos_state["entry_i"]], exit_time=entry.index[i],
                    side=s, entry=pos_state["entry"], exit=exit_price, sl=pos_state["sl"],
                    tp=pos_state["tp"], risk_pts=pos_state["risk"], reason=reason,
                    entry_i=pos_state["entry_i"], exit_i=i, gross_r=gross_r, r=net_r))
                pos_state = None
                active_zone = None  # zone consumed; wait for the next one

        # 3) evaluate zone touch on this closed bar (only when flat, no pending)
        if pos_state is None and pending_fill is None and active_zone is not None:
            z = active_zone
            beyond_stop = closes[i] < z.stop if z.side == LONG else closes[i] > z.stop
            if beyond_stop:
                active_zone = None
            else:
                touched = lows[i] <= z.near <= highs[i]
                if touched and i + 1 < n:
                    confluence_ok = _fvg_confluence_ok(
                        cfg, fvg_lv, i, z, entry_highs_v, entry_lows_v,
                        sweep_pivots_swing, sweep_pivots_induce)
                    if confluence_ok:
                        pending_fill, pending_side = z.near, z.side

    return pd.DataFrame(trades)
