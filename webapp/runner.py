"""Multi-account runner — the trading process (decoupled from the UI).

Strategy-agnostic dispatch: STRATEGY_WORKERS (bottom of this module) maps a
Strategy.name to the worker function that knows how to run one cycle for
one (account, strategy) row. Adding a new strategy here means writing that
one worker function and registering it -- run_worker()/run_coordinator()
themselves never need to change. S007 (_worker_s007, CTRADER) and S009
(_worker_s009, BYBIT) are registered; S009's worker is deliberately
shadow-only (broker="off") until enabling real orders through this
DB-driven path is its own explicit decision. S009's per-account state
(book/equity/last-booked-day) is DB-backed (webapp/state_store.py's
DBStateStore, the `strategy_state` table) instead of the single shared
file bot/s009_paper.py's single-account CLI path still uses -- that is
what made running several Bybit accounts through here safe: the old shared
reports/paper_s009/state.json would have had them clobber each other.

Two modes, same file:

  Coordinator (default) -- one call per strategy per tick, e.g.:
      python -m webapp.runner --strategy S007
    Loads the strategy's enabled AccountStrategy rows from the DB and spawns
    one WORKER subprocess per account, in parallel, waiting up to --timeout
    seconds (default 55s, leaving headroom in a 60s scheduler tick) before
    killing stragglers. Exits 0 as long as the tick itself could be planned
    and run -- an individual account's cycle failing is recorded on that
    account_strategy row (status='error', last_error=...), NOT surfaced as
    the coordinator's own exit code, since one bad account must never look
    like "the whole tick failed" to Ofelia/the scheduler.

  Worker (internal, spawned by the coordinator; --worker --account-strategy-id N):
    Runs exactly ONE S007 cycle for ONE (account, strategy) row, then exits.

Why subprocess-per-account instead of threads/asyncio in one process: cTrader
(CTraderAdapter._run) drives its whole session through `reactor.run()`
(Twisted) and stops the reactor when the session ends. A Twisted reactor can
only be reactor.run()'d ONCE per OS process -- a second call in the same
process raises ReactorNotRestartable. So N accounts in one tick can only run
truly in parallel as N separate processes, each with its own reactor; that is
exactly what the coordinator does below, and it also happens to be a clean
fit for "one job, one exit code, no persistent state" containerization later
(each worker subprocess IS that unit, just not yet wrapped in its own
container -- see the multi-user-architecture decision, Docker/Ofelia stage,
not started yet).

Logging split: curated business events (cycle_start/cycle_end/position_open/
position_close/error) go to the DB `logs` table via _log_event() below, for
the future API/UI. Everything else -- the full per-cycle debug detail
(state/size/skip_* events, order requests/results) -- keeps going to
utils.trade_logger.StrategyLogger's per-account JSONL files under
reports/logs/, same as the single-account bot; those files are also where
label_was_opened()/label_was_closed() read their history-based dedup state
from, so they are NOT optional debug output that can simply be dropped in a
container -- containerizing this runner will need reports/ on a persistent
volume, not a fresh ephemeral filesystem per tick (open item, not solved
here).

Scheduling (which strategy ticks when) lives in deployment/schedule.yml,
read by scripts/scheduler_tick.py -- not here and not in Ofelia's own
config, which stays one static job forever (see docker-compose.yml).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from webapp.db import get_session
from webapp.models import Account, AccountStrategy, LogEntry, Position, Strategy, User
from webapp.schemas import LogEntryCreate, LogKind, LogLevel

from bot.s007_paper import run_cycle_for_account as run_s007_cycle
from utils.trade_logger import StrategyLogger
from webapp.state_store import DBStateStore

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TIMEOUT_S = 55.0
# Cap on the end-of-cycle position sync, and the floor below which it is
# skipped instead of started. Read-only and cheap (reconcile + one deal-list
# window + one symbol lookup, all in one session), so it does not need much --
# but it must not be what pushes a worker past the coordinator's kill line.
SYNC_BUDGET_S = 25.0
MIN_SYNC_BUDGET_S = 8.0


def _log_event(session, kind: LogKind, *, message: str | None = None,
               level: LogLevel = LogLevel.INFO, user: User | None = None,
               account: Account | None = None, strategy: Strategy | None = None,
               cycle_id: str | None = None, payload: dict | None = None) -> None:
    """Write one curated LogEntry row. Goes through webapp/schemas/logs.py
    first (same validation boundary the CLI/migration script use) so a bad
    kind/level fails loud here rather than as a later DB constraint surprise.
    `payload` must never contain decrypted credentials -- see logs.py."""
    validated = LogEntryCreate(
        level=level, kind=kind, message=message, payload=payload, cycle_id=cycle_id,
        user_id=user.id if user else None, account_id=account.id if account else None,
        strategy_id=strategy.id if strategy else None)
    entry = LogEntry(level=validated.level.value, kind=validated.kind.value,
                     message=validated.message, cycle_id=validated.cycle_id,
                     user_id=validated.user_id, account_id=validated.account_id,
                     strategy_id=validated.strategy_id)
    entry.payload = validated.payload
    session.add(entry)


def _open_position_db(session, acc: Account, strat: Strategy, a: dict) -> None:
    exists = session.query(Position).filter_by(
        account_id=acc.id, strategy_id=strat.id, label=a["label"], status="open").first()
    if exists:
        return
    session.add(Position(account_id=acc.id, strategy_id=strat.id, label=a["label"],
                         side=a["side"], entry=a["entry"], sl=a["sl"], tp=a["tp"],
                         is_add=a["is_add"], status="open"))


def _close_position_db(session, acc: Account, strat: Strategy, label: str, reason: str) -> None:
    pos = session.query(Position).filter_by(
        account_id=acc.id, strategy_id=strat.id, label=label, status="open").first()
    if pos:
        pos.status = "closed"
        pos.reason = reason
        pos.closed_at = datetime.now(timezone.utc)


def _worker_s007(link: AccountStrategy, session, budget_s: float | None) -> int:
    """Run one S007/CTRADER cycle for one (account, strategy) DB row.

    Fail-fast: a broker mismatch, or a credential error raised while
    constructing CTraderS007 (inside run_s007_cycle) crashes this process
    loudly (uncaught exception / SystemExit -> non-zero exit) rather than
    being swallowed -- same principle as bot/s007_paper.py's CTraderS007()
    construction comment. A cycle that runs but the BROKER rejects/errors on
    is different: that is caught inside run_s007_cycle and reported back as
    result["error"], recorded on the account_strategy row, with a clean (0)
    process exit -- a broker-side problem for one account is not a
    runner-crash.

    `budget_s` is the wall-clock this worker has before the coordinator kills
    it. Used only to decide how much time is left for the end-of-cycle
    position sync; None means "not launched by the coordinator" and falls
    back to SYNC_BUDGET_S.

    Short-circuits without opening a broker session at all once today's
    cycle already reached day_done/filtered/manual_stop -- mirrors
    scripts/s007_tick.py's loop_settled/loop_resumed guard, which this
    DB-driven path never had: unlike that legacy tick, it used to open a
    real cTrader/Twisted session on every single dispatcher tick for the
    rest of the session window even after the day's target was already
    reached. The marker is a plain `link.status` string ("settled:<local
    date>") -- no separate state file/table needed, and a new day's first
    tick naturally has a stale date and runs normally.
    """
    started = time.monotonic()
    account_strategy_id = link.id
    acc = link.account
    strat = link.strategy
    if acc.broker != "CTRADER":
        raise SystemExit(
            f"account_strategy {account_strategy_id} is broker={acc.broker} -- "
            f"the S007 worker only drives CTRADER accounts")
    user = acc.user

    # `datetime.now()` naive local, not UTC -- matches the session-day
    # boundary S007 itself uses (bot/s007_config.py's TRADE_START/EXIT_END
    # are local Kyiv time, same convention scripts/s007_tick.py's
    # in_session() and utils/trade_logger.StrategyLogger's `ts` both use).
    today_local = datetime.now().date().isoformat()
    if (link.status or "").startswith(f"settled:{today_local}"):
        print(f"[runner] account_strategy {account_strategy_id}: already settled today "
              f"({link.status}) -- skipping, no broker session opened")
        session.close()
        return 0

    creds_row = acc.credentials
    creds = dict(client_id=creds_row.get("client_id"), client_secret=creds_row.get("client_secret"),
                access_token=creds_row.get("access_token"),
                account_id=int(acc.external_account_id) if acc.external_account_id else None,
                host=acc.broker_host)

    logger = StrategyLogger(f"S007-acct{acc.external_account_id or acc.id}",
                            log_root=str(ROOT / "reports" / "logs"))
    preset = link.preset or strat.default_preset

    print(f"[runner] S007 cycle starting: account_strategy={link.id} account={acc.id} "
          f"({acc.label or acc.external_account_id}) user={user.username}")
    _log_event(session, LogKind.CYCLE_START, message="cycle start", user=user, account=acc,
              strategy=strat)
    session.commit()

    result = run_s007_cycle(
        creds, preset=preset, risk_pct=link.risk_pct, fixed_lot=link.fixed_lot,
        use_fixed_lot=link.use_fixed_lot, magic=strat.name, logger=logger)

    for a in result["actions"]:
        if a["kind"] == "open":
            _open_position_db(session, acc, strat, a)
            _log_event(session, LogKind.POSITION_OPEN, message=a["label"], user=user,
                      account=acc, strategy=strat, cycle_id=result.get("cycle_id"), payload=a)
        else:
            _close_position_db(session, acc, strat, a["label"], a["reason"])
            _log_event(session, LogKind.POSITION_CLOSE, message=a["label"], user=user,
                      account=acc, strategy=strat, cycle_id=result.get("cycle_id"), payload=a)

    ok = True
    if result["error"]:
        link.status = "error"
        link.last_error = result["error"]
        _log_event(session, LogKind.ERROR, level=LogLevel.ERROR, message=result["error"],
                  user=user, account=acc, strategy=strat, cycle_id=result.get("cycle_id"))
        print(f"[runner] account_strategy {link.id}: ERROR {result['error']}")
        ok = False
    else:
        n_actions = len(result["actions"])
        settled = bool(result.get("day_done") or result.get("filtered") or result.get("manual_stop"))
        if settled:
            link.status = f"settled:{today_local}"   # short-circuits every tick for the rest of today
        else:
            link.status = f"live: {n_actions} action(s)" if n_actions else "idle"
        link.last_error = None
        print(f"[runner] account_strategy {link.id}: {n_actions} action(s), "
              f"day_done={result.get('day_done')} in_window={result.get('in_window')}")
    link.last_cycle_at = datetime.now(timezone.utc)
    _log_event(session, LogKind.CYCLE_END, message="cycle end", user=user, account=acc,
              strategy=strat, cycle_id=result.get("cycle_id"),
              payload=dict(actions=len(result["actions"]), error=result["error"]))
    session.commit()
    session.close()

    # Refresh the DB from the broker now that the cycle is over, so the UI
    # reflects reality (fills, stops that fired between ticks, manual trades)
    # instead of only what this runner intended -- see webapp/sync_positions.py.
    #
    # Deliberately AFTER session.close() and outside the ok/error decision:
    #   * as a SUBPROCESS, because this process has already spent its Twisted
    #     reactor on the cycle and a reactor cannot be run twice
    #     (ReactorNotRestartable);
    #   * it can never change this worker's exit code. A broker hiccup while
    #     re-reading positions does not mean the trading cycle failed, and
    #     letting it flip the row to status='error' would be a lie about the
    #     thing that actually matters.
    _sync_after_cycle(account_strategy_id, started, budget_s)
    return 0 if ok else 1


def _worker_s009(link: AccountStrategy, session, budget_s: float | None) -> int:
    """Run one S009/BYBIT shadow cycle for one (account, strategy) DB row.

    Deliberately `broker="off"` (shadow-only) here, always -- enabling real
    demo/mainnet orders through this DB-driven path is a separate, explicit
    decision to make later, not a side effect of registering S009. No
    Position-table writes and no end-of-cycle sync: S009's "position" is a
    target-book weight, not a broker position id, and
    webapp/sync_positions.py is CTRADER-only by design (Bybit's positions()
    has no per-position id to reconcile a Position row against -- see that
    module's docstring).

    Per-account state (book/equity/last-booked-day) lives in the
    `strategy_state` DB table via DBStateStore, keyed by this
    account_strategy row -- so a second enabled S009 account never collides
    with the first the way the single shared reports/paper_s009/state.json
    file would.
    """
    from bot.s009_paper import run_cycle_for_account as run_s009_cycle, DEPLOY

    account_strategy_id = link.id
    acc = link.account
    strat = link.strategy
    if acc.broker != "BYBIT":
        raise SystemExit(
            f"account_strategy {account_strategy_id} is broker={acc.broker} -- "
            f"the S009 worker only drives BYBIT accounts")
    user = acc.user

    creds_row = acc.credentials
    creds = dict(api_key=creds_row.get("api_key"), api_secret=creds_row.get("api_secret"))
    logger = StrategyLogger(f"S009-acct{acc.id}", log_root=str(ROOT / "reports" / "logs"))
    state = DBStateStore(link.id, session)

    print(f"[runner] S009 cycle starting: account_strategy={link.id} account={acc.id} "
          f"({acc.label or acc.id}) user={user.username}")
    _log_event(session, LogKind.CYCLE_START, message="cycle start", user=user, account=acc,
              strategy=strat)
    session.commit()

    result = run_s009_cycle(
        account_key=acc.label or f"acct{acc.id}", creds=creds, cfg=DEPLOY, state=state,
        logger=logger, broker="off", allow_mainnet=False)

    ok = True
    if result["error"]:
        link.status = "error"
        link.last_error = result["error"]
        _log_event(session, LogKind.ERROR, level=LogLevel.ERROR, message=result["error"],
                  user=user, account=acc, strategy=strat)
        print(f"[runner] account_strategy {link.id}: ERROR {result['error']}")
        ok = False
    else:
        link.status = (f"shadow: {result['booked']} day(s) booked, "
                       f"{len(result['target'])} target position(s)")
        link.last_error = None
        print(f"[runner] account_strategy {link.id}: booked={result['booked']} "
              f"target={len(result['target'])} equity={result['equity']}")
    link.last_cycle_at = datetime.now(timezone.utc)
    _log_event(session, LogKind.CYCLE_END, message="cycle end", user=user, account=acc,
              strategy=strat, payload=dict(booked=result["booked"], error=result["error"]))
    session.commit()
    session.close()
    return 0 if ok else 1


# Strategy.name -> worker(link, session, budget_s) -> exit code. The only
# place a new strategy needs registering; run_worker()/run_coordinator()
# never need to change for one to be added.
STRATEGY_WORKERS = {
    "S007": _worker_s007,
    "S009": _worker_s009,
}


def run_worker(account_strategy_id: int, budget_s: float | None = None) -> int:
    """Load one (account, strategy) DB row and dispatch to its registered
    STRATEGY_WORKERS entry, in THIS process, then return an exit code. See
    the module docstring for why each cycle is its own OS process rather
    than a thread."""
    session = get_session()
    link = session.get(AccountStrategy, account_strategy_id)
    if link is None:
        raise SystemExit(f"account_strategy {account_strategy_id} not found")
    if not link.enabled:
        print(f"[runner] account_strategy {account_strategy_id} is disabled -- skipping")
        session.close()
        return 0

    worker_fn = STRATEGY_WORKERS.get(link.strategy.name)
    if worker_fn is None:
        raise SystemExit(
            f"account_strategy {account_strategy_id}: no runner worker registered for "
            f"strategy={link.strategy.name!r} (known: {sorted(STRATEGY_WORKERS)})")
    return worker_fn(link, session, budget_s)


def _sync_after_cycle(account_strategy_id: int, started: float,
                      budget_s: float | None) -> None:
    """Spend whatever is left of the tick budget on a position sync.

    Skipped rather than truncated when little time remains: a sync killed
    halfway is not a partial sync of the DB (apply_snapshot commits once, at
    the end) but simply a wasted subprocess, and it would leave the
    coordinator's kill landing on us instead. The next cycle syncs anyway.
    """
    from webapp.sync_positions import spawn_sync_worker   # local: avoids a
    # webapp.runner <-> webapp.sync_positions import cycle (sync_positions
    # imports _log_event from here).

    budget = SYNC_BUDGET_S if budget_s is None else (budget_s - (time.monotonic() - started))
    budget = min(budget, SYNC_BUDGET_S)
    if budget < MIN_SYNC_BUDGET_S:
        print(f"[runner] account_strategy {account_strategy_id}: skipping position "
              f"sync, only {budget:.0f}s of the tick budget left")
        return
    try:
        spawn_sync_worker(account_strategy_id, budget)
    except Exception as e:                       # noqa: BLE001 - see run_worker
        print(f"[runner] account_strategy {account_strategy_id}: position sync "
              f"failed: {e!r} (cycle result unaffected)")


def run_coordinator(strategy_name: str, timeout_s: float = DEFAULT_TIMEOUT_S) -> int:
    """One tick for `strategy_name` -- see module docstring for the subprocess
    fan-out rationale and the "one bad account != tick failure" exit-code
    contract."""
    session = get_session()
    strat = session.query(Strategy).filter_by(name=strategy_name).one_or_none()
    if strat is None:
        raise SystemExit(f"strategy '{strategy_name}' not found in DB -- seed it first "
                          f"(python -m webapp.cli add-strategy)")
    links = (session.query(AccountStrategy)
             .filter_by(strategy_id=strat.id, enabled=True).all())
    link_ids = [link.id for link in links]
    session.close()

    if not link_ids:
        print(f"[runner] no enabled account_strategy rows for '{strategy_name}' -- nothing to do")
        return 0

    print(f"[runner] {strategy_name}: fanning out {len(link_ids)} account(s), "
          f"budget={timeout_s:.0f}s")
    # --budget tells each worker how long it has before the kill below, so it
    # can decide whether the end-of-cycle position sync still fits.
    procs = {lid: subprocess.Popen(
        [sys.executable, "-m", "webapp.runner", "--worker",
         "--account-strategy-id", str(lid), "--budget", str(timeout_s)])
        for lid in link_ids}

    deadline = time.monotonic() + timeout_s
    results = {}
    for lid, p in procs.items():
        remaining = max(0.0, deadline - time.monotonic())
        try:
            results[lid] = p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            results[lid] = -9
            print(f"[runner] account_strategy {lid}: TIMEOUT after {timeout_s:.0f}s, killed")

    ok = sum(1 for rc in results.values() if rc == 0)
    print(f"[runner] {strategy_name}: {ok}/{len(link_ids)} account cycle(s) ok "
          f"({', '.join(f'{lid}={rc}' for lid, rc in results.items())})")

    # A killed/crashed worker never got to write its own status (a clean
    # in-process failure already set status='error' inside run_worker) --
    # mark those here so the DB never shows a stale "idle"/last good status
    # after a timeout or hard crash.
    failed = {lid: rc for lid, rc in results.items() if rc != 0}
    if failed:
        session = get_session()
        for lid, rc in failed.items():
            link = session.get(AccountStrategy, lid)
            if link and link.status != "error":
                link.status = "error"
                link.last_error = f"worker exited rc={rc} (killed/crashed before self-reporting)"
        session.commit()
        session.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strategy", default="S007",
                    help="strategy name to run this tick (coordinator mode, default S007)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help=f"per-tick budget in seconds before killing stragglers "
                         f"(coordinator mode, default {DEFAULT_TIMEOUT_S:.0f})")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--account-strategy-id", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--budget", type=float, default=None, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.worker:
        if a.account_strategy_id is None:
            raise SystemExit("--worker requires --account-strategy-id")
        sys.exit(run_worker(a.account_strategy_id, budget_s=a.budget))
    sys.exit(run_coordinator(a.strategy, timeout_s=a.timeout))


if __name__ == "__main__":
    main()
