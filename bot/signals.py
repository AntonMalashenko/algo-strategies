"""Signal layer: rebuild engine state and derive the desired order set.

Uses run_backtest(return_state=True) on recent M15 history, so the live
zone lifecycle is BY CONSTRUCTION identical to the validated backtest —
no re-implementation of rules.
"""
from __future__ import annotations

import pandas as pd

from strategies.fvg_mtf import run_backtest, BUFFER_PIPS
from bot import config as C


def desired_orders(m15: pd.DataFrame, symbol: str, now=None) -> dict:
    """Return the target broker state for one symbol.

    Output dict:
      orders    -- list of limit orders to keep active RIGHT NOW
                   (empty outside the Asia window): each has side, price,
                   sl, tp, zone_id (prices in raw points, caller converts)
      position  -- engine's open position (if any) for reconciliation
      in_window -- whether the Asia window is currently open
    """
    now = now or m15.index[-1]
    trades, state = run_backtest(
        m15, mode=C.MODE, stop=C.STOP, rr=C.RR,
        pip=C.PIP_RAW, spread_pips=C.SPREAD_PIPS, return_state=True)

    in_window = C.ASIA_START_H <= now.hour < C.ASIA_END_H
    buf = BUFFER_PIPS * C.PIP_RAW
    px = float(m15["close"].iloc[-1])
    candidates = []
    if in_window and state["pos"] is None:
        for z in state["zones"]:
            d = z["dir"]
            near = z["top"] if d == 1 else z["bot"]
            # a resting limit must sit on the passive side of current price
            if (d == 1 and near >= px) or (d == -1 and near <= px):
                continue
            sl = (z["bot"] - buf) if d == 1 else (z["top"] + buf)
            risk = (near - sl) * d
            if risk < 2 * C.PIP_RAW:          # gap-guard, passport §3
                continue
            tp = near + d * C.RR * risk
            candidates.append(dict(
                symbol=symbol, side="buy" if d == 1 else "sell",
                type="limit", price=near, sl=sl, tp=tp,
                zone_id=f"{symbol}:{z['avail'].isoformat()}",
                risk_points=risk, dist=abs(px - near),
            ))
    # engine semantics = FIRST touch wins, one position per symbol:
    # keep only the NEAREST zone per direction; the reconcile loop
    # re-evaluates after every fill/expiry.
    orders = []
    for side in ("buy", "sell"):
        group = [c for c in candidates if c["side"] == side]
        if group:
            orders.append(min(group, key=lambda c: c["dist"]))
    return dict(orders=orders, position=state["pos"],
                in_window=in_window, n_trades_hist=len(trades))
