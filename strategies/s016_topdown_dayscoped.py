"""S016 day-scoped variant -- EXPERIMENTAL, prototype for Anton's intraday hypothesis
(2026-08-26 chat, voice-dictated spec): unlike `run_backtest` in s016_topdown.py
(continuous cascade across many days, confirmed non-day-scoped by trade-level analysis --
only ~2% of its trades happen to have their H4->H1->entry chain land inside one calendar
day), this variant resets the H4 POI search every calendar day at 00:00 broker time,
requires the H1 confirm to FORM the SAME day inside a specific hour window (Anton:
"конфирм... с десяти [до часу дня]", i.e. CONFIRM_HOUR_LO..CONFIRM_HOUR_HI), and bounds
the M15 entry search to a short lag after the H1 confirm forms (Anton: "в этой свече
часовой, может быть, в паре следующих"). Trades, once entered, are held to SL/TP with NO
forced time-of-day exit (Anton: "сделка торгуется бесконечно... стоп или тейк") -- only
the SETUP SEARCH is day-scoped, not trade management; this matches the base engine's
existing (unchanged) exit loop.

Not a baseline, not a modifier of a frozen base -- S016 has no baseline yet (see
s016_topdown.py's module docstring). Kept as a SEPARATE module rather than editing
run_backtest in place: the day-scoping is a structurally different search loop (a date
gate on three cascade stages), not a parameter tweak, and Anton asked to keep exploring
the continuous-cascade engine's existing data IN PARALLEL (2026-08-26: "да надо новый
движок, но пока давай посмотрим ситуации где лучший результат на текущем").

Reuses every zone-detection primitive from s016_topdown.py (single source of truth,
code-architecture skill) -- only the event loop / day-scoping gates below are new.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.s016_topdown import (
    BUFFER_PIPS, IND_MIN_RISK_PIPS, H4_MIN_RISK_PIPS,
    resample_tf, find_order_blocks, find_fvg_generic, find_inducement,
    _overlaps, first_test, _idx_at_or_after,
)

CONFIRM_HOUR_LO = 10        # Kyiv hour, inclusive -- Anton 2026-08-26 voice spec
CONFIRM_HOUR_HI = 13        # Kyiv hour, exclusive ("с 10 по 13 по Киеву")
MAX_ENTRY_LAG_HOURS = 3.0   # max hours from H1-confirm formed_at to the M15 entry trigger --
                             # reading of "в этой свече часовой, может быть, в паре следующих"
                             # as "within the confirm hour or the next couple hours"; first-pass
                             # guess, untuned, exposed as a parameter for a sensitivity sweep.


def run_backtest_dayscoped(m15: pd.DataFrame, rr: float = 2.0, pip: float = 1e-4,
                            spread_pips: float = 0.9,
                            trend_ma_days: int | None = 20, trend_align: str = "with",
                            trend_source: str = "sma",
                            confirm_hour_lo: int = CONFIRM_HOUR_LO,
                            confirm_hour_hi: int = CONFIRM_HOUR_HI,
                            max_entry_lag_hours: float = MAX_ENTRY_LAG_HOURS) -> pd.DataFrame:
    """Day-scoped D1->H4->H1->M15 funnel. Same cascade rules as s016_topdown.run_backtest
    (zone definitions, non-overlap rule, stop rule, RR target, cost model) plus three added
    day-scope gates: (1) the H4 zone's test must land on the same calendar day it formed,
    (2) the H1 confirm must FORM on that same day inside [confirm_hour_lo, confirm_hour_hi)
    broker time, (3) the M15 entry must trigger within max_entry_lag_hours of the H1
    confirm forming. Returns one row per H4 zone that survived to an actual entry (same
    "one trade per H4 zone" / "one trade per M15 zone" dedup as the base engine).
    """
    times = m15.index
    h_arr, l_arr = m15["high"].values, m15["low"].values
    n = len(m15)

    h4 = resample_tf(m15, "4h")
    h1 = resample_tf(m15, "1h")

    h4_zones = find_order_blocks(h4, pd.Timedelta(hours=4))
    h1_zones = find_order_blocks(h1, pd.Timedelta(hours=1))
    h1_ind_zones = find_inducement(h1, pd.Timedelta(hours=1))
    m15_ob_zones = find_order_blocks(m15, pd.Timedelta(minutes=15))
    m15_fvg_zones = find_fvg_generic(m15, pd.Timedelta(minutes=15))
    h1_zones.sort(key=lambda z: z["avail"])
    h1_ind_zones.sort(key=lambda z: z["avail"])
    m15_ob_zones.sort(key=lambda z: z["avail"])
    m15_fvg_zones.sort(key=lambda z: z["avail"])

    if trend_source == "sma":
        if trend_ma_days is not None:
            daily_close = m15["close"].resample("1D").last().dropna()
            sma = daily_close.rolling(trend_ma_days).mean()
            t_daily = np.sign(daily_close - sma).shift(1)
            day_keys = times.floor("D")
            trend = t_daily.reindex(day_keys).fillna(0.0).values
        else:
            trend = None
    elif trend_source == "prev_day_candle":
        # Anton 2026-08-26 (voice spec + "да коснулись" confirmation): "если вчера был
        # лонговый [день], то лонг, шортовый -- то шорт; если вчера достигли [зоны] в виде
        # фвг/блоков/индьюсмента -- смотреть и лонг и шорт, какая цепочка первая". Two
        # rules combined: (1) default direction = yesterday's D1 candle body direction
        # (close vs open); (2) OVERRIDE to neutral (both directions allowed) if an H4 POI
        # zone was TOUCHED/TESTED yesterday -- read as "yesterday was a reactive/structural
        # day", overriding the plain candle-color bias. Scoped to H4 zones specifically
        # (not H1/M15) because that's the level Anton has called "POI" throughout this
        # chat; flagged here in case he means a broader multi-timeframe touch instead.
        daily = m15.resample("1D").agg({"open": "first", "close": "last"}).dropna()
        day_dir = np.sign(daily["close"] - daily["open"]).shift(1).fillna(0.0)

        touched_dates = set()
        for z in h4_zones:
            i0 = _idx_at_or_after(times, z["avail"])
            if i0 >= n:
                continue
            t0 = first_test(z, h_arr, l_arr, times, i0)
            if t0 is not None:
                touched_dates.add(times[t0].normalize())

        day_index = day_dir.index
        touched_yesterday = pd.Series(
            [(d - pd.Timedelta(days=1)) in touched_dates for d in day_index],
            index=day_index,
        )
        day_dir = day_dir.where(~touched_yesterday, 0.0)

        day_keys = times.floor("D")
        trend = day_dir.reindex(day_keys).fillna(0.0).values
    else:
        raise ValueError(f"unknown trend_source: {trend_source!r}")

    buf = BUFFER_PIPS * pip
    cost = spread_pips * pip
    trades = []
    used_m15 = set()

    def _next_zone(pool, d, after_ts, ref_zone):
        for z in pool:
            if z["dir"] == d and z["formed_at"] > after_ts and not _overlaps(z, ref_zone):
                return z
        return None

    def _next_zone_typed(pools, d, after_ts, ref_zone):
        best, best_tag = None, None
        for pool, tag in pools:
            z = _next_zone(pool, d, after_ts, ref_zone)
            if z is not None and (best is None or z["formed_at"] < best["formed_at"]):
                best, best_tag = z, tag
        return best, best_tag

    for z4 in h4_zones:
        z4_day = z4["formed_at"].normalize()

        i4 = _idx_at_or_after(times, z4["avail"])
        if i4 >= n:
            continue
        t_test4 = first_test(z4, h_arr, l_arr, times, i4)
        if t_test4 is None:
            continue
        ts_test4 = times[t_test4]
        if ts_test4.normalize() != z4_day:
            continue  # gate 1: test spilled into a later day -- not intraday, reject

        z1, z1_type = _next_zone_typed(
            [(h1_zones, "ob"), (h1_ind_zones, "ind")], z4["dir"], ts_test4, z4)
        if z1 is None:
            continue
        if z1["formed_at"].normalize() != z4_day:
            continue  # gate 2a: confirm must form same day as the POI
        confirm_hour = z1["formed_at"].hour
        if not (confirm_hour_lo <= confirm_hour < confirm_hour_hi):
            continue  # gate 2b: confirm must form inside the specified hour window

        i1 = _idx_at_or_after(times, z1["avail"])
        if i1 >= n or i1 <= t_test4:
            i1 = t_test4 + 1
        t_test1 = first_test(z1, h_arr, l_arr, times, i1)
        if t_test1 is None:
            continue
        ts_test1 = times[t_test1]

        cand_ob = _next_zone(m15_ob_zones, z4["dir"], ts_test1, z1)
        cand_fvg = _next_zone(m15_fvg_zones, z4["dir"], ts_test1, z1)
        if cand_ob is None and cand_fvg is None:
            continue
        if cand_ob is None:
            z15 = cand_fvg
        elif cand_fvg is None:
            z15 = cand_ob
        else:
            z15 = cand_ob if cand_ob["avail"] <= cand_fvg["avail"] else cand_fvg

        if id(z15) in used_m15:
            continue
        i15 = _idx_at_or_after(times, z15["avail"])
        if i15 >= n or i15 <= t_test1:
            i15 = t_test1 + 1
        t_entry = first_test(z15, h_arr, l_arr, times, i15)
        if t_entry is None:
            continue

        lag_hours = (times[t_entry] - z1["formed_at"]).total_seconds() / 3600.0
        if lag_hours > max_entry_lag_hours:
            continue  # gate 3: entry too far after the confirm candle

        used_m15.add(id(z15))

        d = z4["dir"]
        if trend is not None:
            tv = trend[t_entry]
            if tv != 0:
                want = d if trend_align == "with" else -d
                if tv != want:
                    continue

        entry = z15["top"] if d == 1 else z15["bot"]
        depth_frac = None
        h4_height_pips = (z4["top"] - z4["bot"]) / pip
        if z1_type == "ind":
            raw_sl = (z15["bot"] - buf) if d == 1 else (z15["top"] + buf)
            raw_risk = (entry - raw_sl) * d
            min_risk = IND_MIN_RISK_PIPS * pip
            if raw_risk > 0 and raw_risk < min_risk:
                sl = entry - d * min_risk
            else:
                sl = raw_sl
            stop_mode = "m15_ind"
        else:
            top4, bot4 = z4["top"], z4["bot"]
            height4 = top4 - bot4
            if d == 1:
                touch_px = max(l_arr[t_test4], bot4)
                depth_frac = (top4 - touch_px) / height4 if height4 > 0 else 0.0
            else:
                touch_px = min(h_arr[t_test4], top4)
                depth_frac = (touch_px - bot4) / height4 if height4 > 0 else 0.0
            if depth_frac >= 0.5:
                raw_sl = (bot4 - buf) if d == 1 else (top4 + buf)
                stop_mode = "h4_far"
            else:
                raw_sl = (touch_px - buf) if d == 1 else (touch_px + buf)
                stop_mode = "h4_touch"
            raw_risk = (entry - raw_sl) * d
            min_risk4 = H4_MIN_RISK_PIPS * pip
            if raw_risk > 0 and raw_risk < min_risk4:
                sl = entry - d * min_risk4
            else:
                sl = raw_sl
        risk = (entry - sl) * d
        if risk <= 0:
            continue
        tp = entry + d * rr * risk

        exit_px = exit_reason = exit_t = None
        for t in range(t_entry + 1, n):
            hit_sl = l_arr[t] <= sl if d == 1 else h_arr[t] >= sl
            if hit_sl:
                exit_px, exit_reason, exit_t = sl, "sl", t
                break
            hit_tp = h_arr[t] >= tp if d == 1 else l_arr[t] <= tp
            if hit_tp:
                exit_px, exit_reason, exit_t = tp, "tp", t
                break
        if exit_px is None:
            continue
        pnl = (exit_px - entry) * d - cost
        trades.append(dict(
            time_in=times[t_entry], time_out=times[exit_t], hour=times[t_entry].hour,
            dir=d, entry=entry, sl=sl, tp=tp, exit=exit_px, r=pnl / risk,
            bars_held=exit_t - t_entry, exit_reason=exit_reason,
            h4_zone_formed=z4["formed_at"], h1_zone_formed=z1["formed_at"],
            m15_zone_type=("ob" if z15 is cand_ob else "fvg"),
            h1_zone_type=z1_type, stop_mode=stop_mode,
            confirm_hour=confirm_hour, lag_hours=round(lag_hours, 2),
            depth_frac=depth_frac, h4_height_pips=h4_height_pips, risk_pips=risk / pip,
        ))

    return pd.DataFrame(trades)
