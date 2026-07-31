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
        print(f"  {tag:5} {p['side']:4} @{p['entry']:.1f}  SL {p['sl']:.1f}  TP {p['tp']:.1f}  [{p['label']}]")
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
                          stop_flag_active=None) -> dict:
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

    Returns dict(cycle_id, actions, error, day_done, in_window, filtered,
    manual_stop) -- actions is a list of dicts, each either
    {kind: "open", label, side, entry, sl, tp, is_add, volume_lots} or
    {kind: "close", label, reason}. `error` is None on success or a short
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

    def decide(symbol, m1, broker_positions, balance, money_per_point_per_lot):
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
        risk_cap = balance * daily_risk_cap_pct / 100.0

        logger.event("state", cycle=cid, symbol=symbol,
                     last_bar=str(m1.index[-1]) if len(m1) else None,
                     in_window=res["in_window"], day_done=res["day_done"], flat=res["flat"],
                     direction=res["direction"], context=res.get("context"),
                     broker_positions=len(broker_positions), ours_open=len(have),
                     n_desired=len(res["positions"]), balance=balance,
                     money_per_point_per_lot=money_per_point_per_lot,
                     risk_amount=risk_amount, use_fixed_lot=use_fixed_lot,
                     open_risk=open_risk, risk_cap=risk_cap)

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
                stop_distance = abs(o["entry"] - o["sl"])
                if use_fixed_lot:
                    lot = fixed_lot
                else:
                    lot = lots_for_risk(risk_amount, stop_distance,
                                        money_per_point_per_lot, min_lot=fixed_lot)
                new_risk = lot * stop_distance * money_per_point_per_lot
                if open_risk + new_risk > risk_cap:
                    # Would push today's summed potential loss (already-open +
                    # this one) past DAILY_RISK_CAP_PCT -- skip, don't open.
                    logger.event("skip_risk_cap", cycle=cid, label=lab,
                                 open_risk=open_risk, new_risk=new_risk, risk_cap=risk_cap)
                    continue
                open_risk += new_risk
                logger.event("size", cycle=cid, label=lab, stop_distance=stop_distance,
                             risk_amount=risk_amount, money_per_point_per_lot=money_per_point_per_lot,
                             lot=lot)
                out.append(dict(kind="place", label=lab, side=o["side"], sl=o["sl"], tp=o["tp"],
                                volume_lots=lot, entry=o["entry"], is_add=o["is_add"]))
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
            else:
                req = dict(symbol=symbol, side=a["side"], sl=a["sl"], tp=a["tp"], lot=a["volume_lots"])
                if err is not None:
                    logger.order(lab, "place_market", cycle=cid, request=req, error=err)
                    continue
                logger.order(lab, "place_market", cycle=cid, request=req, result=res_)
                logger.position(lab, "open", cycle=cid, side=a["side"], entry=a["entry"],
                                sl=a["sl"], tp=a["tp"], is_add=a["is_add"], volume_lots=a["volume_lots"])
                actions_taken.append(dict(kind="open", label=lab, side=a["side"], entry=a["entry"],
                                          sl=a["sl"], tp=a["tp"], is_add=a["is_add"],
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
