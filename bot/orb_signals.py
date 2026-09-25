"""S021 (ORB) live decision logic and the runner-facing cycle wrapper.

decide() is a pure function over already-fetched state (no broker I/O of its
own -- CTraderORB.run_live_cycle_orb fetches m1/m15/positions/orders/balance
once per cycle and hands them in, same contract as
bot.ctrader_s007.CTraderS007.run_live_cycle). It closes over `logger`/`cid`
from run_cycle_for_account below, exactly the way bot.s007_paper's own
decide() does -- see that module for the precedent.

Unlike S007's continuous per-minute signal scan, S021's whole day boils down
to a handful of states, checked fresh every cycle from the broker's OWN
reported positions/orders (mostly stateless, like S007) plus
StrategyLogger's append-only per-label log for the two things the broker
snapshot alone can't tell you: "did we already tell the DB about this fill"
and "did today's trade already fully resolve" (see the state list below,
case 6) -- exactly the two guard rails bot.s007_paper.py::decide needed
live (label_was_opened / label_was_closed, decisions-log.md 2026-07-21 and
2026-07-30):

  1. No valid 09:30 (fixed-EST) anchor yet, or ADR14 not computable (not
     enough valid prior sessions) -> nothing to do. The second sub-case is
     logged separately, at WARNING ("insufficient_adr_history"), so it is
     never confused with the normal "no anchor yet" case -- it is exactly
     the failure mode the 2026-09-22 M1->M15 history fix addressed (see
     bot/ctrader_orb.py's module docstring) and could recur if the broker's
     bar cap shrinks further or a data gap eats into the M15 history.
  2. Anchor + ADR14 available, entry window open (09:30 <= now <= 14:29
     fixed-EST), no pending orders, no position, today not already resolved
     -> place BOTH resting STOP orders (buy-stop at U, sell-stop at L),
     each with its own SL. No take-profit (sec 3/10).
  3. One of today's orders has become a broker position -> cancel the
     sibling order if still pending, and log the fill (once).
  4. Time is at or past 15:59 (fixed-EST) and a position for today is still
     open -> close it at market ("time" exit -- S021's only other exit is
     the stop).
  5. Entry window closed (now > 14:29) and neither order filled -> cancel
     both (day skipped, mirrors strategies/orb_intraday/engine.py's
     simulate(), which only scans entries through cfg.entry_cutoff).
  6. Neither a position nor a pending order exists, but our own log shows a
     label opened today with no matching close yet -> the broker's attached
     SL must have fired between cycles (S021 places no TP, so the only two
     ways an order can vanish without us closing it are stop-out or
     broker-side expiry) -- backfill the close (reason="stop_broker_side")
     and do NOT place a fresh entry this cycle. Same "backfill" reasoning
     as bot/s007_paper.py::decide (found live there 2026-07-30) -- without
     it, a stop-out would be invisible to this log and a later cycle would
     re-enter the same day, which sec 3 forbids ("без повторных входов
     после стопа").

A seventh, defensive case: BOTH of today's labels show an open position at
once -- a same-tick double-fill the resting-order model can't rule out the
way the backtest's bar-close "both boundaries touched -> skip the day" guard
does (see bot/ctrader_orb.py's module docstring). Flatten both immediately
and log loudly. Never observed live; S021 has no live history yet.
"""
from __future__ import annotations

import logging

import pandas as pd

from bot import orb_config as C
from bot.risk import lots_for_risk
from strategies.orb_intraday.engine import compute_adr14, compute_daily_sessions


def _today_label(magic: str, day: pd.Timestamp, side: str) -> str:
    return f"{magic}:{day.date().isoformat()}:{side}"


def _m15_min_session_bars(cfg) -> int:
    """M15-bar-count day-validity threshold, scaled from cfg.min_session_bars
    (which strategies/orb_intraday/config.py documents as calibrated for M1
    bars) to M15, at the same fractional session coverage. Used ONLY for the
    live M15 history fetch (see bot/ctrader_orb.py::_get_m15_step) -- the
    frozen cfg.min_session_bars value itself, and the backtest engine that
    reads it, are untouched.

    Derived from cfg's own session times and min_session_bars rather than
    hardcoded, so it can never silently drift out of sync with the frozen
    config. For ORB_BASE: 09:30-15:59 inclusive = 390 M1 bars = 26 M15 bars,
    and 350 * 26 / 390 = 23.33 -> 24 M15 bars.

    Rounded UP (ceil), not to nearest, on purpose: a day with n M1 bars
    in-session populates at least ceil(n / 15) M15 bars, so ceil makes every
    day the M1 rule accepts also pass here (never stricter than the
    backtest), while staying as tight as possible. Round-to-nearest (23)
    was measured on the full NSXUSD histdata set (2019-01..2026-06) to
    wrongly admit 126 days the backtest rejects -- sessions cut ~45 minutes
    short (~345 M1 bars, exactly 23 M15 bars), clustered in DST-transition
    weeks -- which shifted ADR14 by up to ~190 pts on affected days. With
    ceil the only remaining disagreement on that dataset is a single day
    whose exact 09:30 M1 bar is missing (2026-04-28), which the M1 rule
    rejects and M15 bars cannot see.
    """
    session_open_min = cfg.session_open.hour * 60 + cfg.session_open.minute
    session_close_min = cfg.session_close.hour * 60 + cfg.session_close.minute
    session_minutes = session_close_min - session_open_min + 1  # inclusive of both endpoints
    m1_bars_full_session = session_minutes                       # one M1 bar per minute
    m15_bars_full_session = session_minutes // 15
    # integer ceil(min_session_bars * m15 / m1), no float rounding involved
    return -(-cfg.min_session_bars * m15_bars_full_session // m1_bars_full_session)


def _today_anchor(m1_today: pd.DataFrame, cfg) -> tuple[pd.Timestamp, float] | None:
    """(today, open_price) from the session-open (09:30 fixed-EST) bar of the
    LATEST calendar day present in m1_today, or None if that day has no bar
    exactly at cfg.session_open (bot started after the open, weekend/holiday,
    or a data gap).

    Factored out of _levels_for_today so decide()'s logging can tell "no
    anchor yet" (normal) apart from "anchor found, but ADR14 history is
    short" (the "insufficient_adr_history" WARNING event in
    run_cycle_for_account's decide())."""
    if m1_today.empty:
        return None
    dates = m1_today.index.normalize()
    today = dates[-1]
    today_bars = m1_today.loc[dates == today]
    open_ts = pd.Timestamp.combine(today.date(), cfg.session_open)
    if open_ts not in today_bars.index:
        return None
    return today, float(today_bars.loc[open_ts, "open"])


def _valid_prior_sessions(m15_hist: pd.DataFrame, today: pd.Timestamp, cfg) -> pd.DataFrame:
    """Valid prior trading-day sessions (STRICTLY before `today` -- the
    look-ahead guard lives here, so callers may pass an M15 frame that also
    contains today's or even later bars) reconstructed from M15 history via
    strategies/orb_intraday/engine.py::compute_daily_sessions, with the day-
    validity threshold scaled to M15 by _m15_min_session_bars (the frozen
    cfg itself is never modified -- a local .with_() copy is used).

    This is what ADR14 is computed from. Also used standalone by decide()'s
    logging, so it can report how many valid sessions were found even when
    that is fewer than cfg.adr_window (the "insufficient_adr_history" event).
    """
    if m15_hist.empty:
        return pd.DataFrame()
    hist_dates = m15_hist.index.normalize()
    hist = m15_hist.loc[hist_dates < today]
    if hist.empty:
        return pd.DataFrame()
    hist_cfg = cfg.with_(min_session_bars=_m15_min_session_bars(cfg))
    return compute_daily_sessions(hist, hist_cfg)


def _levels_for_today(m1_today: pd.DataFrame, m15_hist: pd.DataFrame, cfg) -> dict | None:
    """Today's O/U/L/ADR14/stop distance, reusing strategies/orb_intraday/
    engine.py's compute_daily_sessions/compute_adr14 -- the exact functions
    the independently-verified backtest uses -- rather than a second
    implementation of the same math that could silently drift from it.

    Takes two separate frames (until 2026-09-22 this was one M1 frame
    covering both): `m1_today`, an M1 window that only needs to contain
    today's session-open bar (O comes from it, see _today_anchor), and
    `m15_hist`, the M15 window ADR14's history is reconstructed from (see
    _valid_prior_sessions; why M15 at all: bot/ctrader_orb.py::
    _get_m15_step). The M15-sourced session_range of a day is EXACT, not
    approximate: the 09:30-15:59 session is 26 whole M15 bars aligned to
    both session boundaries, and max-of-highs / min-of-lows does not depend
    on how the in-session M1 bars are grouped. The one approximation is the
    day-validity rule (which days count toward ADR14): M1 bar counts and
    the exact-09:30-minute bar are not observable in M15 bars, so it uses
    the never-stricter M15 threshold from _m15_min_session_bars -- see that
    docstring for the measured residual difference on real data.

    compute_daily_sessions/compute_adr14 were written for a full historical
    dataset, where "today" already has a complete session's worth of bars.
    Live, "today" is always a PARTIAL day (however far the session has
    gotten) -- calling them on raw live bars as-is would make
    compute_daily_sessions drop today entirely (< min_session_bars) and
    there would be no ADR14 for it at all. Fix: compute daily sessions from
    history STRICTLY BEFORE today (all complete days), then append a
    synthetic "today" row carrying only session_open (all compute_adr14
    needs from the anchor row's own line -- its causal loop only ever reads
    the window strictly BEFORE index i, never row i's own range) and run
    compute_adr14 on that extended frame. This gives exactly the ADR14 the
    backtest would have computed for today, without duplicating the
    ADR/gap-guard math here.
    """
    anchor = _today_anchor(m1_today, cfg)
    if anchor is None:
        return None  # bot started after the open, or a data gap -- no valid anchor today
    today, O = anchor

    daily_hist = _valid_prior_sessions(m15_hist, today, cfg)
    if daily_hist.empty:
        return None  # no valid prior sessions at all yet -- ADR14 impossible
    # n_bars is never read by compute_adr14 (it only reads session_range and
    # the index dates), so 0 is just a placeholder for the synthetic row.
    today_row = pd.DataFrame(
        {"session_open": [O], "session_high": [float("nan")],
         "session_low": [float("nan")], "session_range": [float("nan")],
         "n_bars": [0]}, index=[today])
    daily_ext = pd.concat([daily_hist, today_row])
    adr14 = compute_adr14(daily_ext, cfg)
    adr = adr14.iloc[-1]
    if pd.isna(adr):
        return None  # not enough valid prior sessions yet (< adr_window, or gap guard)

    stop_dist = cfg.stop_adr_mult * adr
    return dict(day=today, O=O, U=O + cfg.k_range * adr, L=O - cfg.k_range * adr,
               adr=float(adr), stop_dist=float(stop_dist))


def run_cycle_for_account(creds: dict | None, *, logger, symbol_candidates=None,
                          history_days: int | None = None, today_days: int | None = None,
                          risk_pct: float | None = None,
                          fixed_lot: float | None = None, use_fixed_lot: bool | None = None,
                          magic: str = C.MAGIC, broker: str = "off",
                          allow_mainnet: bool = False, env: str | None = None) -> dict:
    """One S021 cycle for an arbitrary account -- mirrors
    bot.s007_paper.run_cycle_for_account's shape/contract (same creds shape,
    same kind of return) so webapp/runner.py can drive both the same way.

    `broker` ("off"/"dry"/"execute", default "off") gates NEW entries
    (case 2 in decide() below) exactly like bot/s011_paper.py's own
    `broker` param: "off" places nothing, "dry" computes+logs the intended
    place_stop orders (logger.order(..., result="dry-run")) but sends
    neither, "execute" sends them for real. `allow_mainnet` + `env` mirror
    _worker_s011's double-gate (see that worker's docstring) -- ADDED
    2026-09-22 (ALGODEV-46 follow-up): until this change, S021 had no gate
    at all -- _worker_orb never read AccountStrategy.broker_mode and this
    function unconditionally placed real orders once a signal fired,
    silently NOT matching the "off" default its own DB row already carried
    (confirmed via live logs: no real order had fired yet today when this
    was found, so no live-money exposure resulted, but the gate was pure
    fiction until now). Deliberately narrower than S011's gate: only NEW
    risk (case 2) is broker-mode-checked -- cases 1/3/4/5/6/7 below all
    REACT to a position/order that is already real at the broker (only
    possible once case 2 has actually placed one under "execute"), so they
    always run regardless of `broker`, on purpose: a stray real position
    must still be managed/closed even if `broker` is later turned back to
    "off", never abandoned.

    `history_days` (default C.HISTORY_DAYS) is the calendar-day window of
    the M15 fetch ADR14's history is reconstructed from; `today_days`
    (default C.TODAY_M1_DAYS) is the small M1 window that only has to
    contain today's anchor bar and the current minute. Until 2026-09-22
    `history_days` was an M1 window covering both -- see bot/ctrader_orb.py's
    module docstring for why that never worked live (broker bar cap).

    Returns dict(cycle_id, actions, error) -- actions is a list of
    {kind: "open", label, side, entry, sl, tp, is_add, volume_lots} (from a
    detected resting-order fill) or {kind: "close", label, reason} --
    exactly the two shapes webapp/runner.py::_worker_s007's DB-writing loop
    already understands (_open_position_db/_close_position_db), so
    _worker_orb reuses that loop unchanged. "place_stop"/"cancel_order" are
    logged (logger.order) but never reach `actions` -- a resting order is
    not a DB Position row, only a filled one is.
    """
    from bot.ctrader_orb import CTraderORB

    symbol_candidates = symbol_candidates or C.SYMBOL_CANDIDATES
    history_days = history_days or C.HISTORY_DAYS
    today_days = today_days or C.TODAY_M1_DAYS
    risk_pct = C.RISK_PCT if risk_pct is None else risk_pct
    fixed_lot = C.FIXED_LOT if fixed_lot is None else fixed_lot
    use_fixed_lot = C.USE_FIXED_LOT if use_fixed_lot is None else use_fixed_lot

    if broker == "execute" and allow_mainnet and env != "live":
        return dict(cycle_id=None, actions=[], error=(
            f"--allow-mainnet given but env={env!r} != 'live' -- refusing (see "
            f"bot/s011_paper.py's env-gate incident this double-gate mirrors)"))

    cid = logger.cycle_start(mode="live" if broker == "execute" else
                             ("dry" if broker == "dry" else "shadow"))
    actions_taken: list[dict] = []

    def decide(symbol, m1, m15, positions, orders, balance, money_per_point_per_lot):
        cfg = C.STRATEGY
        levels = _levels_for_today(m1, m15, cfg)
        # Tell "no anchor yet" (normal: weekend, bot started late) apart from
        # "anchor found but ADR14 history is short" (the bug class the
        # M1->M15 history fetch fixed) -- both used to collapse silently
        # into has_levels=False.
        diag_fields: dict = {}
        if levels is None:
            anchor = _today_anchor(m1, cfg)
            if anchor is not None:
                today, _open_price = anchor
                daily_hist = _valid_prior_sessions(m15, today, cfg)
                diag_fields = dict(sessions_available=len(daily_hist),
                                   adr_window=cfg.adr_window)
                if len(daily_hist) < cfg.adr_window:
                    logger.event(
                        "insufficient_adr_history", cycle=cid, level=logging.WARNING,
                        symbol=symbol, **diag_fields,
                        text=(f"today's session-open anchor is valid but only "
                              f"{len(daily_hist)} prior M15-reconstructed sessions were "
                              f"found (need >= {cfg.adr_window}) -- ADR14 not computable "
                              f"this cycle"))
        logger.event("state", cycle=cid, symbol=symbol,
                     last_bar=str(m1.index[-1]) if len(m1) else None,
                     has_levels=levels is not None,
                     n_positions=len(positions), n_orders=len(orders), **diag_fields)
        if levels is None:
            return []

        day, U, L, stop_dist = levels["day"], levels["U"], levels["L"], levels["stop_dist"]
        now_t = m1.index[-1].time()
        long_label = _today_label(magic, day, "long")
        short_label = _today_label(magic, day, "short")

        have = {p["label"]: p for p in positions if p["label"] in (long_label, short_label)}
        pending = {o["label"]: o for o in orders if o["label"] in (long_label, short_label)}

        # --- case 7: both legs filled (execution anomaly) -- flatten both ---
        if len(have) == 2:
            logger.error(f"both S021 legs filled today ({long_label} and {short_label}) "
                        f"-- execution anomaly, flattening both", cycle=cid)
            out = []
            for lab, p in have.items():
                side = "buy" if lab == long_label else "sell"
                if not logger.label_was_opened(lab):
                    logger.position(lab, "open", cycle=cid, side=side, entry=p["price"],
                                    sl=p["stop_loss"], tp=None, is_add=False,
                                    position_id=p["position_id"])
                    actions_taken.append(dict(kind="open", label=lab, side=side,
                                              entry=p["price"], sl=p["stop_loss"], tp=None,
                                              is_add=False, volume_lots=None))
                out.append(dict(kind="close", label=lab, position_id=p["position_id"],
                                volume=p["volume"], reason="both_filled_anomaly"))
            return out

        # --- case 3/4: one leg filled -- cancel sibling, log fill, time-exit ---
        if have:
            lab, p = next(iter(have.items()))
            sibling = short_label if lab == long_label else long_label
            out = []
            if sibling in pending:
                out.append(dict(kind="cancel_order", label=sibling,
                                order_id=pending[sibling]["order_id"]))
                logger.event("cancel_sibling", cycle=cid, label=sibling,
                             reason="opposite_leg_filled")
            if not logger.label_was_opened(lab):
                side = "buy" if lab == long_label else "sell"
                lots = (p["volume"] / (100.0 * money_per_point_per_lot)
                       if money_per_point_per_lot else None)
                logger.position(lab, "open", cycle=cid, side=side, entry=p["price"],
                                sl=p["stop_loss"], tp=None, is_add=False,
                                volume_lots=lots, position_id=p["position_id"])
                actions_taken.append(dict(kind="open", label=lab, side=side,
                                          entry=p["price"], sl=p["stop_loss"], tp=None,
                                          is_add=False, volume_lots=lots))
            # LIVE_EXIT_BUFFER_MIN (see bot/orb_config.py's own comment): the
            # live time-exit fires this many minutes before cfg.session_close
            # itself, purely so the cron window has slack left to catch and
            # execute it -- cfg.session_close is untouched (still read by the
            # backtest engine and the ADR-session-validity check above).
            live_close_min = (cfg.session_close.hour * 60 + cfg.session_close.minute
                              - C.LIVE_EXIT_BUFFER_MIN)
            now_min = now_t.hour * 60 + now_t.minute
            if now_min >= live_close_min:
                out.append(dict(kind="close", label=lab, position_id=p["position_id"],
                                volume=p["volume"], reason="time"))
            return out

        # --- case 5: pending, unfilled -- wait, or cancel at the cutoff ---
        if pending:
            out = []
            if now_t > cfg.entry_cutoff:
                for lab, o in pending.items():
                    out.append(dict(kind="cancel_order", label=lab, order_id=o["order_id"]))
                    logger.event("cancel_unfilled", cycle=cid, label=lab,
                                 reason="entry_cutoff_passed")
            return out

        # --- nothing open, nothing pending: case 6 (backfill a broker-side
        # stop) takes priority over case 2 (fresh entry) ---
        for lab in (long_label, short_label):
            if logger.label_was_opened(lab) and not logger.label_was_closed(lab):
                logger.position(lab, "close", cycle=cid, reason="stop_broker_side")
                actions_taken.append(dict(kind="close", label=lab, reason="stop_broker_side"))
                return []  # today is done; re-evaluate fresh next cycle

        if logger.label_was_closed(long_label) or logger.label_was_closed(short_label):
            return []  # today already resolved (stop, cutoff-cancel, or time-exit)

        if cfg.session_open <= now_t <= cfg.entry_cutoff:
            risk_amount = balance * risk_pct / 100.0
            lot = (fixed_lot if use_fixed_lot else
                  lots_for_risk(risk_amount, stop_dist, money_per_point_per_lot, min_lot=fixed_lot))
            logger.event("size", cycle=cid, long_label=long_label, short_label=short_label,
                         U=U, L=L, stop_dist=stop_dist, lot=lot, risk_amount=risk_amount)
            entry_orders = [
                dict(kind="place_stop", label=long_label, side="buy",
                    stop=U, sl=U - stop_dist, volume_lots=lot),
                dict(kind="place_stop", label=short_label, side="sell",
                    stop=L, sl=L + stop_dist, volume_lots=lot),
            ]
            # broker-mode gate: only NEW risk (this entry) is gated -- see
            # this function's docstring for why cases 1/3/4/5/6/7 above are
            # never gated.
            if broker == "off":
                return []
            if broker == "dry":
                for a in entry_orders:
                    req = dict(side=a["side"], stop=a["stop"], sl=a["sl"], lot=a["volume_lots"])
                    logger.order(a["label"], "place_stop", cycle=cid, request=req,
                                result="dry-run")
                return []
            return entry_orders  # broker == "execute"
        return []

    # CTraderORB() is constructed OUTSIDE the try below, deliberately -- same
    # reasoning as bot/s007_paper.py's own CTraderS007() construction: a
    # malformed credential/config must crash this process loudly, not get
    # swallowed and silently re-logged every minute forever (decisions-log.md
    # 2026-07-29).
    api = CTraderORB(creds=creds)
    error = None
    try:
        cyc = api.run_live_cycle_orb(symbol_candidates, history_days, today_days, decide)
        for r in cyc["results"]:
            a, err, res_ = r["action"], r["error"], r["result"]
            lab = a["label"]
            if a["kind"] == "place_stop":
                req = dict(side=a["side"], stop=a["stop"], sl=a["sl"], lot=a["volume_lots"])
                logger.order(lab, "place_stop", cycle=cid, request=req, result=res_, error=err)
            elif a["kind"] == "cancel_order":
                req = dict(order_id=a["order_id"])
                logger.order(lab, "cancel_order", cycle=cid, request=req, result=res_, error=err)
            elif a["kind"] == "close":
                req = dict(position_id=a.get("position_id"))
                if err is not None:
                    logger.order(lab, "close_position", cycle=cid, request=req, error=err)
                    continue
                logger.order(lab, "close_position", cycle=cid, request=req, result=res_)
                logger.position(lab, "close", cycle=cid, reason=a["reason"])
                actions_taken.append(dict(kind="close", label=lab, reason=a["reason"]))
    except Exception as e:
        logger.error("live cycle failed", exc=e, cycle=cid)
        error = repr(e)[:500]
    logger.cycle_end(cid, actions=len(actions_taken))
    return dict(cycle_id=cid, actions=actions_taken, error=error)
