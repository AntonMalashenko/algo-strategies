"""Event-driven daily simulator for S007 (single source of truth).

Consolidates pyramid.py / pyramid_stops.py / pyramid_v2.py into one config-driven
state machine. Bar-by-bar, no look-ahead (structure confirmed with lag k; stop
checked before target within a bar = conservative). R accounting: each position
risks 1R (risk = |entry - its stop|); the day's R is the (optionally add-weighted)
sum across positions. Costs are applied separately (see costs.py).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import StrategyConfig
from .data import daily_levels
from .setups import find_setup
from .structure import structure_levels


def pick_stop(mode, t, entry, up, L, range_stop):
    def ok(v):
        return (not np.isnan(v)) and ((up and v < entry) or ((not up) and v > entry))

    if mode == "mid_range":
        return range_stop
    if mode == "last_swing":
        chain = [L["last_sl"][t] if up else L["last_sh"][t]]
    elif mode == "prev_swing":
        chain = [L["prev_sl"][t] if up else L["prev_sh"][t],
                 L["last_sl"][t] if up else L["last_sh"][t]]
    elif mode == "prev_fvg":
        chain = [L["bull"][t] if up else L["bear"][t],
                 L["last_sl"][t] if up else L["last_sh"][t]]
    else:
        raise ValueError(f"unknown stop_mode {mode!r}")
    for v in chain:
        if ok(v):
            return v
    return range_stop


def liquidity_tp(up, entry, rh, rl, height, lv, L, t):
    """Nearest liquidity proxy beyond the entry and beyond the broken boundary."""
    range_tp = (rh + height) if up else (rl - height)
    cands = []
    if up:
        for key in ("asia_high", "prev_day_high"):
            v = lv.get(key, np.nan)
            if not np.isnan(v) and v > entry:
                cands.append(v)
        sh = L["prev_sh"][t]
        if not np.isnan(sh) and sh > entry:
            cands.append(sh)
        cands = [c for c in cands if c > rh]
        return min(cands) if cands else range_tp
    else:
        for key in ("asia_low", "prev_day_low"):
            v = lv.get(key, np.nan)
            if not np.isnan(v) and v < entry:
                cands.append(v)
        sl = L["prev_sl"][t]
        if not np.isnan(sl) and sl < entry:
            cands.append(sl)
        cands = [c for c in cands if c < rl]
        return max(cands) if cands else range_tp


def _simulate_leg(highs, lows, closes, L, start_idx, e_price, up, tp, range_stop, cfg,
                  buffer, swing_buffer=0.0, add_cut_idx=None):
    """Run one entry + its pyramiding leg from start_idx. Returns (positions, reached)
    or (None, False) if the first entry fails the min-risk guard. Each position
    stores its own direction ('up') so mixed-direction days aggregate correctly.
    An add is validated only if the broken swing is >= swing_buffer (meaningful
    CHoCH, not a micro-swing) and t <= add_cut_idx (add-time window)."""
    n = len(closes)
    stop0 = pick_stop(cfg.stop_mode, start_idx, e_price, up, L, range_stop)
    if buffer > 0 and abs(e_price - stop0) < buffer:
        return None, False
    positions = [dict(entry=e_price, stop=stop0, status="open", exit=None,
                      is_add=False, up=up, idx=start_idx, tp=tp)]
    armed = False
    armed_price = np.nan   # the pullback swing extreme that armed the current add
    last_add = start_idx
    reached = False
    eff_max = 10**9 if (cfg.unlimited_adds or cfg.daily_loss_cap_R is not None) else cfg.max_positions

    def _agg_R(mark):
        """Aggregate day P&L in R (realized closed + open marked to `mark`)."""
        tot = 0.0
        for p in positions:
            risk = abs(p["entry"] - p["stop"])
            if risk <= 0:
                continue
            px = mark if p["status"] == "open" else p["exit"]
            tot += (px - p["entry"]) / risk if p["up"] else (p["entry"] - px) / risk
        return tot

    for t in range(start_idx, n):
        hi, lo, c = highs[t], lows[t], closes[t]
        for p in positions:
            if p["status"] != "open":
                continue
            if up:
                if lo <= p["stop"]:
                    p["status"] = "stop"; p["exit"] = p["stop"]
                elif hi >= tp:
                    p["status"] = "tp"; p["exit"] = tp
            else:
                if hi >= p["stop"]:
                    p["status"] = "stop"; p["exit"] = p["stop"]
                elif lo <= tp:
                    p["status"] = "tp"; p["exit"] = tp
        if (up and hi >= tp) or ((not up) and lo <= tp):
            for p in positions:
                if p["status"] == "open":
                    p["status"] = "tp"; p["exit"] = tp
            reached = True
            break
        # aggregate daily-loss cap: close everything if day P&L (MtM) <= -cap
        if cfg.daily_loss_cap_R is not None and _agg_R(c) <= -cfg.daily_loss_cap_R:
            for p in positions:
                if p["status"] == "open":
                    p["status"] = "daycap"; p["exit"] = c
            break
        budget_ok = True
        if cfg.daily_loss_cap_R is not None:
            # potential-loss budget: realized losses (stopped) + open risk + this one
            spent = sum(1 for p in positions if p["status"] == "stop")
            openc = sum(1 for p in positions if p["status"] == "open")
            budget_ok = (spent + openc + 1) <= cfg.daily_loss_cap_R
        if (cfg.do_pyramid and len(positions) < eff_max and t > last_add and budget_ok
                and (add_cut_idx is None or t <= add_cut_idx)):
            piv = t - cfg.k
            if piv > last_add:
                if up and L["is_sl"][piv]:
                    armed = True; armed_price = lows[piv]
                if (not up) and L["is_sh"][piv]:
                    armed = True; armed_price = highs[piv]
            lvl = L["last_sh"][t] if up else L["last_sl"][t]
            if armed and not np.isnan(lvl):
                if (up and c > lvl) or ((not up) and c < lvl):
                    # validate the add: broken swing must be a meaningful CHoCH
                    if (swing_buffer > 0 and not np.isnan(armed_price)
                            and abs(lvl - armed_price) < swing_buffer):
                        armed = False  # ignore micro-CHoCH; wait for a fresh pullback+break
                    elif (up and c < tp) or ((not up) and c > tp):
                        st = pick_stop(cfg.stop_mode, t, c, up, L, range_stop)
                        if buffer > 0 and abs(c - st) < buffer:
                            continue
                        positions.append(dict(entry=c, stop=st, status="open",
                                              exit=None, is_add=True, up=up, idx=t, tp=tp))
                        armed = False
                        last_add = t
    last_close = closes[-1]
    for p in positions:
        if p["status"] == "open":
            p["status"] = "eod"; p["exit"] = last_close
    return positions, reached


def simulate_day(bars: pd.DataFrame, rh, rl, mid, height, lv, cfg: StrategyConfig):
    highs = bars["high"].to_numpy(float)
    lows = bars["low"].to_numpy(float)
    closes = bars["close"].to_numpy(float)
    opens = bars["open"].to_numpy(float)

    scenario, direction, e_idx, e_price = find_setup(opens, closes, rh, rl, mid)
    if scenario is None:
        return dict(scenario="NONE")
    if (scenario == "A" and not cfg.allow_A) or (scenario == "B" and not cfg.allow_B):
        return dict(scenario="NONE")

    up = direction == "up"
    # Scenario-A filter: skip if the entry (confirmation) candle already tags the
    # target boundary (up -> rh, down -> rl) — the A move is exhausted at entry.
    if scenario == "A" and cfg.skip_A_entry_reaches_boundary:
        if (up and highs[e_idx] >= rh) or ((not up) and lows[e_idx] <= rl):
            return dict(scenario="NONE")
    range_stop = (rl if up else rh) if scenario == "A" else mid
    L = structure_levels(highs, lows, cfg.k)

    if cfg.tp_mode == "range":
        tp = (rh if up else rl) if scenario == "A" else ((rh + height) if up else (rl - height))
    elif cfg.tp_mode == "liquidity":
        tp = liquidity_tp(up, e_price, rh, rl, height, lv, L, e_idx)
    else:
        raise ValueError(f"unknown tp_mode {cfg.tp_mode!r}")

    n = len(closes)
    buffer = max(cfg.min_risk_points, cfg.min_risk_frac * height)
    swing_buffer = max(cfg.min_swing_points, cfg.min_swing_frac * height)
    add_cut_idx = None
    if cfg.add_window_end is not None:
        tt = bars["time_only"].to_numpy()
        idxs = np.where(tt <= cfg.add_window_end)[0]
        add_cut_idx = int(idxs[-1]) if len(idxs) else -1

    # primary leg (the detected A or B setup)
    positions, reached = _simulate_leg(highs, lows, closes, L, e_idx, e_price, up,
                                       tp, range_stop, cfg, buffer,
                                       swing_buffer=swing_buffer, add_cut_idx=add_cut_idx)
    if positions is None:  # first entry failed the min-risk guard -> no trade
        return dict(scenario="NONE")
    stop0 = positions[0]["stop"]

    # B-reversal -> A model: a FAILED B breakout (never reached its target) that
    # returns to 0.5 flips into a scenario-A trade from the midline toward the
    # opposite boundary (wide common stop + pyramiding). Recovers losing-B days.
    n_recovery = 0
    if cfg.b_reversal_to_A and scenario == "B" and not reached:
        rev_idx = None
        for t in range(e_idx + 1, n):
            if (up and lows[t] <= mid) or ((not up) and highs[t] >= mid):
                rev_idx = t
                break
        if rev_idx is not None and rev_idx < n - 1:
            up_A = not up
            tp_A = rh if up_A else rl                     # opposite boundary
            range_stop_A = rl if up_A else rh             # origin boundary (A-style)
            leg2, _ = _simulate_leg(highs, lows, closes, L, rev_idx, mid, up_A,
                                    tp_A, range_stop_A, cfg, buffer,
                                    swing_buffer=swing_buffer, add_cut_idx=add_cut_idx)
            if leg2 is not None:
                for p in leg2:
                    p["is_recovery"] = True
                positions.extend(leg2)
                n_recovery = len(leg2)

    cost_points = 2.0 * cfg.spread_per_side + cfg.commission_points
    day_R = 0.0
    for p in positions:
        risk = abs(p["entry"] - p["stop"])
        pu = p["up"]
        if risk <= 0:
            p["R"] = 0.0
        elif pu:
            p["R"] = (p["exit"] - p["entry"]) / risk
        else:
            p["R"] = (p["entry"] - p["exit"]) / risk
        # subtract round-trip cost, expressed in R (cost_points / risk_points)
        if risk > 0 and cost_points > 0:
            p["R"] -= cost_points / risk
        weight = cfg.risk_per_add if p["is_add"] else 1.0
        day_R += weight * p["R"]

    n_tp = sum(1 for p in positions if p["status"] == "tp")
    n_stop = sum(1 for p in positions if p["status"] == "stop")
    n_eod = sum(1 for p in positions if p["status"] == "eod")
    # diagnostic features known at/just after entry (for filter analysis; no look-ahead)
    try:
        entry_time = str(bars["time_only"].iloc[e_idx])
    except Exception:
        entry_time = None
    return dict(scenario=scenario, direction=direction, height=height,
                n_pos=len(positions), n_tp=n_tp, n_stop=n_stop, n_eod=n_eod,
                n_recovery=n_recovery, positions=positions, tp=tp,
                reached_tp=reached, day_R=day_R, first_R=positions[0]["R"],
                entry_time=entry_time, entry_idx=e_idx,
                first_risk=abs(e_price - stop0), open_above_mid=bool(opens[0] > mid),
                entry_price=e_price, rh=rh, rl=rl, mid=mid)


def run(df: pd.DataFrame, cfg: StrategyConfig, lv: dict | None = None) -> pd.DataFrame:
    """Run the simulator across every day in df; return one row per traded day."""
    if lv is None and cfg.tp_mode == "liquidity":
        lv = daily_levels(df)
    lv = lv or {}

    # pre-compute overnight-gap map only if the gap filter is active
    gap_map = None
    if cfg.max_gap_points is not None:
        rth = df[(df["time_only"] >= "09:00") & (df["time_only"] <= "17:29")]
        dclose = rth.groupby("date_only")["close"].last()
        lopen = df[df["time_only"] == cfg.trade_start].groupby("date_only")["open"].first()
        dates = sorted(df["date_only"].unique())
        gap_map = {}
        for i, d in enumerate(dates):
            pc = dclose.get(dates[i - 1], np.nan) if i > 0 else np.nan
            gap_map[d] = abs(lopen.get(d, np.nan) - pc)

    rows = []
    for day, day_df in df.groupby("date_only"):
        fr = day_df[(day_df["time_only"] >= cfg.fr_start) & (day_df["time_only"] <= cfg.fr_end)]
        if len(fr) < cfg.min_fr_bars:
            continue
        rh, rl = fr["high"].max(), fr["low"].min()
        mid = (rh + rl) / 2
        height = rh - rl
        if height <= 0:
            continue
        if cfg.max_height is not None and height > cfg.max_height:
            continue
        if cfg.min_height is not None and height < cfg.min_height:
            continue
        if gap_map is not None:
            g = gap_map.get(day, np.nan)
            if not np.isnan(g) and g > cfg.max_gap_points:
                continue
        ld = day_df[(day_df["time_only"] >= cfg.trade_start) & (day_df["time_only"] <= cfg.exit_end)]
        if len(ld) < cfg.min_ld_bars:
            continue
        bars = ld.reset_index(drop=True)
        r = simulate_day(bars, rh, rl, mid, height, lv.get(day, {}), cfg)
        if r.get("scenario") in ("A", "B"):
            r["date"] = day
            rows.append(r)
    return pd.DataFrame(rows)


def summarize(res: pd.DataFrame, label: str = "") -> None:
    if label:
        print(f"\n===== {label} =====")
    if len(res) == 0 or "scenario" not in res.columns:
        print("  (no traded days)")
        return
    for sc in ["A", "B", "ALL"]:
        sub = res if sc == "ALL" else res[res["scenario"] == sc]
        if len(sub) == 0:
            continue
        cum = sub.sort_values("date")["day_R"].cumsum().to_numpy()
        mdd = (cum - np.maximum.accumulate(cum)).min() if len(cum) else 0.0
        print(f"  {sc}: n={len(sub):<4} exp={sub.day_R.mean():+.3f}R  sum={sub.day_R.sum():+7.1f}R  "
              f"days+={100 * (sub.day_R > 0).mean():3.0f}%  avg_pos={sub.n_pos.mean():.2f}  "
              f"maxDD={mdd:7.1f}R")
