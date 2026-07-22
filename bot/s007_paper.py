"""S007 paper/live runner (GER40 London x Frankfurt).

Dry-run works fully offline from a local M1 CSV and prints the desired position
set — use it to eyeball the bot before touching the broker:

    python -m bot.s007_paper --dry-run
    python -m bot.s007_paper --dry-run --at "2024-05-10 10:57"

Broker cycle (reuses the S004 cTrader connection / .env):

    python -m bot.s007_paper --accounts     # list account ids for the token
    python -m bot.s007_paper --check         # auth + balance + GER40 present
    python -m bot.s007_paper --live          # one reconcile cycle; schedule every 1 min

The bot is stateless: each cycle rebuilds the day's state from recent M1 bars via
the validated engine and reconciles the broker to it. The common 0.5 stop and the
day target are attached to each order (server-side); the bot only opens new
entries/adds and closes everything at the day's target or at 16:59.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from bot import s007_config as C
from bot.risk import lots_for_risk
from bot.s007_signals import plan_now
from utils.trade_logger import StrategyLogger

ROOT = Path(__file__).resolve().parent.parent
LOG = StrategyLogger("S007", log_root=str(ROOT / "reports" / "logs"))


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


def live():
    from bot.ctrader_s007 import CTraderS007
    cid = LOG.cycle_start(mode="live", preset=C.PRESET)
    actions_taken = []
    status = {}

    def decide(symbol, m1, broker_positions, balance, money_per_point_per_lot):
        """Pure decision step (no I/O): plan_now() + diff against what the
        broker already has open, sized to equal dollar risk per position.
        Runs inside the single cTrader session (see CTraderS007.run_live_cycle)
        between fetching state (incl. balance + the instrument's contract
        metadata) and placing orders, so this cannot make its own broker
        calls -- balance and money_per_point_per_lot are handed in already
        fetched, fresh, for this cycle."""
        res = plan_now(m1)
        status.update(day_done=res["day_done"], in_window=res["in_window"])
        have = {p["label"]: p for p in broker_positions if p["label"].startswith(C.MAGIC)}
        risk_amount = balance * C.RISK_PCT / 100.0
        LOG.event("state", cycle=cid, symbol=symbol,
                  last_bar=str(m1.index[-1]) if len(m1) else None,
                  in_window=res["in_window"], day_done=res["day_done"], flat=res["flat"],
                  direction=res["direction"], context=res.get("context"),
                  broker_positions=len(broker_positions), ours_open=len(have),
                  n_desired=len(res["positions"]), balance=balance,
                  money_per_point_per_lot=money_per_point_per_lot,
                  risk_amount=risk_amount, use_fixed_lot=C.USE_FIXED_LOT)

        out = []
        if res["day_done"] or res["flat"] or not res["in_window"]:
            reason = "target" if res["day_done"] else ("flat_time" if res["flat"] else "out_of_window")
            for lab, p in have.items():       # target hit / end of session -> flatten
                out.append(dict(kind="close", label=lab, reason=reason,
                                position_id=p["position_id"], volume=p["volume"]))
        else:
            want = {p["label"]: p for p in res["positions"]}
            for lab, o in want.items():       # open new entries/adds (server-side SL/TP)
                if lab in have:
                    continue
                if LOG.label_was_closed(lab):
                    # Broker doesn't show it open, but OUR log already saw it
                    # close today -- a real stop-out the current M1 bar just
                    # hasn't caught up to yet, not "never opened". Re-placing
                    # here is exactly the 2026-07-21 duplicate-reopen bug.
                    LOG.event("skip_reopen", cycle=cid, label=lab,
                              reason="already_closed_today_per_position_log")
                    continue
                stop_distance = abs(o["entry"] - o["sl"])
                if C.USE_FIXED_LOT:
                    lot = C.FIXED_LOT
                else:
                    lot = lots_for_risk(risk_amount, stop_distance,
                                        money_per_point_per_lot, min_lot=C.FIXED_LOT)
                LOG.event("size", cycle=cid, label=lab, stop_distance=stop_distance,
                          risk_amount=risk_amount, money_per_point_per_lot=money_per_point_per_lot,
                          lot=lot)
                out.append(dict(kind="place", label=lab, side=o["side"], sl=o["sl"], tp=o["tp"],
                                volume_lots=lot, entry=o["entry"], is_add=o["is_add"]))
        return out

    try:
        api = CTraderS007()
        cyc = api.run_live_cycle(C.SYMBOL_CANDIDATES, C.HISTORY_DAYS, decide)
        symbol = cyc["symbol"]
        for r in cyc["results"]:
            a, err, res_ = r["action"], r["error"], r["result"]
            lab = a["label"]
            if a["kind"] == "close":
                req = dict(position_id=a["position_id"], volume=a["volume"])
                if err is not None:
                    LOG.order(lab, "close_position", cycle=cid, request=req, error=err)
                    continue
                LOG.order(lab, "close_position", cycle=cid, request=req, result=res_)
                LOG.position(lab, "close", cycle=cid, reason=a["reason"])
                actions_taken.append(f"close {lab} ({a['reason']})")
            else:
                req = dict(symbol=symbol, side=a["side"], sl=a["sl"], tp=a["tp"], lot=a["volume_lots"])
                if err is not None:
                    LOG.order(lab, "place_market", cycle=cid, request=req, error=err)
                    continue
                LOG.order(lab, "place_market", cycle=cid, request=req, result=res_)
                LOG.position(lab, "open", cycle=cid, side=a["side"], entry=a["entry"],
                             sl=a["sl"], tp=a["tp"], is_add=a["is_add"], volume_lots=a["volume_lots"])
                actions_taken.append(
                    f"open {lab} {a['side']} lot={a['volume_lots']:.3f} SL{a['sl']:.1f} TP{a['tp']:.1f}")
    except Exception as e:
        LOG.error("live cycle failed", exc=e, cycle=cid)
    LOG.cycle_end(cid, actions=len(actions_taken))
    print(f"cycle done: {len(actions_taken)} actions")
    for a_ in actions_taken:
        print(" ", a_)
    if status:
        # machine-readable marker for the scheduling loop: once day_done and
        # nothing left to close (0 actions this cycle), it can stop polling
        # every minute and sleep until the next session window instead.
        print(f"STATUS day_done={status.get('day_done')} in_window={status.get('in_window')} "
              f"actions={len(actions_taken)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--at", default=None)
    ap.add_argument("--accounts", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    if a.accounts:
        accounts()
    elif a.check:
        check()
    elif a.live:
        live()
    else:
        dry_run(a.at)
