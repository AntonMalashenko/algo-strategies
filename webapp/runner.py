"""Multi-account runner — the trading process (decoupled from the UI).

Reads ENABLED accounts from the DB and runs one S007 reconcile cycle for each,
using that account's owner credentials and per-account strategy config. Writes
status/positions back to the DB and logs per account via the shared StrategyLogger.

Schedule it every minute during the session (cron), independently of the web UI:
    * 10-16 * * 1-5  python -m webapp.runner
Add --dry to test the whole DB->runner->status pipeline offline from local M1 CSV
(no broker, no SDK needed).
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from webapp.db import get_session, init_db
from webapp.models import Account, User, Position
from bot import s007_config as C
from bot.s007_signals import plan_now
from utils.trade_logger import StrategyLogger

ROOT = Path(__file__).resolve().parent.parent


def _creds_for(user: User) -> dict:
    """Per-user cTrader creds; client_id/secret fall back to the global app env."""
    return dict(
        client_id=user.ctrader_client_id or os.environ.get("CTRADER_CLIENT_ID"),
        client_secret=user.client_secret or os.environ.get("CTRADER_CLIENT_SECRET"),
        access_token=user.access_token or os.environ.get("CTRADER_ACCESS_TOKEN"),
        account_id=None,  # set per account below
    )


def _logger_for(acc: Account) -> StrategyLogger:
    name = f"{acc.strategy}-acct{acc.ctid_trader_account_id}"
    return StrategyLogger(name, log_root=str(ROOT / "reports" / "logs"))


def _load_local_m1() -> pd.DataFrame:
    path = ROOT / C.DRY_RUN_DATA
    comp = "gzip" if str(path).endswith(".gz") else None
    df = pd.read_csv(path, compression=comp)
    df.columns = [c.lower() for c in df.columns]
    idx = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str))
    df.index = idx
    return df[["open", "high", "low", "close"]].sort_index()


def run_account(session, acc: Account, dry: bool = False, at: str | None = None) -> str:
    """One reconcile cycle for a single account. Returns a status string."""
    log = _logger_for(acc)
    cid = log.cycle_start(mode="dry" if dry else "live", account=acc.ctid_trader_account_id,
                          preset=acc.preset, enabled=acc.enabled)
    try:
        if dry:
            m1 = _load_local_m1()
            if at:
                m1 = m1.loc[:pd.Timestamp(at)]
            m1 = m1.tail(C.HISTORY_DAYS * 1440)
            res = plan_now(m1, preset=acc.preset)
            log.event("state", cycle=cid, dry=True, in_window=res["in_window"],
                      day_done=res["day_done"], direction=res["direction"],
                      context=res.get("context"), n_desired=len(res["positions"]))
            _sync_positions(session, acc, res, cid, log, broker=None, dry=True)
            status = f"dry: {len(res['positions'])} desired"
        else:
            from bot.ctrader_s007 import CTraderS007
            creds = _creds_for(acc.user)
            creds["account_id"] = acc.ctid_trader_account_id
            creds["host"] = acc.host
            api = CTraderS007(creds=creds)
            symbol = acc.symbol or api.resolve_symbol(C.SYMBOL_CANDIDATES)
            m1 = api.get_m1(symbol, C.HISTORY_DAYS)
            res = plan_now(m1, preset=acc.preset)
            broker = {p["label"]: p for p in api.open_positions()
                      if p["label"].startswith(acc.strategy)}
            log.event("state", cycle=cid, symbol=symbol, in_window=res["in_window"],
                      day_done=res["day_done"], direction=res["direction"],
                      context=res.get("context"), broker_open=len(broker),
                      n_desired=len(res["positions"]))
            status = _reconcile_broker(api, symbol, acc, res, broker, cid, log, session)
        acc.status = status
        acc.last_error = None
    except Exception as e:                       # never let one account kill the loop
        log.error(f"account {acc.ctid_trader_account_id} cycle failed", exc=e, cycle=cid)
        acc.status = "error"
        acc.last_error = repr(e)[:500]
        status = "error"
    acc.last_cycle_at = datetime.now(timezone.utc)
    log.cycle_end(cid, status=status)
    session.commit()
    return status


def _reconcile_broker(api, symbol, acc, res, broker, cid, log, session) -> str:
    lot = acc.fixed_lot
    actions = 0
    if res["day_done"] or res["flat"] or not res["in_window"]:
        reason = "target" if res["day_done"] else ("flat_time" if res["flat"] else "closed")
        for lab, p in broker.items():
            req = dict(position_id=p["position_id"], volume=p["volume"])
            try:
                r = api.close_position(p["position_id"], p["volume"])
                log.order(lab, "close_position", cycle=cid, request=req, result=r)
                log.position(lab, "close", cycle=cid, reason=reason)
                _close_position_db(session, acc, lab, reason)
                actions += 1
            except Exception as e:
                log.order(lab, "close_position", cycle=cid, request=req, error=e)
        return f"flat ({reason}): {actions} closed"
    want = {p["label"]: p for p in res["positions"]}
    for lab, o in want.items():
        if lab in broker:
            continue
        req = dict(symbol=symbol, side=o["side"], sl=o["sl"], tp=o["tp"], lot=lot)
        try:
            r = api.place_market(symbol, o["side"], o["sl"], o["tp"], volume_lots=lot, label=lab)
            log.order(lab, "place_market", cycle=cid, request=req, result=r)
            log.position(lab, "open", cycle=cid, side=o["side"], entry=o["entry"],
                         sl=o["sl"], tp=o["tp"], is_add=o["is_add"])
            _open_position_db(session, acc, o)
            actions += 1
        except Exception as e:
            log.order(lab, "place_market", cycle=cid, request=req, error=e)
    return f"live: {len(want)} desired, {actions} new"


def _sync_positions(session, acc, res, cid, log, broker, dry):
    """Dry mode: mirror desired positions into the DB + per-position log."""
    if res["day_done"] or res["flat"]:
        for pos in [p for p in acc.positions if p.status == "open"]:
            _close_position_db(session, acc, pos.label, "target" if res["day_done"] else "flat")
        return
    for o in res["positions"]:
        log.position(o["label"], "desired", cycle=cid, side=o["side"], entry=o["entry"],
                     sl=o["sl"], tp=o["tp"], is_add=o["is_add"])
        _open_position_db(session, acc, o)


def _open_position_db(session, acc, o):
    exists = next((p for p in acc.positions if p.label == o["label"] and p.status == "open"), None)
    if exists:
        return
    session.add(Position(account_id=acc.id, label=o["label"], side=o["side"],
                         entry=o["entry"], sl=o["sl"], tp=o["tp"], is_add=o["is_add"],
                         status="open"))


def _close_position_db(session, acc, label, reason):
    for p in acc.positions:
        if p.label == label and p.status == "open":
            p.status = "closed"
            p.reason = reason
            p.closed_at = datetime.now(timezone.utc)


def run_all(dry: bool = False, at: str | None = None) -> int:
    init_db()
    session = get_session()
    accounts = session.query(Account).filter(Account.enabled.is_(True)).all()
    n = 0
    for acc in accounts:
        run_account(session, acc, dry=dry, at=at)
        n += 1
    session.close()
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="offline test from local M1 CSV")
    ap.add_argument("--at", default=None, help="dry: simulate 'now' at this timestamp")
    a = ap.parse_args()
    count = run_all(dry=a.dry, at=a.at)
    print(f"runner: processed {count} enabled account(s)")
