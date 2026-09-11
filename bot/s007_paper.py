"""S007 paper/live runner (GER40 London x Frankfurt).

Dry-run works fully offline from a local M1 CSV and prints the desired position
set — use it to eyeball the bot before touching the broker:

    python -m bot.s007_paper --dry-run
    python -m bot.s007_paper --dry-run --at "2024-05-10 10:57"

Broker cycle (reuses the S004 cTrader connection / .env):

    python -m bot.s007_paper --accounts     # list account ids for the token
    python -m bot.s007_paper --check         # auth + balance + GER40 present
    python -m bot.s007_paper --live          # one reconcile cycle; schedule every 1 min

Manual kill-switch for the rest of today (e.g. news event, discretionary override):

    python -m bot.s007_paper --stop-today    # close everything, no new entries, until tomorrow
    python -m bot.s007_paper --resume-today  # cancel the stop early, same day

The bot is stateless: each cycle rebuilds the day's state from recent M1 bars via
the validated engine and reconciles the broker to it. The common 0.5 stop and the
day target are attached to each order (server-side); the bot only opens new
entries/adds and closes everything at the day's target or at 16:59.

`run_cycle_for_account()` below is the same decide()/reconcile logic, factored
out so webapp/runner.py's multi-account DB-driven runner can drive it for any
number of DB-registered accounts (each with its own creds/preset/risk/lot),
without re-implementing the trading rules a second time. `live()` (the CLI
above) is unchanged behaviour-wise: it just calls that function with the
module's own C.*/STOP_FLAG/LOG defaults, exactly as before this refactor.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from bot import s007_config as C
from bot.risk import lots_for_risk
from bot.s007_signals import plan_now
from utils.trade_logger import StrategyLogger

ROOT = Path(__file__).resolve().parent.parent
LOG = StrategyLogger("S007", log_root=str(ROOT / "reports" / "logs"))

# Manual stop flag: a file holding today's date, checked fresh every cycle
# (the bot is stateless -- see module docstring). If present AND its content
# is today's date, decide() treats the rest of today like `flat` (close
# everything, no new entries). A stale flag (yesterday's date left behind) is
# ignored automatically -- no manual cleanup needed, unlike a plain touch-file.
STOP_FLAG = ROOT / "reports" / "control" / "S007_STOP_TODAY"

# ALGODEV-41: how far the offset breakeven stop must stay on the safe side of
# the latest M1 bar. cTrader rejects an amend whose stopLoss sits on the wrong
# side of the current market outright (TRADING_BAD_STOPS -- seen live
# 2026-08-06 and 2026-08-31 on other wrong-side stop bugs), so the amend target
# fill +- breakeven_offset_points is capped at the last bar's low (buy) / high
# (sell) minus/plus this buffer. Points, GER40's own price units; ~1 round-trip
# spread of headroom against the bar-to-tick gap our 1-minute poll cannot see.
BREAKEVEN_AMEND_MARKET_BUFFER_POINTS = 1.5


def breakeven_stop_price(side: str, fill: float, offset_points: float,
                         m1: pd.DataFrame) -> float:
    """Where a live breakeven amend should put the stop (pure, no I/O).

    Base behaviour (offset_points == 0.0, every preset that doesn't set
    cfg.breakeven_offset_points): exactly the broker's own fill price, the
    frozen ALGODEV-37/39 result -- a BE exit then books the round-trip spread
    as a small loss.

    With an offset: fill + offset (buy) / fill - offset (sell), so a retrace
    onto the moved stop clears the spread instead. Two clamps, both one-sided:
      * never past the latest M1 bar's low (buy) / high (sell) less
        BREAKEVEN_AMEND_MARKET_BUFFER_POINTS -- if price has ALREADY retraced
        to near fill+offset, an amend there would be a stop on the wrong side
        of the market and cTrader rejects the whole request;
      * never back beyond `fill` itself, so the fallback of a deep retrace is
        exactly the old sl=fill amend, never something looser or on the far
        side of the entry.
    """
    if offset_points <= 0 or not len(m1):
        return fill
    last_bar = m1.iloc[-1]
    if side == "buy":
        market_cap = float(last_bar["low"]) - BREAKEVEN_AMEND_MARKET_BUFFER_POINTS
        return max(min(fill + offset_points, market_cap), fill)
    market_floor = float(last_bar["high"]) + BREAKEVEN_AMEND_MARKET_BUFFER_POINTS
    return min(max(fill - offset_points, market_floor), fill)


def _stop_flag_active() -> bool:
    if not STOP_FLAG.exists():
        return False
    return STOP_FLAG.read_text().strip() == datetime.now().strftime("%Y-%m-%d")


def stop_today():
    STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
    STOP_FLAG.write_text(datetime.now().strftime("%Y-%m-%d"))
    print(f"S007 stopped for today ({STOP_FLAG.read_text()}) -- "
          f"next --live cycle will close all open positions and open no new ones.")


def resume_today():
    # Always log loop_resumed, even if the flag file was already gone -- the
    # scheduler (scripts/s007_tick.py) may have logged loop_settled earlier
    # today (day_done/filtered/manual_stop) and needs a resume marker AFTER
    # that settle timestamp to start running full cycles again; without this
    # event a bare STOP_FLAG.unlink() would silently do nothing, since the
    # scheduler's "settled today" check doesn't look at the flag file at all.
    LOG.event("loop_resumed", today=datetime.now().strftime("%Y-%m-%d"))
    if STOP_FLAG.exists():
        STOP_FLAG.unlink()
        print("S007 manual stop cleared -- resuming normal trading.")
    else:
        print("S007 was not stopped -- nothing to clear (scheduler un-settled anyway).")


def _load_local(path: Path) -> pd.DataFrame:
    if path.suffix == ".gz" or path.name.endswith(".csv.gz"):
        df = pd.read_csv(path, compression="gzip")
    else:
        df = pd.read_csv(path)
    df.columns = [c.lower() for c in df.columns]
    if "date" in df.columns and "time" in df.columns:
        idx = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
    else:
        idx = pd.to_datetime(df.iloc[:, 0])
    df.index = idx
    return df[["open", "high", "low", "close"]].sort_index()


def dry_run(at: str | None):
    path = ROOT / C.DRY_RUN_DATA
    if not path.exists():
        print(f"local M1 not found at {path}; put a CSV there (date,time,open,high,low,close)")
        return
    m1 = _load_local(path)
    if at:
        m1 = m1.loc[:pd.Timestamp(at)]
    m1 = m1.tail(C.HISTORY_DAYS * 1440)
    res = plan_now(m1)
    now = str(at or (m1.index[-1] if len(m1) else "?"))
    cid = LOG.cycle_start(mode="dry_run", now=now, preset=C.PRESET,
                          in_window=res["in_window"], day_done=res["day_done"],
                          flat=res["flat"], direction=res["direction"],
                          context=res.get("context"), n_desired=len(res["positions"]))
    print(f"as-of {now}  | preset {C.PRESET} | in_window={res['in_window']} "
          f"day_done={res['day_done']} flat={res['flat']} dir={res['direction']}")
    print(f"desired open positions: {len(res['positions'])}")
    for p in res["positions"]:
        tag = "ADD" if p["is_add"] else "ENTRY"
        be = "  [BE]" if p.get("be_moved") else ""
        print(f"  {tag:5} {p['side']:4} @{p['entry']:.1f}  SL {p['sl']:.1f}  TP {p['tp']:.1f}  [{p['label']}]{be}")
        LOG.position(p["label"], "desired", cycle=cid, side=p["side"], entry=p["entry"],
                     sl=p["sl"], tp=p["tp"], is_add=p["is_add"])
    LOG.cycle_end(cid, n_desired=len(res["positions"]))


def accounts():
    from bot.ctrader_s007 import CTraderS007
    for a in CTraderS007(require_account=False).get_accounts():
        print(f"  ctidTraderAccountId={a['account_id']}  ({'LIVE' if a['is_live'] else 'DEMO'})")


def check():
    from bot.ctrader_s007 import CTraderS007
    api = CTraderS007()
    sym = api.resolve_symbol(C.SYMBOL_CANDIDATES)
    print(f"OK: symbol resolved to '{sym}'. Connection + auth working.")


def run_cycle_for_account(creds: dict | None, *, preset: str, risk_pct: float, fixed_lot: float,
                          use_fixed_lot: bool, magic: str, logger: StrategyLogger,
                          symbol_candidates=None, history_days: int | None = None,
                          daily_risk_cap_pct: float | None = None, fx_rate: float | None = None,
                          stop_flag_active=None, initial_balance: float | None = None) -> dict:
    """One S007 reconcile cycle for an arbitrary account, reusing the exact
    decide()/reconcile logic `live()` below uses for the single .env/
    accounts.yml-configured account -- so a DB-registered multi-account run
    (webapp/runner.py) and the original single-account CLI can never drift
    apart into two competing implementations of the same trading rules.

    `creds`: same shape CTraderS007(creds=...) expects (client_id,
    client_secret, access_token, account_id, host), or None to fall back to
    CTraderAdapter's own .env/accounts.yml single-account resolution (what
    `live()` below still does, unchanged).

    symbol_candidates/history_days/daily_risk_cap_pct/fx_rate default to
    bot.s007_config (C) when omitted -- override only where a specific
    account genuinely differs (none do yet). stop_flag_active defaults to
    "never stopped" (no per-account manual kill-switch file exists yet;
    the module-level STOP_FLAG below is single-account-only by design).

    `initial_balance` (ALGODEV-37): the fixed number decide()'s $ risk cap
    is computed against -- webapp/runner.py::_worker_s007 passes the DB's
    AccountStrategy.initial_balance (never the broker's live balance) so
    the cap is one stable value all day, not something that (before this)
    got smaller as the day's losses reduced the broker-reported balance,
    letting daily_risk_cap_pct silently mean less risk budget than
    intended after a losing stretch. None (the live()/CLI default, no DB
    row) falls back to the broker's live balance, unchanged from before.

    Returns dict(cycle_id, actions, error, day_done, in_window, filtered,
    manual_stop) -- actions is a list of dicts, each one of
    {kind: "open", label, side, entry, sl, tp, is_add, volume_lots},
    {kind: "close", label, reason} or
    {kind: "amend", label, sl, prev_sl} (ALGODEV-37 breakeven stop move).
    `error` is None on success or a short
    repr() of the exception that ended the cycle early.
    """
    from bot.ctrader_s007 import CTraderS007
    symbol_candidates = symbol_candidates or C.SYMBOL_CANDIDATES
    history_days = history_days or C.HISTORY_DAYS
    daily_risk_cap_pct = C.DAILY_RISK_CAP_PCT if daily_risk_cap_pct is None else daily_risk_cap_pct
    fx_rate = C.EUR_TO_USD_FX_RATE_APPROX if fx_rate is None else fx_rate
    stop_flag_active = stop_flag_active or (lambda: False)

    cid = logger.cycle_start(mode="live", preset=preset)
    actions_taken: list[dict] = []
    status_info: dict = {}

    def decide(symbol, m1, broker_positions, balance, money_per_point_per_lot,
               closed_deals=None):
        """Pure decision step (no I/O): plan_now() + diff against what the
        broker already has open, sized to equal dollar risk per position.
        Runs inside the single cTrader session (see CTraderS007.run_live_cycle)
        between fetching state (incl. balance + the instrument's contract
        metadata) and placing orders, so this cannot make its own broker
        calls -- balance and money_per_point_per_lot are handed in already
        fetched, fresh, for this cycle."""
        # money_per_point_per_lot as fetched by CTraderS007.run_live_cycle is
        # correct in DE40/GER40's own quote currency (EUR), not yet in this
        # account's deposit currency (USD) -- see C.EUR_TO_USD_FX_RATE_APPROX
        # for the full story (decisions-log.md 2026-07-23) and how/when to
        # refresh this snapshot rate. Applied here, before any logging or
        # sizing, so every downstream $ figure (risk_amount comparisons, the
        # "state"/"size" log events, lots_for_risk) is already in real USD.
        money_per_point_per_lot = money_per_point_per_lot * fx_rate
        res = plan_now(m1, preset=preset)
        manual_stop = stop_flag_active()
        status_info.update(day_done=res["day_done"], in_window=res["in_window"],
                           filtered=res.get("filtered", False), manual_stop=manual_stop)
        have = {p["label"]: p for p in broker_positions if p["label"].startswith(magic)}
        risk_amount = balance * risk_pct / 100.0

        # Real potential loss already on the books, from the broker's own fill
        # price/stopLoss (not our nominal risk_amount) -- volume is in Open API
        # raw units (see CTraderS007._volume_from_lots: raw = lots*100*lotSize),
        # and money_per_point_per_lot here already equals lotSize*FX, so
        # (volume/100)*|price-sl|*FX == lots*lotSize*FX*|price-sl|, the lotSize
        # cancels and no separate contract-size lookup is needed.
        open_risk = sum(
            (p["volume"] / 100.0) * abs(p["price"] - p["stop_loss"]) * fx_rate
            for p in have.values() if p.get("price") and p.get("stop_loss")
        )

        # Day-scoped memory of every position opened TODAY, open or already
        # closed alike -- from our own append-only position log, NOT the
        # broker's open-positions snapshot. This is the 2026-09-02 fix: both
        # day-level caps below used to be computed from `have` (open right
        # now), so every stop-out wave erased its own spent risk and handed
        # the next wave a fresh budget -- 8 positions / ~4% lost on a day
        # whose cap read "2%". A closed position must keep counting against
        # the day's budget until the day ends.
        today = m1.index[-1].date().isoformat() if len(m1) else None
        opened_today = logger.open_records(f"{magic}:{today}:") if today else {}
        # Actual fill price for closed-today labels, from the broker's deal
        # history (run_live_cycle fetches the last 24h of closing deals in the
        # same session): a position that opened and died between two reconcile
        # snapshots never appeared in `have`, so its slippage-adjusted risk
        # exists nowhere else. Matched by position_id (logged at open, below).
        # No deal match (fetch failed / closed same second) -> fall back to
        # our logged planned entry: risk without slippage, still counted.
        deal_entry = {}
        for dl in (closed_deals or []):
            pid = dl.get("position_id")
            if pid is not None and pid not in deal_entry:
                deal_entry[pid] = dl.get("entry_price")
        closed_risk_today = 0.0
        for lab_, rec in opened_today.items():
            if lab_ in have:
                continue  # still open -- already counted via open_risk above
            lot_, sl_ = rec.get("volume_lots"), rec.get("sl")
            entry_ = deal_entry.get(rec.get("position_id")) or rec.get("entry")
            if not lot_ or sl_ is None or entry_ is None:
                continue
            closed_risk_today += (float(lot_) * abs(float(entry_) - float(sl_))
                                  * money_per_point_per_lot)
        spent_risk_today = open_risk + closed_risk_today
        opened_today_count = len(set(opened_today) | set(have))

        # ALGODEV-37: the $ cap is computed against `initial_balance` (the
        # DB's AccountStrategy.initial_balance, threaded in from
        # webapp/runner.py::_worker_s007 -- see run_cycle_for_account's own
        # docstring), NOT the broker's live `balance`. Using live balance
        # made the cap shrink right along with the day's losses (2% of an
        # already-reduced balance is a smaller $ number), which is backwards
        # for a budget that's supposed to bound the day's risk -- found live
        # 2026-09-02 (Anton): after ~$312 of realized morning stop-outs,
        # cumulative day risk (realized + still-open) had already passed 4%
        # of the day's STARTING balance while the live-balance-based cap
        # kept reporting room to add more. `initial_balance=None` (the
        # live()/CLI default, no DB row) falls back to live `balance`,
        # unchanged from before.
        risk_cap = (initial_balance if initial_balance is not None else balance) \
            * daily_risk_cap_pct / 100.0

        # ALGODEV-36: day-level position cap, count-based off config values
        # only -- floor(daily_risk_cap_pct / risk_pct), e.g. 2% / 0.5% = 4
        # positions/day, across BOTH legs (primary + any b_reversal_to_A
        # recovery leg) combined. Counted against opened_today_count (every
        # label OUR LOG opened today, closed or not, union the broker's live
        # snapshot) -- counting `have` alone let each stop-out wave reset the
        # count to zero on 2026-09-02. A position must clear BOTH this count
        # cap and the $ budget check below to be placed -- this one exists
        # because lots_for_risk's min-lot floor can overshoot the nominal
        # risk_amount on a wide-stop entry (found live 2026-09-02: a floored
        # position's real $ risk came out ~1.6x its nominal target), and as
        # the config-only backstop should the $ math above ever go wrong.
        max_positions_per_day = int(daily_risk_cap_pct / risk_pct + 1e-9) if risk_pct > 0 else 10**9

        logger.event("state", cycle=cid, symbol=symbol,
                     last_bar=str(m1.index[-1]) if len(m1) else None,
                     in_window=res["in_window"], day_done=res["day_done"], flat=res["flat"],
                     direction=res["direction"], context=res.get("context"),
                     broker_positions=len(broker_positions), ours_open=len(have),
                     n_desired=len(res["positions"]), balance=balance,
                     money_per_point_per_lot=money_per_point_per_lot,
                     risk_amount=risk_amount, use_fixed_lot=use_fixed_lot,
                     open_risk=open_risk, closed_risk_today=closed_risk_today,
                     spent_risk_today=spent_risk_today,
                     opened_today=opened_today_count, risk_cap=risk_cap,
                     max_positions_per_day=max_positions_per_day)

        # A "ghost": the engine entered AND resolved (stop/tp/daycap) this
        # position within bars already elapsed by the time this cycle polled
        # -- decide() only ever places broker orders for res["positions"]
        # (status 'eod', still open as of "now"), so a ghost never becomes a
        # real trade and would otherwise leave no trace anywhere. Logged once
        # per label (label_was_ghosted guards against re-logging it every
        # cycle for the rest of the day, since plan_now() keeps returning it).
        for p in res.get("resolved", []):
            lab = p["label"]
            if logger.label_was_opened(lab) or logger.label_was_ghosted(lab):
                continue
            logger.position(lab, "ghost", cycle=cid, side=p["side"], entry=p["entry"],
                            sl=p["sl"], exit=p["exit"], status=p["status"],
                            is_add=p["is_add"], is_recovery=p["is_recovery"], r=p["r"])

        out = []
        if manual_stop or res["day_done"] or res["flat"] or not res["in_window"]:
            reason = ("manual_stop" if manual_stop else
                      "target" if res["day_done"] else
                      ("flat_time" if res["flat"] else "out_of_window"))
            for lab, p in have.items():       # target hit / end of session / manual stop -> flatten
                out.append(dict(kind="close", label=lab, reason=reason,
                                position_id=p["position_id"], volume=p["volume"]))
        else:
            want = {p["label"]: p for p in res["positions"]}
            placed_this_cycle = 0
            for lab, o in want.items():       # open new entries/adds (server-side SL/TP)
                if lab in have:
                    continue
                if logger.label_was_opened(lab) and not logger.label_was_closed(lab):
                    # We ourselves already placed this label successfully and
                    # the broker no longer reports it open -- it has, by
                    # construction, already closed broker-side (stop/TP/manual),
                    # even though nothing logged the close yet (nothing had a
                    # reason to, until this reconcile came back empty). Backfill
                    # the close now, in THIS cycle, before label_was_closed()
                    # is checked below -- otherwise a lagging M1 bar can still
                    # "want" this label and we'd re-place it with a stop the
                    # live price has already moved past (bug found live
                    # 2026-07-30, see decisions-log.md).
                    logger.position(lab, "close", cycle=cid, reason="broker_side_close_detected")
                    logger.event("skip_reopen", cycle=cid, label=lab,
                                 reason="broker_side_close_detected")
                    continue
                if logger.label_was_closed(lab):
                    # Broker doesn't show it open, but OUR log already saw it
                    # close today -- a real stop-out the current M1 bar just
                    # hasn't caught up to yet, not "never opened". Re-placing
                    # here is exactly the 2026-07-21 duplicate-reopen bug.
                    logger.event("skip_reopen", cycle=cid, label=lab,
                                 reason="already_closed_today_per_position_log")
                    continue
                if opened_today_count + placed_this_cycle >= max_positions_per_day:
                    # Today's position count (everything opened today per our
                    # own log + placed so far this cycle) already at the
                    # config-derived cap -- skip, don't open. See
                    # max_positions_per_day's own comment.
                    logger.event("skip_max_positions", cycle=cid, label=lab,
                                 opened_today=opened_today_count + placed_this_cycle,
                                 max_positions_per_day=max_positions_per_day)
                    continue
                # ALGODEV-38: this loop only ever reaches here for a label
                # NOT already in `have` -- i.e. every position placed below
                # is, by construction, brand new at the broker. It must use
                # the position's PRE-breakeven stop (o["orig_sl"]), not
                # o["sl"] -- the engine's bar replay can report a position as
                # already be_moved (sl == entry) on the very first cycle it's
                # detected, if the replayed price path crossed the breakeven
                # trigger before this cycle ever ran. Using o["sl"] there
                # placed a live market order with a zero-distance stop
                # (instant stop-out on the next tick/spread) AND silently
                # zeroed this trade's cost in the $ risk-cap check below
                # (new_risk = lot * stop_distance * ... = 0), letting it
                # bypass a cap that had just correctly rejected it -- found
                # live 2026-09-03 (twice, same session). The existing amend
                # path (below) still moves the broker's real stop to
                # breakeven from the ACTUAL fill price once the position is
                # genuinely open -- unaffected by this fix.
                place_sl = o.get("orig_sl", o["sl"])
                stop_distance = abs(o["entry"] - place_sl)
                if use_fixed_lot:
                    lot = fixed_lot
                else:
                    lot = lots_for_risk(risk_amount, stop_distance,
                                        money_per_point_per_lot, min_lot=fixed_lot)
                # ALGODEV-37 + 2026-09-02 fix: $ gate, alongside (not instead
                # of) the count cap above. spent_risk_today is the whole
                # day's budget consumption -- open positions (broker fill
                # price) PLUS everything already closed today (deal-history
                # fill, or our logged entry as fallback) -- so a wave of
                # stop-outs no longer refunds its own risk to the next wave,
                # and a slippage-inflated fill keeps counting at its real
                # size after it closes.
                new_risk = lot * stop_distance * money_per_point_per_lot
                if spent_risk_today + new_risk > risk_cap:
                    logger.event("skip_risk_cap", cycle=cid, label=lab,
                                 spent_risk_today=spent_risk_today,
                                 new_risk=new_risk, risk_cap=risk_cap)
                    continue
                spent_risk_today += new_risk
                placed_this_cycle += 1
                logger.event("size", cycle=cid, label=lab, stop_distance=stop_distance,
                             risk_amount=risk_amount, money_per_point_per_lot=money_per_point_per_lot,
                             lot=lot)
                out.append(dict(kind="place", label=lab, side=o["side"], sl=place_sl, tp=o["tp"],
                                volume_lots=lot, entry=o["entry"], is_add=o["is_add"]))
            # ALGODEV-37 live breakeven: originally WHEN to fire came only
            # from the engine (plan_now()'s be_moved flag, computed on the
            # engine's own theoretical entry/risk0), and this layer only
            # decided WHERE the stop goes (the broker's ACTUAL fill price,
            # so the amend itself includes slippage). ALGODEV-39 (found live
            # 2026-09-04): that left the TRIGGER blind to slippage -- a
            # poorly filled entry can sit real price = 0.5R+ of its REAL
            # (fill-to-stop) risk in favor while the engine, replaying its
            # own clean entry, still sees far less than 0.5R and never
            # fires. So the trigger now ALSO fires off the broker's real
            # numbers: real fill/stop (already fetched into `have` this
            # cycle) plus the active preset's breakeven_at_r (surfaced by
            # plan_now() as res["breakeven_at_r"]), checked against every M1
            # bar since the position's real open time (`opened_ts`) the same
            # conservative way the engine checks its own bars. Either
            # trigger firing is sufficient -- the engine path still covers
            # the base (near-zero-slippage) case exactly as before.
            # Deliberately OUTSIDE the day-cap accounting above (frozen
            # invariant, 2026-09-02 fix): an amend opens nothing, so it must
            # never touch opened_today/spent_risk_today math. Moving the SL
            # toward entry shrinks the broker-reported open_risk NEXT cycle
            # -- that is correct (the risk really is gone), and the position
            # still counts in opened_today_count, so the count cap cannot be
            # bypassed.
            breakeven_at_r = res.get("breakeven_at_r")
            # ALGODEV-41: 0.0 (or a missing key, e.g. an older/stubbed
            # plan_now) keeps the frozen "amend to exactly the fill" behaviour.
            breakeven_offset_points = res.get("breakeven_offset_points", 0.0) or 0.0
            for lab, o in want.items():
                engine_triggered = bool(o.get("be_moved"))
                # Cheap short-circuit BEFORE touching `have`/`p` at all: if
                # breakeven is off for this preset AND the engine hasn't
                # fired, neither trigger path below can possibly apply --
                # skip exactly like the pre-ALGODEV-39 code did. Matters
                # beyond speed: `p["side"]` isn't guaranteed to exist on
                # every broker-position shape a caller/test hands in, and
                # this label may not even be a real broker position worth
                # inspecting yet.
                if not engine_triggered and breakeven_at_r is None:
                    continue
                if lab not in have:
                    continue
                p = have[lab]
                fill, cur_sl = p.get("price"), p.get("stop_loss")
                if not fill:
                    continue
                # Only ever tighten: skip if the broker stop already sits at
                # or beyond breakeven (makes the amend one-shot across cycles
                # without needing its own state -- after a successful amend,
                # cur_sl == fill and this test fails forever after). Never
                # loosen a stop the broker/user may have moved further.
                # ALGODEV-41: the threshold stays `fill`, NOT fill+offset, and
                # that is deliberate on both sides. First amend: the original
                # stop is below fill for a buy (above for a sell), so the test
                # is false and we proceed -- unchanged by the offset. After a
                # successful amend: cur_sl is fill+offset >= fill (or exactly
                # fill when the market clamp in breakeven_stop_price() bit), so
                # the test is true and the amend stays one-shot either way.
                # Raising the threshold to fill+offset would instead RE-amend
                # every cycle whenever the clamp had capped the first move --
                # repeated no-progress broker calls for a stop we already
                # deliberately placed short of the target.
                if cur_sl and ((p["side"] == "buy" and cur_sl >= fill)
                               or (p["side"] == "sell" and cur_sl <= fill)):
                    continue
                triggered = engine_triggered
                if not triggered and breakeven_at_r is not None and cur_sl is not None:
                    real_risk = abs(fill - cur_sl)
                    opened_ts = p.get("opened_ts")
                    position_opened_at = (
                        pd.to_datetime(opened_ts, unit="ms", utc=True)
                        .tz_convert("Europe/Bucharest").tz_localize(None)
                        if opened_ts else None)
                    # `opened_ts` isn't always available (e.g. a test double
                    # that doesn't model it) -- fall back to just the latest
                    # bar rather than the WHOLE day's bars, so a price swing
                    # from before this position even existed can never be
                    # mistaken for a real breakeven move.
                    bars_since_open = (m1[m1.index >= position_opened_at]
                                      if position_opened_at is not None else m1.tail(1))
                    if real_risk > 0 and len(bars_since_open):
                        if p["side"] == "buy":
                            real_trigger_price = fill + breakeven_at_r * real_risk
                            triggered = bool((bars_since_open["high"] >= real_trigger_price).any())
                        else:
                            real_trigger_price = fill - breakeven_at_r * real_risk
                            triggered = bool((bars_since_open["low"] <= real_trigger_price).any())
                if not triggered:
                    continue
                # ALGODEV-41: the stop goes to fill +- breakeven_offset_points
                # (profit direction) rather than to the fill exactly, so a
                # retrace onto it clears the round-trip spread instead of
                # booking it. offset 0.0 -> amend_sl == fill, unchanged.
                amend_sl = breakeven_stop_price(p["side"], fill,
                                                breakeven_offset_points, m1)
                logger.event("breakeven_trigger", cycle=cid, label=lab,
                             engine_entry=o["entry"], engine_sl=o["sl"],
                             engine_be_moved=bool(o.get("be_moved")),
                             broker_fill=fill, broker_sl=cur_sl,
                             breakeven_offset_points=breakeven_offset_points,
                             amend_sl=amend_sl,
                             # what the offset actually bought after the
                             # market clamp: signed distance entry -> BE stop,
                             # always >= 0 and <= breakeven_offset_points.
                             applied_offset_points=abs(amend_sl - fill))
                out.append(dict(kind="amend", label=lab, position_id=p["position_id"],
                                sl=amend_sl, tp=p.get("take_profit"),
                                prev_sl=cur_sl))
        return out

    # CTraderS007() is constructed OUTSIDE the try below, deliberately: its
    # __init__ loads credentials before any broker call. A malformed
    # credential/config must crash this process loudly (non-zero exit), not
    # get swallowed into the per-cycle except below and silently re-logged
    # every minute forever. Real incident 2026-07-29, see
    # tests/configs/test_accounts_yaml.py and decisions-log.md.
    api = CTraderS007(creds=creds)
    error = None
    try:
        cyc = api.run_live_cycle(symbol_candidates, history_days, decide)
        symbol = cyc["symbol"]
        post_positions = cyc.get("post_positions", cyc["positions"])
        for r in cyc["results"]:
            a, err, res_ = r["action"], r["error"], r["result"]
            lab = a["label"]
            if a["kind"] == "close":
                req = dict(position_id=a["position_id"], volume=a["volume"])
                if err is not None:
                    logger.order(lab, "close_position", cycle=cid, request=req, error=err)
                    continue
                logger.order(lab, "close_position", cycle=cid, request=req, result=res_)
                logger.position(lab, "close", cycle=cid, reason=a["reason"])
                actions_taken.append(dict(kind="close", label=lab, reason=a["reason"]))
            elif a["kind"] == "amend":
                # ALGODEV-37 live breakeven: stop moved to the broker's own
                # fill price once the engine's be_moved flag fired (see
                # decide() above). A failed amend is logged and retried
                # naturally next cycle (the improvement check in decide()
                # still sees the old stop).
                req = dict(position_id=a["position_id"], sl=a["sl"], tp=a["tp"],
                           prev_sl=a["prev_sl"])
                if err is not None:
                    logger.order(lab, "amend_position_sltp", cycle=cid, request=req, error=err)
                    continue
                logger.order(lab, "amend_position_sltp", cycle=cid, request=req, result=res_)
                logger.position(lab, "breakeven_moved", cycle=cid, sl=a["sl"],
                                prev_sl=a["prev_sl"], tp=a["tp"])
                actions_taken.append(dict(kind="amend", label=lab, sl=a["sl"],
                                          prev_sl=a["prev_sl"]))
            else:
                req = dict(symbol=symbol, side=a["side"], sl=a["sl"], tp=a["tp"], lot=a["volume_lots"])
                if err is not None:
                    logger.order(lab, "place_market", cycle=cid, request=req, error=err)
                    continue
                logger.order(lab, "place_market", cycle=cid, request=req, result=res_)
                # position_id from the broker's ORDER_ACCEPTED response, so a
                # later cycle can match this label to a closing deal in the
                # broker's deal history even if the position never survived
                # long enough to appear in a reconcile snapshot (see
                # spent_risk_today above). None if the response shape ever
                # changes -- the risk math then falls back to planned entry.
                try:
                    pid = int(res_.position.positionId) or None
                except Exception:
                    pid = None
                # ALGODEV-39: log the broker's REAL fill price/stop (from the
                # extra reconcile CTraderS007.run_live_cycle does right after
                # placing, cyc["post_positions"]), not just the engine's
                # planned entry/sl (a["entry"]/a["sl"]) -- a market order can
                # slip, and our own position log is the source every later
                # cycle's day-cap fallback math (spent_risk_today above) reads
                # back via logger.open_records() when the broker's own deal
                # history has no match for a same-day close. Planned values
                # are kept alongside (planned_entry/planned_sl) so slippage
                # stays visible/diagnosable, but `entry`/`sl` are now real.
                broker_position = next((p for p in post_positions
                                        if pid is not None and p.get("position_id") == pid), None)
                real_entry = broker_position["price"] if broker_position else a["entry"]
                real_sl = broker_position["stop_loss"] if broker_position else a["sl"]
                logger.position(lab, "open", cycle=cid, side=a["side"], entry=real_entry,
                                sl=real_sl, tp=a["tp"], is_add=a["is_add"],
                                volume_lots=a["volume_lots"], position_id=pid,
                                planned_entry=a["entry"], planned_sl=a["sl"])
                actions_taken.append(dict(kind="open", label=lab, side=a["side"], entry=real_entry,
                                          sl=real_sl, tp=a["tp"], is_add=a["is_add"],
                                          volume_lots=a["volume_lots"]))
    except Exception as e:
        logger.error("live cycle failed", exc=e, cycle=cid)
        error = repr(e)[:500]
    logger.cycle_end(cid, actions=len(actions_taken))
    return dict(cycle_id=cid, actions=actions_taken, error=error, **status_info)


def live():
    result = run_cycle_for_account(
        None, preset=C.PRESET, risk_pct=C.RISK_PCT, fixed_lot=C.FIXED_LOT,
        use_fixed_lot=C.USE_FIXED_LOT, magic=C.MAGIC, logger=LOG,
        stop_flag_active=_stop_flag_active)
    actions_taken = result["actions"]
    print(f"cycle done: {len(actions_taken)} actions")
    for a in actions_taken:
        if a["kind"] == "close":
            print(f"  close {a['label']} ({a['reason']})")
        elif a["kind"] == "amend":
            print(f"  amend {a['label']} SL{a['sl']:.1f} (breakeven, was {a['prev_sl']})")
        else:
            print(f"  open {a['label']} {a['side']} lot={a['volume_lots']:.3f} "
                  f"SL{a['sl']:.1f} TP{a['tp']:.1f}")
    if result.get("day_done") is not None:
        # machine-readable marker for the scheduling loop (scripts/s007_loop.py):
        # once day_done (target reached) OR filtered (today's Frankfurt range
        # already failed the height filter -- a verdict that can't change for
        # the rest of the day, see s007_signals.py::plan_now) it can stop
        # polling every minute and sleep until the next session window instead.
        print(f"STATUS day_done={result.get('day_done')} in_window={result.get('in_window')} "
              f"filtered={result.get('filtered')} manual_stop={result.get('manual_stop')} "
              f"actions={len(actions_taken)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--at", default=None)
    ap.add_argument("--accounts", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--stop-today", action="store_true")
    ap.add_argument("--resume-today", action="store_true")
    a = ap.parse_args()
    if a.stop_today:
        stop_today()
    elif a.resume_today:
        resume_today()
    elif a.accounts:
        accounts()
    elif a.check:
        check()
    elif a.live:
        live()
    else:
        dry_run(a.at)
