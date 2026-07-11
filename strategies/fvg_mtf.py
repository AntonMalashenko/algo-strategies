"""Multi-timeframe FVG bounce strategy: H4 fair value gap + M15 confirmation.

Setup (long side; short is mirrored):
  1. A bullish FVG forms on H4: low of bar i+2 > high of bar i.
     Zone = [high[i], low[i+2]], available after bar i+2 closes.
  2. Price returns down into the zone on M15 ("arming" / first touch).
  3. Entry depends on mode:
       base  -- limit fill at zone top on first touch, no confirmation;
       shift -- market structure shift: first M15 close above the last
                confirmed fractal swing high (recorded at arming time);
       fvg15 -- a bullish M15 FVG (low[t] > high[t-2]) forms after arming;
       ob    -- after a shift-style break, place a limit at the top of the
                last bearish M15 candle before the break (order block retest).
  4. Stop-loss modes:
       zone  -- behind the far edge of the H4 FVG zone (+ buffer);
       swing -- behind the reaction extreme since arming (+ buffer).
  5. Take-profit: static, entry +/- RR * risk.

Causality guarantees:
  - H4 zones become available only after the H4 bar CLOSES;
  - fractal swings are used only once confirmed (2 bars later);
  - entries/exits are evaluated bar by bar; if SL and TP are both hit
    inside one M15 bar, the trade is counted as a LOSS (pessimistic).

One open position at a time; each zone trades at most once. A zone dies
when price closes beyond its far edge, or when the confirmation window
after the first touch expires.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BUFFER_PIPS = 2.0      # stop-loss buffer
WINDOW_BARS = 96       # confirmation window after first touch (~1 day of M15)
OB_VALID_BARS = 48     # how long an order-block limit stays active


def resample_h4(m15: pd.DataFrame) -> pd.DataFrame:
    """Build H4 bars from M15 so both TFs are perfectly consistent."""
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    h4 = m15.resample("4h").agg(agg).dropna(subset=["close"])
    return h4


def find_h4_fvg(h4: pd.DataFrame, sweep_lookback: int = 20) -> list[dict]:
    """Return H4 FVG zones with the time they become tradable (bar close).

    Each zone carries a `sweep` tag: True when the impulse that created the
    FVG also swept a prior H4 swing level (liquidity grab). For a bullish
    FVG: the low of the first two pattern bars wicks BELOW the most recent
    confirmed H4 fractal low (within `sweep_lookback` bars). Bearish is
    mirrored. Fractals are used only once confirmed (2 bars later) -- causal.
    The tag does not change trading behaviour; it is analysis metadata.
    """
    hi, lo = h4["high"].values, h4["low"].values
    times = h4.index
    last_sh, last_sl = _last_confirmed_swings(hi, lo)   # per-bar confirmed swings
    # remember WHERE the swing was, to enforce the lookback window
    n = len(h4)
    sw_lo_idx = np.full(n, -1)
    sw_hi_idx = np.full(n, -1)
    cur_lo_i = cur_hi_i = -1
    for t in range(n):
        j = t - 2
        if j - 2 >= 0:
            seg_h = hi[j - 2: j + 3]
            if hi[j] == seg_h.max() and seg_h.argmax() == 2:
                cur_hi_i = j
            seg_l = lo[j - 2: j + 3]
            if lo[j] == seg_l.min() and seg_l.argmin() == 2:
                cur_lo_i = j
        sw_hi_idx[t], sw_lo_idx[t] = cur_hi_i, cur_lo_i

    zones = []
    for i in range(n - 2):
        avail = times[i + 2] + pd.Timedelta(hours=4)  # close of 3rd bar
        if lo[i + 2] > hi[i]:            # bullish gap
            sweep = (not np.isnan(last_sl[i]) and sw_lo_idx[i] >= 0
                     and i - sw_lo_idx[i] <= sweep_lookback
                     and min(lo[i], lo[i + 1]) < last_sl[i])
            zones.append(dict(dir=1, top=lo[i + 2], bot=hi[i],
                              avail=avail, sweep=bool(sweep)))
        elif hi[i + 2] < lo[i]:          # bearish gap
            sweep = (not np.isnan(last_sh[i]) and sw_hi_idx[i] >= 0
                     and i - sw_hi_idx[i] <= sweep_lookback
                     and max(hi[i], hi[i + 1]) > last_sh[i])
            zones.append(dict(dir=-1, top=lo[i], bot=hi[i + 2],
                              avail=avail, sweep=bool(sweep)))
    return zones


def _last_confirmed_swings(h: np.ndarray, l: np.ndarray, k: int = 2):
    """For each bar t, the value of the last fractal swing high/low that is
    already confirmed at t (fractal centre >= k bars in the past)."""
    n = len(h)
    last_sh = np.full(n, np.nan)
    last_sl = np.full(n, np.nan)
    cur_sh, cur_sl = np.nan, np.nan
    for t in range(n):
        j = t - k                       # candidate fractal centre now confirmed
        if j - k >= 0:
            seg_h = h[j - k: j + k + 1]
            if h[j] == seg_h.max() and (seg_h.argmax() == k):
                cur_sh = h[j]
            seg_l = l[j - k: j + k + 1]
            if l[j] == seg_l.min() and (seg_l.argmin() == k):
                cur_sl = l[j]
        last_sh[t], last_sl[t] = cur_sh, cur_sl
    return last_sh, last_sl


def run_backtest(m15: pd.DataFrame, mode: str = "base", stop: str = "zone",
                 rr: float = 2.0, pip: float = 1e-4,
                 spread_pips: float = 0.9,
                 partial_tp: float | None = None,
                 breakeven_at: float | None = None,
                 time_stop_bars: int | None = None,
                 trend_ma_days: int | None = None,
                 trend_align: str = "with",
                 max_reentries: int = 0,
                 return_state: bool = False):
    """Event-driven backtest. Returns a DataFrame of trades.

    Trade-management options (ALL OFF by default -- with every option at None
    the engine reproduces the baseline behaviour exactly, which is the
    rollback guarantee for experiments):
      partial_tp      -- fraction of the position closed at +1R (e.g. 0.5);
                         the remainder keeps running to the full TP.
      breakeven_at    -- once price reaches +<x>R, move SL to entry.
      time_stop_bars  -- exit at close if the trade is still open after N bars.
      trend_ma_days   -- daily-trend filter: compare the PREVIOUS completed
                         day's close to its N-day SMA. trend_align="with"
                         takes longs only in an up-trend (shorts in a
                         down-trend); "against" mirrors it. Causal: only
                         completed days are used.
      max_reentries   -- after a STOP-LOSS exit the zone may be re-traded up
                         to N more times, but only on a FRESH touch: price
                         must first close back beyond the near edge (leave
                         the zone in the trade direction) and then touch the
                         edge again. Zone invalidation still applies.

    Intrabar pessimism: within one bar SL is always assumed to be hit BEFORE
    any favourable level (partial/BE trigger/TP).
    """
    if mode == "base" and stop == "swing":
        raise ValueError("base mode has no reaction swing at entry time")

    o = m15["open"].values
    h = m15["high"].values
    l = m15["low"].values
    c = m15["close"].values
    times = m15.index
    n = len(m15)

    zones = find_h4_fvg(resample_h4(m15))
    zones.sort(key=lambda z: z["avail"])
    last_sh, last_sl = _last_confirmed_swings(h, l)

    # daily trend per M15 bar: previous COMPLETED day's close vs its SMA.
    # trend[t] = +1 (up), -1 (down), 0 (undefined / warm-up)
    if trend_ma_days is not None:
        daily_close = m15["close"].resample("1D").last().dropna()
        sma = daily_close.rolling(trend_ma_days).mean()
        t_daily = np.sign(daily_close - sma).shift(1)   # only completed days
        day_keys = times.floor("D")
        trend = t_daily.reindex(day_keys).fillna(0.0).values
    else:
        trend = None

    buf = BUFFER_PIPS * pip
    cost = spread_pips * pip            # total round-trip cost in price units

    zi = 0                              # next zone to activate
    active: list[dict] = []
    pos = None
    trades = []

    for t in range(n):
        bar_time = times[t]

        # 1. activate zones whose H4 bar has closed
        while zi < len(zones) and zones[zi]["avail"] <= bar_time:
            z = dict(zones[zi])
            z.update(armed=False, armed_at=-1, react=np.nan,
                     ref=np.nan, pending=None, dead=False,
                     entries=0, in_trade=False, wait_exit=False)
            active.append(z)
            zi += 1

        # 2. manage open position (pessimistic: SL first)
        if pos is not None:
            d = pos["dir"]
            risk0 = pos["risk"]
            hit_sl = l[t] <= pos["sl"] if d == 1 else h[t] >= pos["sl"]
            if hit_sl:
                pnl = pos["realized"] + pos["frac"] * (pos["sl"] - pos["entry"]) * d - cost
                reason = "be" if pos["be_done"] and pos["sl"] == pos["entry"] else "sl"
                _record(trades, pos, bar_time, t, pos["sl"], pnl / risk0,
                        reason, mode, stop, rr)
                _release(pos, reason == "sl", max_reentries)
                pos = None
                continue
            if partial_tp and not pos["partial_done"]:
                lvl = pos["entry"] + d * risk0          # partial booked at +1R
                if (h[t] >= lvl) if d == 1 else (l[t] <= lvl):
                    pos["realized"] += partial_tp * risk0
                    pos["frac"] -= partial_tp
                    pos["partial_done"] = True
            if breakeven_at is not None and not pos["be_done"]:
                lvl = pos["entry"] + d * breakeven_at * risk0
                if (h[t] >= lvl) if d == 1 else (l[t] <= lvl):
                    pos["sl"] = pos["entry"]
                    pos["be_done"] = True
            hit_tp = h[t] >= pos["tp"] if d == 1 else l[t] <= pos["tp"]
            if hit_tp:
                pnl = pos["realized"] + pos["frac"] * (pos["tp"] - pos["entry"]) * d - cost
                _record(trades, pos, bar_time, t, pos["tp"], pnl / risk0,
                        "tp", mode, stop, rr)
                _release(pos, False, max_reentries)
                pos = None
                continue
            if time_stop_bars is not None and t - pos["t_in"] >= time_stop_bars:
                pnl = pos["realized"] + pos["frac"] * (c[t] - pos["entry"]) * d - cost
                _record(trades, pos, bar_time, t, c[t], pnl / risk0,
                        "time", mode, stop, rr)
                _release(pos, False, max_reentries)
                pos = None
            continue                    # no new signals while managing

        # 3. zone lifecycle & entries
        for z in active:
            if z["dead"] or z["in_trade"]:
                continue
            d = z["dir"]
            # invalidation: close beyond far edge
            far = z["bot"] if d == 1 else z["top"]
            if (d == 1 and c[t] < far) or (d == -1 and c[t] > far):
                z["dead"] = True
                continue

            near = z["top"] if d == 1 else z["bot"]
            touched = l[t] <= near if d == 1 else h[t] >= near

            if not z["armed"]:
                if z["wait_exit"]:      # after an SL: require a fresh approach
                    left = c[t] > near if d == 1 else c[t] < near
                    if left:
                        z["wait_exit"] = False
                    continue
                if not touched:
                    continue
                z["armed"] = True
                z["armed_at"] = t
                z["react"] = l[t] if d == 1 else h[t]
                z["ref"] = last_sh[t] if d == 1 else last_sl[t]
                if mode == "base":
                    if trend is not None and not _trend_ok(trend[t], d, trend_align):
                        z["dead"] = True    # touched against the filter: consumed
                        continue
                    entry = min(o[t], near) if d == 1 else max(o[t], near)
                    pos = _open(z, entry, t, times, stop, rr, buf, d)
                    if pos is None:
                        z["dead"] = True
                        break           # keep original bar semantics
                    z["entries"] += 1
                    z["in_trade"] = True
                    pos["zref"] = z
                    pos["attempt"] = z["entries"]
                    break
                continue

            # armed: update reaction extreme, check window
            z["react"] = min(z["react"], l[t]) if d == 1 else max(z["react"], h[t])
            if t - z["armed_at"] > WINDOW_BARS:
                z["dead"] = True
                continue

            entry = None
            if mode == "shift" and not np.isnan(z["ref"]):
                if (d == 1 and c[t] > z["ref"]) or (d == -1 and c[t] < z["ref"]):
                    entry = c[t]
            elif mode == "fvg15" and t >= 2:
                if (d == 1 and l[t] > h[t - 2]) or (d == -1 and h[t] < l[t - 2]):
                    entry = c[t]
            elif mode == "ob":
                if z["pending"] is None and not np.isnan(z["ref"]):
                    broke = (d == 1 and c[t] > z["ref"]) or (d == -1 and c[t] < z["ref"])
                    if broke:
                        px = _find_ob(o, c, h, l, z["armed_at"], t, d)
                        if px is not None:
                            z["pending"] = dict(px=px, until=t + OB_VALID_BARS)
                elif z["pending"] is not None:
                    if t > z["pending"]["until"]:
                        z["dead"] = True
                        continue
                    filled = l[t] <= z["pending"]["px"] if d == 1 else h[t] >= z["pending"]["px"]
                    if filled:
                        entry = z["pending"]["px"]

            if entry is not None:
                if trend is not None and not _trend_ok(trend[t], d, trend_align):
                    z["dead"] = True
                    continue
                pos = _open(z, entry, t, times, stop, rr, buf, d)
                if pos is None:
                    z["dead"] = True
                    break               # keep original bar semantics
                z["entries"] += 1
                z["in_trade"] = True
                pos["zref"] = z
                pos["attempt"] = z["entries"]
                break

        active = [z for z in active if not z["dead"]]

        # 4. pessimistic same-bar stop-out for a position opened on this bar
        if pos is not None and pos["time_in"] == bar_time:
            d0 = pos["dir"]
            if (d0 == 1 and l[t] <= pos["sl"]) or (d0 == -1 and h[t] >= pos["sl"]):
                pnl = (pos["sl"] - pos["entry"]) * d0 - cost
                _record(trades, pos, bar_time, t, pos["sl"],
                        pnl / pos["risk"], "sl", mode, stop, rr)
                _release(pos, True, max_reentries)
                pos = None

    if return_state:
        # final engine state for live/paper mirroring: zones still alive
        # (not consumed) and the currently open position, if any
        alive = [z for z in active if not z["dead"] and not z["in_trade"]]
        return pd.DataFrame(trades), dict(zones=alive, pos=pos)
    return pd.DataFrame(trades)


def _release(pos, was_sl: bool, max_reentries: int):
    """Free the zone after a trade: revive it for a re-entry after an SL
    (if attempts remain), otherwise consume it."""
    z = pos.get("zref")
    if z is None:
        return
    z["in_trade"] = False
    if was_sl and z["entries"] <= max_reentries:
        z["armed"] = False
        z["wait_exit"] = True
        z["pending"] = None
    else:
        z["dead"] = True


def _trend_ok(tv: float, d: int, align: str) -> bool:
    """True if the daily trend value allows a trade in direction d."""
    if tv == 0:
        return False                    # warm-up / missing day: stand aside
    want = d if align == "with" else -d
    return tv == want


def _record(trades, pos, bar_time, t, exit_px, r, reason, mode, stop, rr):
    trades.append(dict(
        time_in=pos["time_in"], time_out=bar_time,
        hour=pos["hour"], dir=pos["dir"], entry=pos["entry"],
        sl=pos["sl0"], tp=pos["tp"], exit=exit_px, r=r,
        bars_held=t - pos["t_in"], exit_reason=reason,
        sweep=pos.get("sweep", False), attempt=pos.get("attempt", 1),
        mode=mode, stop=stop, rr=rr,
    ))


def _open(z, entry, t, times, stop, rr, buf, d):
    if stop == "zone":
        sl = (z["bot"] - buf) if d == 1 else (z["top"] + buf)
    else:                               # swing
        sl = (z["react"] - buf) if d == 1 else (z["react"] + buf)
    risk = (entry - sl) * d
    if risk <= 0:
        return None
    tp = entry + d * rr * risk
    return dict(entry=entry, sl=sl, sl0=sl, tp=tp, dir=d, risk=risk,
                frac=1.0, realized=0.0, partial_done=False, be_done=False,
                t_in=t, time_in=times[t], hour=times[t].hour,
                sweep=z.get("sweep", False))


def _find_ob(o, c, h, l, start, end, d):
    """Last opposite-colour candle before the breaking impulse."""
    for t in range(end - 1, start - 1, -1):
        if d == 1 and c[t] < o[t]:
            return h[t]
        if d == -1 and c[t] > o[t]:
            return l[t]
    return None


def session_of(hour: int) -> str:
    """Rough session buckets in broker/server time (typically GMT+2/+3)."""
    if 0 <= hour < 7:
        return "Asia"
    if 7 <= hour < 13:
        return "London"
    if 13 <= hour < 21:
        return "NewYork"
    return "Late"
