"""S031 engine: one model at a time, D1 levels -> M5 signals -> M1 fills/exits.

Execution model (all causal):
  * Signals for bounce / breakout / fb3 are evaluated on the CLOSE of an M5 bar;
    the order is live from the next M5 bar on. The course's "place the limit
    30 s before BPU2 closes" is modelled as "at BPU2 close" (the bar is
    practically complete; this is the only possible bias and it is tiny).
  * fb2 places its stop order at the OPEN of the second bar, fb1 places it the
    moment the level is breached inside the bar -- both resolved on M1.
  * Fills and exits are walked minute by minute. In the fill minute only the
    stop-loss is checked (conservative); TP from the next minute on. A minute
    that touches both SL and TP is booked as SL.
  * Positions/orders do not survive the session end (course: intraday, close
    manually at the end of the day).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import time as dtime

import numpy as np
import pandas as pd

from utils.histdata import resample_ohlc

from .config import (INSTRUMENTS, MODEL_BOUNCE, MODEL_BREAKOUT, MODEL_FB1, MODEL_FB2,
                     MODEL_FB3, TREND_MODE_NONE, GerchikConfig)
from .levels import Level, build_daily, gerchik_atr, levels_by_day, stop_size, trading_day_index

M5_RULE = "5min"
FB3_MAX_LOOKBACK_BARS = 96          # how far back the fb3 breakout bar may be (8h of M5)
MINUTES_PER_HOUR = 60


@dataclass
class Trade:
    symbol: str
    model: str
    day: pd.Timestamp
    side: int
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    entry: float
    sl: float
    tp: float
    exit_time: pd.Timestamp | None = None
    exit: float = np.nan
    reason: str = ""
    risk: float = np.nan
    r_gross: float = np.nan
    r_net: float = np.nan
    level: float = np.nan
    level_kind: str = ""
    level_touches: int = 0
    level_mirror: bool = False
    level_round: bool = False
    level_age_days: int = 0
    atr: float = np.nan
    atr_used: float = np.nan
    with_trend: bool = True
    calc_stop: float = np.nan
    tech_stop: bool = False


@dataclass
class _Order:
    kind: str              # "limit" | "stop"
    side: int              # +1 long, -1 short
    price: float
    level: Level
    calc_stop: float
    signal_time: pd.Timestamp
    atr_used: float
    with_trend: bool
    cancel_away: float = np.inf   # cancel if a bar CLOSES this far beyond entry against the fill side
    cancel_close_beyond: float = np.nan  # cancel if a bar closes beyond this price (break / model broken)
    cancel_at_reach: float = np.nan      # cancel if price reaches this (next level)
    expires_bar: int = -1                # last M5 bar index the order lives through (-1 = session)
    extreme: float = np.nan              # FB: running extreme beyond the level (for the technical stop)
    meta: dict = field(default_factory=dict)


def _session_utc(day: pd.Timestamp, sess: tuple[str, str, str]) -> tuple[pd.Timestamp, pd.Timestamp]:
    tz, s, e = sess
    hs, ms = map(int, s.split(":"))
    he, me = map(int, e.split(":"))
    start = pd.Timestamp.combine(day.date(), dtime(hs, ms)).tz_localize(tz).tz_convert("UTC").tz_localize(None)
    end = pd.Timestamp.combine(day.date(), dtime(he, me)).tz_localize(tz).tz_convert("UTC").tz_localize(None)
    return start, end


class _DayContext:
    """Everything known at the start of a trading day + running day stats."""

    def __init__(self, j, d1, atr, lv, cfg, spread):
        self.j = j
        self.cfg = cfg
        self.atr = float(atr[j])
        self.prev_close = float(d1["close"].iloc[j - 1])
        lo_win = max(0, j - cfg.level_lookback_days)
        self.range_hi = float(d1["high"].iloc[lo_win:j].max())
        self.range_lo = float(d1["low"].iloc[lo_win:j].min())
        self.levels: list[Level] = lv
        self.prices = np.array(sorted(L.price for L in lv))
        self.spread = spread
        self.day_hi = -np.inf
        self.day_lo = np.inf
        self.losses = 0

    def atr_used(self) -> float:
        hi = max(self.day_hi, self.prev_close)
        lo = min(self.day_lo, self.prev_close)
        return (hi - lo) / self.atr

    def direction_ok(self, side: int, price: float) -> tuple[bool, bool, float]:
        trend = 1 if price >= self.prev_close else -1
        with_trend = side == trend
        used = self.atr_used()
        if self.cfg.trend_mode == TREND_MODE_NONE:
            return True, with_trend, used
        at_extreme = price >= self.range_hi or price <= self.range_lo
        if used < self.cfg.atr_used_threshold:
            return with_trend, with_trend, used
        return (not with_trend) or at_extreme, with_trend, used

    def room_ok(self, side: int, level: float, entry: float, stop: float) -> bool:
        gap = self.cfg.level_merge_frac * stop
        if side > 0:
            nxt = self.prices[self.prices > level + gap]
            if len(nxt) == 0:
                return True
            nl = nxt.min()
        else:
            nxt = self.prices[self.prices < level - gap]
            if len(nxt) == 0:
                return True
            nl = nxt.max()
        if abs(nl - entry) < self.cfg.min_room_stops * stop:
            return False
        if self.cfg.require_tech_atr and abs(nl - level) < self.atr:
            return False
        return True

    def next_level(self, side: int, level: float, stop: float) -> float:
        gap = self.cfg.level_merge_frac * stop
        nxt = self.prices[self.prices > level + gap] if side > 0 else self.prices[self.prices < level - gap]
        if len(nxt) == 0:
            return np.nan
        return nxt.min() if side > 0 else nxt.max()


def simulate(symbol: str, m1: pd.DataFrame, cfg: GerchikConfig) -> list[Trade]:
    spec = INSTRUMENTS[symbol]
    d1 = build_daily(m1)
    atr = gerchik_atr(d1, cfg)
    lv_by_day = levels_by_day(d1, atr, cfg)
    day_pos = {d: i for i, d in enumerate(d1.index)}

    m5 = resample_ohlc(m1, M5_RULE)
    t5 = m5.index
    o5, h5, l5, c5 = (m5[k].to_numpy() for k in ("open", "high", "low", "close"))
    td5 = trading_day_index(t5)

    t1 = m1.index
    o1, h1, l1, c1 = (m1[k].to_numpy() for k in ("open", "high", "low", "close"))
    m1_start = t1.searchsorted(t5)
    m1_end = t1.searchsorted(t5 + pd.Timedelta(M5_RULE))

    trades: list[Trade] = []
    # group M5 bars by trading day
    bounds = np.flatnonzero(np.r_[True, td5[1:] != td5[:-1], True])
    for a, b in zip(bounds[:-1], bounds[1:]):
        day = td5[a]
        j = day_pos.get(day)
        if j is None or j < 1 or np.isnan(atr[j]):
            continue
        lv_all = lv_by_day[j]
        if not lv_all:
            continue
        day_open = float(d1["open"].iloc[j])
        scan = cfg.level_scan_atr * atr[j]
        lv = [L for L in lv_all if abs(L.price - day_open) <= scan]
        ctx = _DayContext(j, d1, atr, lv_all, cfg, spec["spread"])
        if cfg.min_atr_in_stops > 0 and atr[j] < cfg.min_atr_in_stops * stop_size(day_open, atr[j], cfg):
            continue
        s_start, s_end = _session_utc(day, spec["session"])
        trades.extend(_run_day(symbol, cfg, ctx, lv, day, a, b, s_start, s_end,
                               t5, o5, h5, l5, c5, m1_start, m1_end, t1, o1, h1, l1, c1))
    return trades


def _run_day(symbol, cfg, ctx, lv, day, a, b, s_start, s_end,
             t5, o5, h5, l5, c5, m1s, m1e, t1, o1, h1, l1, c1):
    out: list[Trade] = []
    pos: Trade | None = None
    order: _Order | None = None
    last_entry_time = s_end - pd.Timedelta(minutes=cfg.no_entry_last_minutes)
    bounce_from = s_start + pd.Timedelta(minutes=MINUTES_PER_HOUR) if cfg.skip_first_hour_bounce else s_start
    used_levels: set[float] = set()   # one attempt per level per day per model

    def in_session(t):
        return s_start <= t < s_end

    def can_place(t):
        return (pos is None and order is None and ctx.losses < cfg.max_losses_per_day
                and s_start <= t < last_entry_time)

    def open_trade(o: _Order, t, fill_px, tech_sl=np.nan):
        side = o.side
        calc_sl = fill_px - side * o.calc_stop
        sl, tech = calc_sl, False
        if not np.isnan(tech_sl):
            r_tech = (tech_sl - fill_px) * -side
            if 0 < r_tech <= cfg.tech_stop_max_ratio * o.calc_stop:
                r_tech = max(r_tech, cfg.min_risk_stop_frac * o.calc_stop)
                sl, tech = fill_px - side * r_tech, True
        risk = abs(fill_px - sl)
        L = o.level
        return Trade(symbol=symbol, model=cfg.model, day=day, side=side,
                     signal_time=o.signal_time, entry_time=t, entry=fill_px, sl=sl,
                     tp=fill_px + side * cfg.rr_target * risk, risk=risk, level=L.price,
                     level_kind=L.kind, level_touches=L.touches, level_mirror=L.mirror,
                     level_round=L.round_number, level_age_days=ctx.j - L.pivot_day,
                     atr=ctx.atr, atr_used=o.atr_used, with_trend=o.with_trend,
                     calc_stop=o.calc_stop, tech_stop=tech)

    def close_trade(p: Trade, t, px, reason):
        p.exit_time, p.exit, p.reason = t, px, reason
        p.r_gross = (px - p.entry) * p.side / p.risk
        p.r_net = p.r_gross - ctx.spread / p.risk
        if p.r_net < 0:
            ctx.losses += 1
        out.append(p)

    def walk_position(p: Trade, k0, k1, first_minute_sl_only=None):
        """Walk M1 [k0,k1). Returns True if the position closed."""
        for k in range(k0, k1):
            if t1[k] >= s_end:
                close_trade(p, t1[k - 1], c1[k - 1], "session_end")
                return True
            sl_hit = (l1[k] <= p.sl) if p.side > 0 else (h1[k] >= p.sl)
            if sl_hit:
                px = min(p.sl, o1[k]) if p.side > 0 else max(p.sl, o1[k])
                close_trade(p, t1[k], px, "sl")
                return True
            if k == first_minute_sl_only:
                continue
            tp_hit = (h1[k] >= p.tp) if p.side > 0 else (l1[k] <= p.tp)
            if tp_hit:
                px = max(p.tp, o1[k]) if p.side > 0 else min(p.tp, o1[k])
                close_trade(p, t1[k], px, "tp")
                return True
        return False

    for i in range(a, b):
        t = t5[i]
        k0, k1 = m1s[i], m1e[i]

        # ---------------- fb1 / fb2 orders created at/inside this bar ----------------
        if cfg.model in (MODEL_FB1, MODEL_FB2) and can_place(t) and i - 1 >= a:
            order = _arm_fb(cfg, ctx, lv, i, a, t, o5, h5, l5, c5, used_levels)

        # ---------------- pending order -> fill ----------------
        if order is not None:
            o = order
            filled_k = None
            for k in range(k0, k1):
                if t1[k] >= s_end:
                    break
                if cfg.model == MODEL_FB1 and not o.meta.get("breached"):
                    brk = (h1[k] > o.meta["breach_px"]) if o.side < 0 else (l1[k] < o.meta["breach_px"])
                    if brk:
                        ok, wt, used = ctx.direction_ok(o.side, o.level.price)
                        if not ok or not ctx.room_ok(o.side, o.level.price, o.price, o.calc_stop):
                            order = None
                            break
                        o.meta["breached"] = True
                        used_levels.add(o.level.price)
                        o.with_trend, o.atr_used, o.signal_time = wt, used, t1[k]
                        o.extreme = h1[k] if o.side < 0 else l1[k]
                    continue
                if cfg.model in (MODEL_FB1, MODEL_FB2, MODEL_FB3):
                    o.extreme = max(o.extreme, h1[k]) if o.side < 0 else min(o.extreme, l1[k])
                if o.kind == "limit":
                    hit = (l1[k] <= o.price) if o.side > 0 else (h1[k] >= o.price)
                    px = min(o.price, o1[k]) if o.side > 0 else max(o.price, o1[k])
                else:
                    hit = (h1[k] >= o.price) if o.side > 0 else (l1[k] <= o.price)
                    px = max(o.price, o1[k]) if o.side > 0 else min(o.price, o1[k])
                if hit:
                    if cfg.model in (MODEL_FB1, MODEL_FB2, MODEL_FB3):
                        depth = abs(o.extreme - o.level.price)
                        if cfg.model == MODEL_FB1 and depth > cfg.fb_max_depth_atr * ctx.atr:
                            order = None
                            break
                        tech = o.extreme + ctx.cfg.touch_tol_frac * o.calc_stop * (-o.side)
                        pos = open_trade(o, t1[k], px, tech_sl=tech)
                    else:
                        pos = open_trade(o, t1[k], px)
                    order = None
                    filled_k = k
                    break
            if pos is not None and filled_k is not None:
                if walk_position(pos, filled_k, k1, first_minute_sl_only=filled_k):
                    pos = None
            elif order is not None:
                o = order
                # end-of-bar cancellation rules
                cancel = not in_session(t + pd.Timedelta(M5_RULE)) or (o.expires_bar >= 0 and i >= o.expires_bar)
                if not np.isnan(o.cancel_close_beyond):
                    cancel |= (c5[i] < o.cancel_close_beyond) if o.side > 0 else (c5[i] > o.cancel_close_beyond)
                if np.isfinite(o.cancel_away):
                    cancel |= abs(c5[i] - o.price) >= o.cancel_away
                if not np.isnan(o.cancel_at_reach):
                    cancel |= (l5[i] <= o.cancel_at_reach) if o.side > 0 else (h5[i] >= o.cancel_at_reach)
                if cancel:
                    order = None
        elif pos is not None:
            if walk_position(pos, k0, k1):
                pos = None

        # running day stats (after the bar is complete)
        ctx.day_hi = max(ctx.day_hi, h5[i])
        ctx.day_lo = min(ctx.day_lo, l5[i])

        # ---------------- close-of-bar signals ----------------
        t_close = t + pd.Timedelta(M5_RULE)
        if order is None and can_place(t_close):
            if cfg.model == MODEL_BOUNCE and t_close >= bounce_from:
                order = _sig_bounce(cfg, ctx, lv, i, a, t_close, o5, h5, l5, c5, used_levels)
            elif cfg.model == MODEL_BREAKOUT:
                order = _sig_breakout(cfg, ctx, lv, i, a, t_close, h5, l5, c5, used_levels)
            elif cfg.model == MODEL_FB3:
                order = _sig_fb3(cfg, ctx, lv, i, a, t_close, o5, h5, l5, c5, used_levels)

    if pos is not None:            # data ended inside the day
        k = m1e[b - 1] - 1
        close_trade(pos, t1[k], c1[k], "data_end")
    return out


# ------------------------------------------------------------------ signals ---

def _levels_near(lv, price, dist):
    return [L for L in lv if abs(L.price - price) <= dist]


def _sig_bounce(cfg, ctx, lv, i, a, t, o5, h5, l5, c5, used):
    if i - 2 < a:
        return None
    for L in lv:
        if L.price in used:
            continue
        stop = stop_size(L.price, ctx.atr, cfg)
        tol, luft = cfg.touch_tol_frac * stop, cfg.luft_frac * stop
        for side in (1, -1):
            if side > 0:   # support: bars above the level, lows hit it
                bpu1 = abs(l5[i - 1] - L.price) <= tol and c5[i - 1] > L.price
                bpu2 = (l5[i] >= L.price - tol) and (l5[i] <= L.price + luft + tol) and c5[i] > L.price
                compress = c5[i - 2] > c5[i - 1] > c5[i]
            else:          # resistance: bars below, highs hit it
                bpu1 = abs(h5[i - 1] - L.price) <= tol and c5[i - 1] < L.price
                bpu2 = (h5[i] <= L.price + tol) and (h5[i] >= L.price - luft - tol) and c5[i] < L.price
                compress = c5[i - 2] < c5[i - 1] < c5[i]
            if not (bpu1 and bpu2):
                continue
            if cfg.bounce_skip_compression and compress:
                continue
            entry = L.price + side * luft
            ok, wt, used_atr = ctx.direction_ok(side, c5[i])
            if not ok or not ctx.room_ok(side, L.price, entry, stop):
                continue
            used.add(L.price)
            return _Order(kind="limit", side=side, price=entry, level=L, calc_stop=stop,
                          signal_time=t, atr_used=used_atr, with_trend=wt,
                          cancel_away=cfg.bounce_cancel_stops * stop,
                          cancel_close_beyond=L.price - side * tol)
    return None


def _sig_breakout(cfg, ctx, lv, i, a, t, h5, l5, c5, used):
    n = cfg.compression_bars
    w = cfg.breakout_small_bar_window
    if i - n - w + 1 < a:
        return None
    closes = c5[i - n + 1:i + 1]
    rng_recent = (h5[i - n + 1:i + 1] - l5[i - n + 1:i + 1]).mean()
    rng_ref = (h5[i - n - w + 1:i - n + 1] - l5[i - n - w + 1:i - n + 1]).mean()
    if rng_recent > rng_ref:
        return None
    for L in lv:
        if L.price in used:
            continue
        stop = stop_size(L.price, ctx.atr, cfg)
        tol = cfg.touch_tol_frac * stop
        for side in (1, -1):
            if side > 0:
                ok_shape = np.all(closes < L.price) and np.all(np.diff(closes) > 0) \
                    and c5[i] >= L.price - cfg.breakout_near_stops * stop
            else:
                ok_shape = np.all(closes > L.price) and np.all(np.diff(closes) < 0) \
                    and c5[i] <= L.price + cfg.breakout_near_stops * stop
            if not ok_shape:
                continue
            entry = L.price + side * tol
            ok, wt, used_atr = ctx.direction_ok(side, c5[i])
            if not ok or not ctx.room_ok(side, L.price, entry, stop):
                continue
            used.add(L.price)
            return _Order(kind="stop", side=side, price=entry, level=L, calc_stop=stop,
                          signal_time=t, atr_used=used_atr, with_trend=wt,
                          cancel_close_beyond=L.price - side * cfg.breakout_cancel_stops * stop)
    return None


def _arm_fb(cfg, ctx, lv, i, a, t, o5, h5, l5, c5, used):
    """fb1: arm at bar open on the nearest level; fb2: second bar opens beyond."""
    best = None
    for L in lv:
        if L.price in used:
            continue
        stop = stop_size(L.price, ctx.atr, cfg)
        tol = cfg.touch_tol_frac * stop
        if cfg.model == MODEL_FB1:
            # Arm on the level nearest to the bar OPEN (known at bar start, no
            # intrabar peeking). Breach, filters and fill are resolved on M1;
            # the level counts as "attempted" only once it is actually breached.
            d = abs(o5[i] - L.price)
            if d > cfg.fb1_arm_atr * ctx.atr:
                continue
            if best is None or d < best[0]:
                side = -1 if o5[i] < L.price else 1    # short = false breakout UP through resistance
                best = (d, _Order(kind="stop", side=side, price=L.price + side * tol, level=L,
                                  calc_stop=stop, signal_time=t, atr_used=np.nan, with_trend=True,
                                  expires_bar=i,
                                  meta={"breach_px": L.price - side * tol, "breached": False}))
            continue
        else:  # fb2
            if i - 2 < a:
                continue
            for side in (-1, 1):
                if side < 0:
                    pat = c5[i - 2] < L.price and c5[i - 1] > L.price and o5[i] > L.price
                    ext = h5[i - 1]
                else:
                    pat = c5[i - 2] > L.price and c5[i - 1] < L.price and o5[i] < L.price
                    ext = l5[i - 1]
                if not pat:
                    continue
                entry = L.price + side * tol
                ok, wt, used_atr = ctx.direction_ok(side, o5[i])
                if not ok or not ctx.room_ok(side, L.price, entry, stop):
                    continue
                used.add(L.price)
                return _Order(kind="stop", side=side, price=entry, level=L, calc_stop=stop,
                              signal_time=t, atr_used=used_atr, with_trend=wt,
                              expires_bar=i, extreme=ext)
    return best[1] if best is not None else None


def _sig_fb3(cfg, ctx, lv, i, a, t, o5, h5, l5, c5, used):
    n = cfg.fb3_min_bars
    for L in lv:
        if L.price in used:
            continue
        stop = stop_size(L.price, ctx.atr, cfg)
        tol = cfg.touch_tol_frac * stop
        for side in (-1, 1):   # short = held above resistance, then fails
            # walk back from i while bars open AND close beyond with no wick break back
            m = 0
            k = i
            while k > a and k > i - FB3_MAX_LOOKBACK_BARS:
                if side < 0:
                    held = o5[k] > L.price and c5[k] > L.price and l5[k] >= L.price - tol
                else:
                    held = o5[k] < L.price and c5[k] < L.price and h5[k] <= L.price + tol
                if not held:
                    break
                m += 1
                k -= 1
            # bar k must be the breakout bar: closed beyond, previous close on the original side
            if m < n or k - 1 < a:
                continue
            brk = (c5[k] > L.price and c5[k - 1] < L.price) if side < 0 else (c5[k] < L.price and c5[k - 1] > L.price)
            if not brk:
                continue
            if m != n:          # place exactly once, when the n-th held bar closes
                continue
            entry = L.price + side * tol
            ok, wt, used_atr = ctx.direction_ok(side, c5[i])
            if not ok or not ctx.room_ok(side, L.price, entry, stop):
                continue
            used.add(L.price)
            ext = h5[k:i + 1].max() if side < 0 else l5[k:i + 1].min()
            nxt = ctx.next_level(-side, L.price, stop)
            return _Order(kind="stop", side=side, price=entry, level=L, calc_stop=stop,
                          signal_time=t, atr_used=used_atr, with_trend=wt,
                          extreme=ext, cancel_at_reach=nxt)
    return None


def trades_to_frame(trades: list[Trade]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame(columns=[f for f in Trade.__dataclass_fields__])
    return pd.DataFrame([asdict(t) for t in trades])
