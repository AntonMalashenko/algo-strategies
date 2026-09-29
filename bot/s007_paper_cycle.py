"""One S007 paper-trading cycle (ALGODEV-45 step 3).

Ties the two halves together: `bot.s007_paper.make_decide()` -- the SAME
decision closure the live, order-placing path runs -- and `PaperBook`, which
stands in for the broker. Nothing about the trading rules lives here; this
module only routes the actions `decide()` returns into the virtual book
instead of onto the wire, and logs the result.

SAFETY INVARIANT: this module imports no order-sending code and holds no
broker handle. The only broker object in the paper daemon is used for
READING market data; every write goes to `PaperBook`. `decide()` itself
cannot reach the network either (it is documented as a pure step), so there
is no path from a paper cycle to a real order.

WHAT IS DELIBERATELY NOT MODELLED
Fills are booked at the engine's planned entry, with no slippage. That is a
known optimism, and it is the honest default: the live slippage distribution
is not stationary (2026-09-25's trade :48 filled 23 points off plan and cost
-$87.70 on its own), so inventing a number here would be a fabricated result
dressed up as a measurement. Paper P&L is therefore an UPPER bound, and the
gap between paper and live fills is itself one of the things this daemon
exists to measure.
"""
from __future__ import annotations

from utils.trade_logger import StrategyLogger

from bot import s007_config as C
from bot.s007_paper import make_decide
from bot.s007_paper_book import PaperBook


def run_paper_cycle(book: PaperBook, *, symbol: str, m1, logger: StrategyLogger,
                    preset: str, money_per_point_per_lot: float,
                    magic: str = C.MAGIC,
                    risk_pct: float | None = None,
                    fixed_lot: float | None = None,
                    use_fixed_lot: bool | None = None,
                    daily_risk_cap_pct: float | None = None,
                    fx_rate: float | None = None,
                    initial_balance: float | None = None,
                    broker_min_lot: float | None = None) -> dict:
    """Run one full paper cycle against `book` and return a summary dict.

    `money_per_point_per_lot` is the RAW, quote-currency value the broker
    reports (`full_symbol.lotSize`) -- NOT pre-multiplied by `fx_rate`, because
    `decide()` applies that conversion itself on the way in, exactly as it does
    for the live path. Passing an already-converted number here would apply the
    FX rate twice and inflate every risk figure by ~14%. The book's own
    `money_per_point_per_lot` is the opposite convention (already converted,
    because it books P&L onto an account-currency balance) -- hence no default
    from the book: the two are NOT interchangeable and a silent fallback would
    pick the wrong one.
    """
    risk_pct = C.RISK_PCT if risk_pct is None else risk_pct
    fixed_lot = C.FIXED_LOT if fixed_lot is None else fixed_lot
    use_fixed_lot = C.USE_FIXED_LOT if use_fixed_lot is None else use_fixed_lot
    daily_risk_cap_pct = (C.DAILY_RISK_CAP_PCT if daily_risk_cap_pct is None
                          else daily_risk_cap_pct)
    fx_rate = C.EUR_TO_USD_FX_RATE_APPROX if fx_rate is None else fx_rate

    cid = logger.cycle_start(mode="paper", preset=preset)
    status_info: dict = {}

    # 1. Broker-side settlement first: honour server-side SL/TP on the bars
    # that arrived since the last tick, so `decide()` sees a settled book --
    # the same state a real reconcile would have returned.
    for rec in book.mark_fills(m1):
        logger.position(rec["label"], "close", cycle=cid, reason=rec["reason"],
                        exit_price=rec["exit_price"], pnl=rec["pnl"],
                        balance_after=rec["balance_after"])

    # 2. The shared decision step -- byte-identical to the live path.
    decide = make_decide(
        preset=preset, magic=magic, risk_pct=risk_pct, fixed_lot=fixed_lot,
        use_fixed_lot=use_fixed_lot, daily_risk_cap_pct=daily_risk_cap_pct,
        fx_rate=fx_rate, initial_balance=initial_balance, logger=logger,
        stop_flag_active=lambda: False, cid=cid, status_info=status_info)

    actions = decide(symbol, m1, book.as_broker_positions(), book.balance,
                     money_per_point_per_lot, closed_deals=_as_closed_deals(book),
                     broker_min_lot=broker_min_lot)

    # 3. Apply to the virtual book instead of sending anything.
    last_bar_ts = m1.index[-1] if len(m1) else None
    last_close = float(m1["close"].iloc[-1]) if len(m1) else None
    applied: list[dict] = []
    for a in actions:
        kind = a["kind"]
        label = a["label"]
        if kind == "place":
            pos = book.place(label=label, side=a["side"], fill=float(a["entry"]),
                             sl=float(a["sl"]), tp=a.get("tp"),
                             volume_lots=a["volume_lots"], opened_ts=last_bar_ts)
            logger.position(label, "open", cycle=cid, side=a["side"],
                            entry=pos["price"], sl=pos["stop_loss"], tp=pos["take_profit"],
                            is_add=a["is_add"], volume_lots=a["volume_lots"],
                            position_id=pos["position_id"],
                            planned_entry=a["entry"], planned_sl=a["sl"])
            applied.append(dict(kind="open", label=label))
        elif kind == "amend":
            pos = book.amend(label, float(a["sl"]))
            if pos is None:
                # Stop was breached in step 1 of this same tick; the position
                # no longer exists. Not an error -- the identical race exists
                # against a real broker, which would reject the amend.
                logger.event("paper_amend_skipped", cycle=cid, label=label,
                             reason="position_already_closed")
                continue
            logger.position(label, "breakeven_moved", cycle=cid, sl=a["sl"],
                            prev_sl=a["prev_sl"], tp=a.get("tp"))
            applied.append(dict(kind="amend", label=label))
        else:
            rec = book.close(label, last_close, a.get("reason", "close"))
            if rec is None:
                continue
            logger.position(label, "close", cycle=cid, reason=rec["reason"],
                            exit_price=rec["exit_price"], pnl=rec["pnl"],
                            balance_after=rec["balance_after"])
            applied.append(dict(kind="close", label=label))

    logger.event("paper_state", cycle=cid, symbol=symbol,
                 last_bar=str(last_bar_ts) if last_bar_ts is not None else None,
                 balance=round(book.balance, 2), open_positions=len(book.positions),
                 closed_today=len(book.closed), **status_info)
    logger.cycle_end(cid, actions=len(applied))
    return dict(cycle_id=cid, actions=applied, balance=book.balance,
                open_positions=len(book.positions), **status_info)


def _as_closed_deals(book: PaperBook) -> list[dict]:
    """The book's own fills in the shape `decide()` expects from the broker's
    deal history: it matches these to its logged labels by `position_id` to
    value already-closed positions at their REAL fill in the daily risk budget
    (bot/s007_paper.py, the 2026-09-02 fix). Without this the paper book would
    fall back to planned entries and its risk accounting would drift from the
    live one for reasons that have nothing to do with the strategy."""
    return [dict(position_id=c["position_id"], entry_price=c["entry_price"],
                 exit_price=c["exit_price"], pnl=c["pnl"])
            for c in book.closed]
