"""Equal-dollar-risk position sizing for lot/point-based instruments (cTrader
CFD/FX). Shared by every strategy that trades through `bot.ctrader.CTraderAdapter`
subclasses (S007 today; S009 later) so a second implementation doesn't drift
from this one.

Pure math, no broker I/O: the caller fetches `balance` (ProtoOATraderReq) and
`money_per_point_per_lot` (from the instrument's own contract metadata) from
the broker once per session and passes them in -- see
`CTraderAdapter._get_balance_step` / `CTraderS007._get_full_symbol_step` and
`CTraderS007.run_live_cycle`.
"""
from __future__ import annotations

# Below this stop distance a position is treated as having "no real stop"
# (bad/stale data, or the same near-zero-risk artifact the S007 engine's
# min-risk guard already filters in backtest) -- sizing off of it would
# divide by ~0 and produce an absurd lot size, so we floor to min_lot instead.
MIN_STOP_POINTS = 0.5


def lots_for_risk(risk_amount: float, stop_distance_points: float,
                   money_per_point_per_lot: float, min_lot: float) -> float:
    """Lot size that risks ~`risk_amount` (account currency) if price moves
    `stop_distance_points` against the position, given the instrument's
    `money_per_point_per_lot` (account-currency P&L per 1.0 point at 1.0 lot,
    from the broker's own symbol metadata for this session).

    Returns `min_lot` -- never smaller -- whenever the risk-sized volume
    would be smaller than the broker's minimum, or the inputs are degenerate
    (near-zero stop distance, non-positive money_per_point_per_lot or
    risk_amount): "0.25% risk, or the minimum lot if that doesn't fit."
    Broker-side clamping to [minVolume, maxVolume] / stepVolume still happens
    downstream in `CTraderS007._volume_from_lots` when the order is placed.
    """
    if (stop_distance_points is None or stop_distance_points < MIN_STOP_POINTS
            or not money_per_point_per_lot or money_per_point_per_lot <= 0
            or not risk_amount or risk_amount <= 0):
        return min_lot
    lots = risk_amount / (stop_distance_points * money_per_point_per_lot)
    return max(lots, min_lot)
