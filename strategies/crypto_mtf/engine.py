"""Event-driven backtest engine for S008 (deterministic skeleton).

Reads bars strictly left-to-right; at each M15 close only bars already closed by
that instant are visible (HTF bars are aligned by CLOSE time, so no forming-bar
look-ahead). Signal source for the skeleton run is the HTF trend-overlay (the
bot's deterministic entry when ML says Hold); the ML path is added later as a
swappable signal function.

Execution model (documented modelling choices — the live bot places SL/TP on the
exchange and we cannot replay tick fills):
  - Entry at the close of the signal bar.
  - SL/TP monitored on each SUBSEQUENT bar; if a bar's range spans both, SL is
    assumed hit first (conservative).
  - One position at a time; an opposite valid signal reverses (close-at-close,
    then open) when cfg.allow_reverse.
  - Costs charged per side: taker fee + half-spread + slippage on notional.
Accounting is in USDT and in R (net_pnl / risk_usdt at entry).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

from .config import CryptoMTFConfig, BASELINE_S008
from .context import (
    MarketContext, MarketStage, analyse_timeframe, detect_h4_poi,
    h1_confirm_side, _detect_stage, _TREND_SCORE,
)
from .signals import HTFFilter
from .risk import calc_sl_tp_qty

CTX_HISTORY = 200          # HTF bars fed to context (matches live history_limit)
SIGNAL_WINDOW = 30         # M15 candles for the entry signal
MS_PER_MIN = 60_000
H1_MS = 60 * MS_PER_MIN
M15_MS = 15 * MS_PER_MIN
HTF_CLOSE_MS = {"h1": H1_MS, "h4": 4 * H1_MS, "d1": 24 * H1_MS}


@dataclass
class Trade:
    entry_ts: int
    exit_ts: int
    side: str
    entry: float
    exit: float
    qty: float
    risk_usdt: float
    gross_pnl: float
    fees: float
    net_pnl: float
    r_net: float
    reason: str          # "tp" | "sl" | "reverse"
    year: int


def _series(bars: list[dict], key: str) -> np.ndarray:
    return np.fromiter((b[key] for b in bars), dtype=np.float64, count=len(bars))


def run_backtest(
    m15: list[dict],
    h1: list[dict],
    h4: list[dict],
    d1: list[dict],
    cfg: CryptoMTFConfig = BASELINE_S008,
    signal_fn: Optional[Callable] = None,
) -> list[Trade]:
    """Run the skeleton backtest and return the list of closed trades.

    Each bar dict needs ts, open, high, low, close, volume. `signal_fn`, if given,
    is `signal_fn(m15_window, ctx, filt) -> (side|None, conf)`; default is the
    trend-overlay.
    """
    m15_ts = _series(m15, "ts")
    # HTF close-time arrays: a bar with open ts closes at ts + interval.
    h1_close = _series(h1, "ts") + HTF_CLOSE_MS["h1"]
    h4_close = _series(h4, "ts") + HTF_CLOSE_MS["h4"]
    d1_close = _series(d1, "ts") + HTF_CLOSE_MS["d1"]

    filt = HTFFilter(None, cfg)
    if signal_fn is None:
        def signal_fn(win, ctx, f):
            return f.trend_signal(win)

    # Caches: analyse_timeframe / POI depend only on the latest closed bar index
    # per timeframe, so they change at most once per new HTF bar, not per M15.
    ca_d: dict = {}
    ca_4: dict = {}
    ca_1: dict = {}
    ca_poi: dict = {}

    def assemble_ctx(kd, k4, k1, sd, s4, s1, ref):
        d = ca_d.get(kd)
        if d is None and len(sd) >= 20:
            d = analyse_timeframe(sd, "D1"); ca_d[kd] = d
        a4 = ca_4.get(k4)
        if a4 is None and len(s4) >= 20:
            a4 = analyse_timeframe(s4, "H4"); ca_4[k4] = a4
        a1 = ca_1.get(k1)
        if a1 is None and len(s1) >= 20:
            a1 = analyse_timeframe(s1, "H1"); ca_1[k1] = a1
        c = MarketContext()
        c.daily, c.h4, c.h1 = d, a4, a1
        bias = 0.0; tw = 0.0
        for tf_a, w in [(d, cfg.weight_d1), (a4, cfg.weight_h4), (a1, cfg.weight_h1)]:
            if tf_a is None:
                continue
            bias += _TREND_SCORE[tf_a.trend] * tf_a.strength * w; tw += w
        c.bias = bias / tw if tw > 0 else 0.0
        poi = ca_poi.get(k4)
        if poi is None:
            poi = detect_h4_poi(s4); ca_poi[k4] = poi
        c.h4_poi = poi
        c.h1_confirmed_side = h1_confirm_side(c.h1, cfg)
        c.market_stage, c.stage_side, c.stage_reason = _detect_stage(c, ref, cfg)
        if c.market_stage in {MarketStage.EARLY_TREND, MarketStage.EXPANSION} and c.stage_side:
            c.allowed = {c.stage_side}
        elif c.market_stage == MarketStage.CONFLICT:
            c.allowed = set()
        elif c.bias > cfg.allowed_buy_bias:
            c.allowed = {"Buy"}
        elif c.bias < cfg.allowed_sell_bias:
            c.allowed = {"Sell"}
        else:
            c.allowed = {"Buy", "Sell"}
        return c

    trades: list[Trade] = []
    pos: Optional[dict] = None
    ctx = None
    last_h1_bucket = -1

    # Warmup: need CTX_HISTORY closed D1 bars and SIGNAL_WINDOW M15 bars.
    start = int(np.searchsorted(d1_close, m15_ts + M15_MS, side="right").min()) if False else 0
    # start when the M15 close time has >= CTX_HISTORY closed D1 bars behind it
    start = int(np.searchsorted(m15_ts, d1_close[CTX_HISTORY - 1] - M15_MS, side="left"))
    start = max(start, SIGNAL_WINDOW)

    fee_per_side = cfg.taker_fee_per_side + cfg.half_spread_frac + cfg.slippage_frac

    def close_pos(exit_price: float, exit_ts: int, reason: str) -> None:
        nonlocal pos
        d = (exit_price - pos["entry"]) if pos["side"] == "Buy" else (pos["entry"] - exit_price)
        gross = d * pos["qty"]
        fees = fee_per_side * pos["qty"] * (pos["entry"] + exit_price)
        net = gross - fees
        trades.append(Trade(
            entry_ts=pos["entry_ts"], exit_ts=exit_ts, side=pos["side"],
            entry=pos["entry"], exit=exit_price, qty=pos["qty"],
            risk_usdt=pos["risk_usdt"], gross_pnl=gross, fees=fees, net_pnl=net,
            r_net=net / pos["risk_usdt"] if pos["risk_usdt"] > 0 else 0.0,
            reason=reason, year=_year(exit_ts),
        ))
        pos = None

    def try_open(side: str, bar: dict, win: list[dict]) -> None:
        nonlocal pos
        entry = bar["close"]
        sl, tp, qty = calc_sl_tp_qty(entry, side, win, cfg)
        if qty <= 0:
            return
        risk_usdt = abs(entry - sl) * qty
        pos = dict(side=side, entry=entry, sl=sl, tp=tp, qty=qty,
                   entry_ts=bar["ts"], entry_i=None, risk_usdt=risk_usdt)

    for i in range(start, len(m15)):
        bar = m15[i]
        t_close = int(m15_ts[i]) + M15_MS

        # 1. manage open position on THIS bar (only bars strictly after entry)
        if pos is not None and bar["ts"] > pos["entry_ts"]:
            hi, lo = bar["high"], bar["low"]
            if pos["side"] == "Buy":
                hit_sl = lo <= pos["sl"]
                hit_tp = hi >= pos["tp"]
                if hit_sl:
                    close_pos(pos["sl"], bar["ts"], "sl")
                elif hit_tp:
                    close_pos(pos["tp"], bar["ts"], "tp")
            else:
                hit_sl = hi >= pos["sl"]
                hit_tp = lo <= pos["tp"]
                if hit_sl:
                    close_pos(pos["sl"], bar["ts"], "sl")
                elif hit_tp:
                    close_pos(pos["tp"], bar["ts"], "tp")

        # 2. refresh HTF context on a new H1 bucket (matches live cadence)
        bucket = t_close // H1_MS
        if bucket != last_h1_bucket:
            last_h1_bucket = bucket
            kd = int(np.searchsorted(d1_close, t_close, side="right"))
            k4 = int(np.searchsorted(h4_close, t_close, side="right"))
            k1 = int(np.searchsorted(h1_close, t_close, side="right"))
            sd = d1[max(0, kd - CTX_HISTORY):kd]
            s4 = h4[max(0, k4 - CTX_HISTORY):k4]
            s1 = h1[max(0, k1 - CTX_HISTORY):k1]
            if len(sd) >= 20 and len(s4) >= 20 and len(s1) >= 20:
                ctx = assemble_ctx(kd, k4, k1, sd, s4, s1, bar["close"])
                filt.update(ctx)
            else:
                ctx = None
                filt.update(None)

        if ctx is None:
            continue

        # 3. signal on the last SIGNAL_WINDOW closed M15 bars
        win = m15[i - SIGNAL_WINDOW + 1:i + 1]
        side, _conf = signal_fn(win, ctx, filt)
        if side is None:
            continue

        # 4. act
        if pos is None:
            if filt.allows(side, bar["close"]):
                try_open(side, bar, win)
        elif side != pos["side"] and cfg.allow_reverse and filt.allows(side, bar["close"]):
            close_pos(bar["close"], bar["ts"], "reverse")
            try_open(side, bar, win)

    return trades


def _year(ts_ms: int) -> int:
    # epoch ms -> UTC year without datetime (avoids Date.now-style restrictions)
    days = ts_ms // 86_400_000
    y = 1970
    while True:
        leap = (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)
        dy = 366 if leap else 365
        if days < dy:
            return y
        days -= dy
        y += 1
