"""Refresh the DB's picture of positions from the broker — the reconciler.

The trading runner (webapp/runner.py) writes what it *intended*: it inserts a
Position row when it sends an order and closes that row when it sends a close.
Anything that happens without the runner in the loop — a stop-loss or take-
profit firing between ticks, a manual close in the cTrader UI, a trade opened
by hand or by an older bot, a worker killed by the tick timeout after the
order went out but before the commit — leaves the DB describing a world that
no longer exists. This module reads the broker and makes the DB match:

  * a DB row that is still open at the broker  -> refresh entry / SL / TP /
    volume, and record broker_position_id (the only durable handle we have;
    without it a later close cannot be matched to its deal, so capturing it
    while the position is open is the whole reason this runs every cycle)
  * a DB row the broker no longer has          -> close it, and fill
    exit_price / volume_lots / gross_profit / swap / commission / pnl from
    the closing deal when broker_position_id lets us find it
  * a broker position with no DB row           -> insert one with
    origin='adopted', so the UI shows the real account rather than only the
    slice this bot happens to know about

Scope: cTrader (S007) only, on purpose. Bybit's positions() reports
`{symbol: net qty}` with no per-position id and no label, so a Bybit row can
be matched to *a* symbol but never to a specific Position record — inventing
that mapping would corrupt history rather than fix it. Bybit accounts are
skipped, loudly, not silently half-synced.

What it deliberately does NOT do: synthesize closed rows out of deals for
positions it never saw open. A closing deal carries no label, so such a row
could not be told apart from the bot row that same close belongs to, and the
two would double-count in every PnL total. Adoption therefore happens only
while a position is open at the broker; since this runs at the end of every
trading cycle, an adopted position gets its row within one tick and its PnL
when it later closes. History from before this module existed stays as it is.

It places no orders. Nothing here can open, close, or modify a position at
the broker — the only broker calls are ProtoOAReconcileReq, ProtoOADealListReq
and ProtoOASymbolByIdReq, all read-only (bot/ctrader_s007.py::sync_snapshot).

Two modes, same file, mirroring webapp/runner.py:

  Coordinator (default):
      python -m webapp.sync_positions --strategy S007
    One worker subprocess per enabled (account, strategy) row, in parallel.

  Worker (internal): --worker --account-strategy-id N

Why subprocesses again: CTraderAdapter._run drives its session through
Twisted's reactor.run(), and a reactor can only be run once per OS process
(ReactorNotRestartable). That is also why webapp/runner.py cannot simply call
sync_account_strategy() in-process after a cycle — it has already spent its
reactor — and instead spawns a worker via spawn_sync_worker() below.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from webapp.db import get_session
from webapp.models import Account, AccountStrategy, Position, Strategy
from webapp.schemas import LogKind, LogLevel

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TIMEOUT_S = 45.0
# How far back to ask for deals. A position closed while the syncer was down
# still gets its PnL as long as it closed inside this window; beyond it the
# row is closed with NULL money fields (unknown, not zero).
DEFAULT_LOOKBACK_DAYS = 7

# Reason stamped on a row the broker no longer has and the runner never
# closed. Deliberately not "stop"/"target": which one it was is a guess, and
# a guessed reason is indistinguishable from a recorded one once it is in the
# table. The exit price is stored next to it; infer from that if you must.
REASON_BROKER_CLOSED = "broker_closed"


def _aggregate_deals(deals: list) -> dict:
    """{position_id: merged deal} — a position can be closed in several
    partial deals, and only their sum is the position's real result. The
    last-executed deal supplies exit_price/deal_id; volumes and money add up.
    """
    out: dict[int, dict] = {}
    for d in sorted(deals, key=lambda x: x.get("executed_ms") or 0):
        pid = d["position_id"]
        cur = out.get(pid)
        if cur is None:
            out[pid] = dict(d)
            continue
        for k in ("gross_profit", "swap", "commission", "pnl"):
            cur[k] = (cur.get(k) or 0.0) + (d.get(k) or 0.0)
        cur["closed_volume"] = (cur.get("closed_volume") or 0) + (d.get("closed_volume") or 0)
        if cur.get("volume_lots") is not None and d.get("volume_lots") is not None:
            cur["volume_lots"] += d["volume_lots"]
        # later deal wins for the "when/at what price did it finally end" facts
        cur["exit_price"] = d.get("exit_price")
        cur["deal_id"] = d.get("deal_id")
        cur["executed_ms"] = d.get("executed_ms")
    return out


def _apply_deal(pos: Position, deal: dict) -> None:
    pos.exit_price = deal.get("exit_price")
    pos.gross_profit = deal.get("gross_profit")
    pos.swap = deal.get("swap")
    pos.commission = deal.get("commission")
    pos.pnl = deal.get("pnl")
    pos.broker_deal_id = deal.get("deal_id")
    if deal.get("volume_lots") is not None:
        pos.volume_lots = deal["volume_lots"]


def _ms_to_dt(ms) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).replace(tzinfo=None)


def apply_snapshot(session, acc: Account, strat: Strategy, snap: dict,
                   now: datetime | None = None) -> dict:
    """Fold one broker snapshot into the DB. Pure DB work — no broker calls,
    no network — so the whole reconciliation can be unit-tested against a
    hand-built snapshot dict. Does not commit; the caller owns the transaction.

    Returns counters: {"refreshed", "closed", "priced", "adopted", "skipped"}.
    """
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    broker_open = list(snap.get("positions") or [])
    by_pid = _aggregate_deals(list(snap.get("deals") or []))

    # Two ways to recognise one of our rows in the broker's list, in priority
    # order. broker_position_id first: it is the broker's own handle, exact,
    # and it is the ONLY thing that matches an adopted row -- an adopted
    # position's label is ours (`adopted:<id>`), not one the broker echoes
    # back, so a label-only match would drop it every pass and close a
    # position that is still very much open.
    by_broker_id = {p["position_id"]: p for p in broker_open if p.get("position_id")}
    # Label match for bot rows: it works before the first sync has captured an
    # id, which is exactly the window where a row would otherwise look closed.
    ours = {p["label"]: p for p in broker_open
            if (p.get("label") or "").startswith(strat.name)}

    rows = (session.query(Position)
            .filter_by(account_id=acc.id, strategy_id=strat.id, status="open").all())

    n = dict(refreshed=0, closed=0, priced=0, adopted=0, skipped=0)
    matched_pids: set[int] = set()

    for pos in rows:
        live = by_broker_id.get(pos.broker_position_id) if pos.broker_position_id else None
        if live is None:
            live = ours.get(pos.label)
        if live is not None:
            if live.get("position_id"):
                matched_pids.add(live["position_id"])
            # Broker truth overwrites our intent: `entry` was what we asked
            # for, `price` is what we got, and SL/TP may have been moved at
            # the broker since. Guard each one -- a broker that reports 0 for
            # an unset TP must not turn a real TP into 0.0 in the DB.
            if live.get("price"):
                pos.entry = live["price"]
            if live.get("stop_loss"):
                pos.sl = live["stop_loss"]
            if live.get("take_profit"):
                pos.tp = live["take_profit"]
            if live.get("volume_lots") is not None:
                pos.volume_lots = live["volume_lots"]
            pos.broker_position_id = live.get("position_id")
            pos.synced_at = now
            n["refreshed"] += 1
            continue

        # Gone from the broker -> it closed while we were not looking.
        pos.status = "closed"
        pos.reason = pos.reason or REASON_BROKER_CLOSED
        deal = by_pid.get(pos.broker_position_id) if pos.broker_position_id else None
        if deal:
            _apply_deal(pos, deal)
            pos.closed_at = pos.closed_at or _ms_to_dt(deal.get("executed_ms")) or now
            n["priced"] += 1
        else:
            # No broker_position_id (opened before this syncer existed, or the
            # close fell outside the deal lookback): close the row honestly and
            # leave every money column NULL. NULL means "unknown" -- anything
            # summing pnl must skip it rather than read it as break-even.
            pos.closed_at = pos.closed_at or now
        pos.synced_at = now
        n["closed"] += 1

    # Adoption. Two guards, both necessary:
    #   * a label belonging to another strategy is left alone -- that
    #     strategy's own sync owns it, and adopting it here would duplicate
    #     the row under the wrong strategy_id;
    #   * broker_position_id is checked account-wide (not per strategy) so a
    #     second sync pass, or a second strategy on the same account, cannot
    #     adopt the same broker position twice.
    known_prefixes = [s for (s,) in session.query(Strategy.name).all()]
    for p in broker_open:
        label = p.get("label") or ""
        if p.get("position_id") in matched_pids:
            continue
        if any(label.startswith(pref) for pref in known_prefixes
               if pref != strat.name):
            n["skipped"] += 1
            continue
        pid = p.get("position_id")
        dupe = (session.query(Position)
                .filter_by(account_id=acc.id, broker_position_id=pid).first()) if pid else None
        if dupe is not None:
            continue
        session.add(Position(
            account_id=acc.id, strategy_id=strat.id,
            # deterministic and obviously not a bot label, so a re-run cannot
            # create a second row and nobody mistakes it for a bot trade
            label=label or f"adopted:{pid}",
            side=p.get("side") or "buy",
            entry=p.get("price") or 0.0,
            sl=p.get("stop_loss") or 0.0,
            tp=p.get("take_profit") or 0.0,
            is_add=False, status="open", origin="adopted",
            broker_position_id=pid,
            volume_lots=p.get("volume_lots"),
            opened_at=_ms_to_dt(p.get("opened_ts")) or now,
            synced_at=now))
        n["adopted"] += 1

    return n


def sync_account_strategy(session, link: AccountStrategy,
                          days: int = DEFAULT_LOOKBACK_DAYS) -> dict:
    """Read the broker for one (account, strategy) row and fold it into the
    DB. Commits. Raises on a broker/credential failure -- the caller decides
    whether that is fatal (worker) or merely logged (end of trading cycle)."""
    from webapp.runner import _log_event   # local: keeps this module importable
                                           # (and unit-testable) without bot/

    acc = link.account
    strat = link.strategy
    if acc.broker != "CTRADER":
        raise SystemExit(
            f"account_strategy {link.id} is broker={acc.broker} -- position sync "
            f"supports CTRADER only (Bybit reports net size per symbol, with no "
            f"position id or label to reconcile a Position row against)")

    from bot.ctrader_s007 import CTraderS007   # local: no SDK needed to import

    creds_row = acc.credentials
    api = CTraderS007(creds=dict(
        client_id=creds_row.get("client_id"), client_secret=creds_row.get("client_secret"),
        access_token=creds_row.get("access_token"),
        account_id=int(acc.external_account_id) if acc.external_account_id else None,
        host=acc.broker_host))

    snap = api.sync_snapshot(days=days)
    n = apply_snapshot(session, acc, strat, snap)

    _log_event(session, LogKind.SYNC,
               message=(f"sync: {n['refreshed']} open, {n['closed']} closed "
                        f"({n['priced']} priced), {n['adopted']} adopted"),
               level=LogLevel.WARNING if n["adopted"] else LogLevel.INFO,
               user=acc.user, account=acc, strategy=strat,
               payload=dict(n, broker_open=len(snap.get("positions") or []),
                            deals=len(snap.get("deals") or []), lookback_days=days))
    if n["adopted"]:
        _log_event(session, LogKind.POSITION_ADOPTED, level=LogLevel.WARNING,
                   message=f"{n['adopted']} broker position(s) this bot did not open",
                   user=acc.user, account=acc, strategy=strat)
    session.commit()
    print(f"[sync] account_strategy {link.id}: {n}")
    return n


def spawn_sync_worker(account_strategy_id: int, timeout_s: float) -> int | None:
    """Run one sync as a SUBPROCESS and wait up to `timeout_s`.

    This is what webapp/runner.py calls at the end of a trading cycle: the
    runner's own process has already spent its Twisted reactor on the cycle,
    so the sync cannot happen in-process (ReactorNotRestartable). Returns the
    exit code, or None if it had to be killed. Never raises -- a failed sync
    must not turn a completed trading cycle into a failed one.
    """
    if timeout_s <= 0:
        return None
    try:
        p = subprocess.Popen([sys.executable, "-m", "webapp.sync_positions",
                              "--worker", "--account-strategy-id", str(account_strategy_id)])
    except Exception as e:                      # noqa: BLE001 - see docstring
        print(f"[sync] could not spawn worker for {account_strategy_id}: {e!r}")
        return None
    try:
        return p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        print(f"[sync] account_strategy {account_strategy_id}: TIMEOUT after "
              f"{timeout_s:.0f}s, killed")
        return None


def run_worker(account_strategy_id: int, days: int = DEFAULT_LOOKBACK_DAYS) -> int:
    session = get_session()
    link = session.get(AccountStrategy, account_strategy_id)
    if link is None:
        raise SystemExit(f"account_strategy {account_strategy_id} not found")
    try:
        sync_account_strategy(session, link, days=days)
        return 0
    finally:
        session.close()


def run_coordinator(strategy_name: str, timeout_s: float = DEFAULT_TIMEOUT_S,
                    days: int = DEFAULT_LOOKBACK_DAYS) -> int:
    """One sync pass for every enabled row of `strategy_name`. Same exit-code
    contract as webapp/runner.py's coordinator: one account failing is that
    account's problem, not the pass's."""
    session = get_session()
    strat = session.query(Strategy).filter_by(name=strategy_name).one_or_none()
    if strat is None:
        raise SystemExit(f"strategy '{strategy_name}' not found in DB")
    link_ids = [l.id for l in session.query(AccountStrategy)
                .filter_by(strategy_id=strat.id, enabled=True).all()]
    session.close()

    if not link_ids:
        print(f"[sync] no enabled account_strategy rows for '{strategy_name}'")
        return 0

    print(f"[sync] {strategy_name}: {len(link_ids)} account(s), budget={timeout_s:.0f}s")
    procs = {lid: subprocess.Popen(
        [sys.executable, "-m", "webapp.sync_positions", "--worker",
         "--account-strategy-id", str(lid), "--days", str(days)])
        for lid in link_ids}

    deadline = time.monotonic() + timeout_s
    results = {}
    for lid, p in procs.items():
        try:
            results[lid] = p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            results[lid] = -9
            print(f"[sync] account_strategy {lid}: TIMEOUT, killed")
    ok = sum(1 for rc in results.values() if rc == 0)
    print(f"[sync] {strategy_name}: {ok}/{len(link_ids)} ok "
          f"({', '.join(f'{lid}={rc}' for lid, rc in results.items())})")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default="S007")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    ap.add_argument("--days", type=int, default=DEFAULT_LOOKBACK_DAYS,
                    help=f"deal-history lookback (default {DEFAULT_LOOKBACK_DAYS})")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--account-strategy-id", type=int, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.worker:
        if a.account_strategy_id is None:
            raise SystemExit("--worker requires --account-strategy-id")
        sys.exit(run_worker(a.account_strategy_id, days=a.days))
    sys.exit(run_coordinator(a.strategy, timeout_s=a.timeout, days=a.days))


if __name__ == "__main__":
    main()
