"""S007 signal layer — derive the desired position set from recent M1 bars.

Reuses the VALIDATED engine (`ger40_lonfra.simulate_day`) on the day's bars up to
"now", so the live position lifecycle is BY CONSTRUCTION identical to the backtest
— no rule re-implementation. This mirrors S004's bot/signals.py.

Input: M1 DataFrame with a naive EET DatetimeIndex and open/high/low/close columns
(same timezone the engine was validated on — the cTrader adapter already returns
Europe/Bucharest naive bars).
"""
from __future__ import annotations

import pandas as pd

from strategies.ger40_lonfra import simulate_day, daily_levels
from strategies.ger40_lonfra import config as GC
from bot import s007_config as C


def _preset(name=None):
    return getattr(GC, name or C.PRESET)


def _prep(m1: pd.DataFrame) -> pd.DataFrame:
    dt = pd.DatetimeIndex(m1.index)
    df = pd.DataFrame({c.lower(): m1[c].to_numpy() for c in m1.columns})
    df["dt"] = dt
    df["date_only"] = dt.date
    df["time_only"] = dt.strftime("%H:%M")
    return df.sort_values("dt").reset_index(drop=True)


def plan_now(m1: pd.DataFrame, now: pd.Timestamp | None = None,
             preset: str | None = None) -> dict:
    """Return the desired broker state for GER40 as of `now`.

    dict:
      in_window   -- True while inside the trade session (10:00-16:59)
      day_done    -- True if the day's target was reached (=> close all)
      flat        -- True at/after EXIT_END (=> close all)
      positions   -- list of positions that SHOULD be open right now, each:
                     {label, side, entry, sl, tp, is_add}
      direction   -- 'up'/'down'/None for the day
      filtered    -- True only when today's Frankfurt range failed the
                     day-quality height filter -- a verdict that is FINAL for
                     the rest of today (see the height-check branch below).
                     Absent/falsy in every other case, including "not enough
                     bars yet" and "no A/B scenario yet", which can still
                     resolve later today and must keep being polled.
    """
    cfg = _preset(preset).with_(trade_start=C.TRADE_START, exit_end=C.EXIT_END,
                                fr_start=C.FR_START, fr_end=C.FR_END)
    df = _prep(m1)
    now = now or df["dt"].iloc[-1]
    today = now.date()
    now_hm = now.strftime("%H:%M")

    in_window = C.TRADE_START <= now_hm <= C.EXIT_END
    flat = now_hm >= C.EXIT_END

    fr = df[(df.date_only == today) & (df.time_only >= C.FR_START) & (df.time_only <= C.FR_END)]
    if len(fr) < cfg.min_fr_bars:
        return dict(in_window=in_window, day_done=False, flat=flat, positions=[],
                    direction=None, context=None)
    rh, rl = fr["high"].max(), fr["low"].min()
    mid = (rh + rl) / 2
    height = rh - rl
    if height <= 0 or (cfg.max_height is not None and height > cfg.max_height):
        # This verdict is FINAL for the day, not "no signal yet": the Frankfurt
        # range (09:00-09:59) is already fixed and closed by the time we can
        # compute rh/rl at all, so height cannot change on a later call this
        # same day. filtered=True lets a scheduler (scripts/s007_loop.py) stop
        # polling once it sees this, instead of re-checking every minute until
        # 16:59 for an answer that was already decided at ~10:00 (see
        # decisions-log.md 2026-07-23). Contrast with the two branches above/
        # below this one, which return the same in-window "nothing to do yet"
        # shape but for reasons that CAN still resolve later today (bars still
        # arriving, or no A/B scenario yet) -- those must NOT set filtered.
        return dict(in_window=in_window, day_done=False, flat=flat, positions=[],
                    direction=None, context=None, filtered=True)

    ld = df[(df.date_only == today) & (df.time_only >= C.TRADE_START) & (df.time_only <= now_hm)]
    if len(ld) < 2:
        return dict(in_window=in_window, day_done=False, flat=flat, positions=[],
                    direction=None, context=None)
    bars = ld.reset_index(drop=True)

    lv = daily_levels(df) if cfg.tp_mode == "liquidity" else {}
    r = simulate_day(bars, rh, rl, mid, height, lv.get(today, {}), cfg)
    if r.get("scenario") not in ("A", "B"):
        return dict(in_window=in_window, day_done=False, flat=flat, positions=[],
                    direction=None, context=None)

    reached = bool(r["reached_tp"])
    tp = r["tp"]
    context = dict(scenario=r["scenario"], direction=r["direction"],
                   rh=float(rh), rl=float(rl), mid=float(mid), height=float(height),
                   tp=float(tp), n_pos=int(r["n_pos"]))
    wanted = []
    # positions still alive now = those left 'open' at the last bar (engine marks
    # them 'eod' at loop end); 'stop'/'tp'/'daycap' are already closed.
    for p in r["positions"]:
        if p["status"] != "eod":
            continue
        side = "buy" if p["up"] else "sell"
        wanted.append(dict(
            label=f"{C.MAGIC}:{today.isoformat()}:{p['idx']}",
            side=side, entry=float(p["entry"]), sl=float(p["stop"]),
            tp=float(tp), is_add=bool(p["is_add"]),
        ))
    return dict(in_window=in_window, day_done=reached, flat=flat,
                positions=wanted, direction=r["direction"], context=context)
