"""ALGODEV-45 step 4: S007 LIVE daemon -- one cTrader session for the whole
trading session, placing REAL orders.

This is the step-2/3 daemon's sibling, and the first one that trades. The
difference from scripts/s007_daemon.py is only the SAFETY INVARIANT: that one
can never send an order (asserted by a test), this one is the live path and
is meant to.

WHAT IT REPLACES
Until now S007 ran as a stateless tick: Ofelia dispatched
`python -m webapp.runner --strategy S007` every minute, which fanned out one
subprocess per account, and each subprocess opened a fresh cTrader TCP
connection, did the app-auth + account-auth handshake, traded, and tore the
connection down again -- paying that handshake ~400 times per session for
~7 hours of trading. ALGODEV-44 measured where a cycle's seconds went and the
answer was "the connection and the SDK's send queue, not the strategy". This
daemon pays the handshake once.

WHAT IT DELIBERATELY DOES NOT CHANGE
Nothing about the trading rules, the risk sizing, the logging or the DB
bookkeeping. Every tick calls webapp.runner::_worker_s007 -- the exact
function Ofelia's per-minute worker calls -- with the already-connected
client injected. That is the whole design: one implementation of the cycle,
two owners of the socket. If this file ever starts to contain strategy logic,
the split has been done wrong.

HOW THE SYNC/ASYNC SEAM WORKS
The cTrader SDK is Twisted, so the persistent session lives on a reactor
running in this process's MAIN thread. _worker_s007 and everything under it
(bot/s007_paper.py, the DB writes) is ordinary blocking code. Each tick is
therefore run in a reactor WORKER THREAD (reactor.callInThread), and the one
place that needs the reactor -- CTraderS007.run_live_cycle -- hands the work
back with threads.blockingCallFromThread. So the strategy code never learns
it is inside a daemon.

MUTUAL EXCLUSION (the thing that must not go wrong)
Two processes trading one account would double its size. This daemon takes
webapp.runner.cycle_lock for its ENTIRE run, so any Ofelia-dispatched worker
that fires at the same minute finds the lock held and skips. Comment the
S007 job out of deployment/schedule.yml at cutover anyway -- the lock is the
backstop, not the plan.

NO AUTO-RECONNECT, BY DESIGN (inherited from step 2)
A dropped connection stops the process. The container's `restart:
unless-stopped` then brings up a FRESH process with a fresh session and a
fresh token, which is a far simpler thing to reason about than an in-process
retry on a half-dead socket. State that matters survives this: open positions
live at the broker, the day's dedup history lives in reports/logs/, and
"today is already settled" lives in AccountStrategy.status.

WINDOW-AWARE
Real work only inside S007's own trading window (scripts.s007_watchdog
::expected_to_run, the same Kyiv weekday window deployment/schedule.yml's
S007 cron used). Outside it the session stays open but no broker call is
made.

Usage:
    python -m scripts.s007_live_daemon --account-strategy-id 1
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.s007_daemon import (  # noqa: E402 -- after sys.path setup
    DAEMON_TOKEN_LEEWAY_S, DEFAULT_TICK_INTERVAL_S, _load_creds_and_config,
    _refresh_config,
)

LIVE_STRATEGY_NAME = "S007-live-daemon"

# How long a clean shutdown gets before this process kills itself.
#
# reactor.stop() joins the thread pool, and a cycle thread can be parked in
# threads.blockingCallFromThread waiting on a Deferred that a just-dropped
# session will never fire -- so the "clean" shutdown would hang forever,
# holding the trading lock, with `restart: unless-stopped` never getting its
# chance because the process never actually exits. A live bot that is neither
# trading nor restarting is the worst of the three states, so past this grace
# the process exits hard (non-zero, so the container comes back; the kernel
# drops the flock with the process).
HARD_EXIT_GRACE_S = 20.0


def _hard_exit_after_grace() -> None:
    """Arm the backstop above. Daemon timer, so it never keeps an otherwise
    finished process alive."""
    t = threading.Timer(HARD_EXIT_GRACE_S, lambda: os._exit(1))
    t.daemon = True
    t.start()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-strategy-id", type=int, required=True)
    ap.add_argument("--tick-interval", type=float, default=DEFAULT_TICK_INTERVAL_S)
    args = ap.parse_args()

    from bot.ctrader_s007 import CTraderS007
    from utils.trade_logger import StrategyLogger
    from webapp.runner import cycle_lock

    startup = _load_creds_and_config(args.account_strategy_id)
    logger = StrategyLogger(LIVE_STRATEGY_NAME, log_root=str(ROOT / "reports" / "logs"))

    with cycle_lock(args.account_strategy_id) as held:
        if not held:
            # Another daemon, or a worker mid-cycle. Refusing to start is the
            # only safe answer: see MUTUAL EXCLUSION in the module docstring.
            logger.event("live_daemon_refused", reason="cycle lock already held",
                         account_strategy_id=args.account_strategy_id)
            raise SystemExit(
                f"account_strategy {args.account_strategy_id} is already being traded "
                f"by another process -- refusing to start a second one")

        logger.event("live_daemon_start", account_strategy_id=args.account_strategy_id,
                     account=startup["account_label"], tick_interval_s=args.tick_interval,
                     token_leeway_s=DAEMON_TOKEN_LEEWAY_S)

        # The credentials are fixed for this process's lifetime: _load_creds_and_config
        # already renewed the access token with a DAY-long leeway, which outlives one
        # trading session. A restart picks up a fresh pair.
        client = CTraderS007(creds=startup["creds"])
        _run_loop(client, args, logger)


def _run_loop(client, args, logger) -> None:
    """Hold the session open and fire one cycle per tick until stopped."""
    from twisted.internet import reactor, task

    from scripts.s007_watchdog import expected_to_run
    from webapp.db import get_session
    from webapp.models import AccountStrategy
    from webapp.runner import _worker_s007

    # None until the first tick, so the very first tick always logs its window
    # state once instead of only on a transition (a daemon started outside the
    # window would otherwise stay silent forever).
    window_state = {"active": None}
    # One tick at a time. A cycle normally takes a few seconds, but a slow
    # broker response must never let tick N+1 start while tick N is still
    # deciding -- both would read the same positions and could place the same
    # entry twice. Plain dict flag rather than a Lock: it is only ever written
    # from the reactor thread (set) and the worker thread (clear), never
    # read-modify-written concurrently.
    busy = {"now": False}

    def one_tick():
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)  # naive, matches expected_to_run
        active = expected_to_run(now_utc)
        if active != window_state["active"]:
            logger.event("live_daemon_window_change", active=active)
            window_state["active"] = active
        if not active:
            return
        if busy["now"]:
            logger.event("live_daemon_tick_skipped", reason="previous tick still running")
            return
        cfg = _refresh_config(args.account_strategy_id)
        if not cfg["enabled"]:
            # The operator pressed Stop in the web panel. Same contract the
            # stateless workers honour: the DB is read live every tick, so a
            # config change lands within one tick without a restart.
            logger.event("live_daemon_tick_skipped", reason="account_strategy disabled")
            return
        busy["now"] = True
        reactor.callInThread(run_one_cycle)

    def run_one_cycle():
        """One full S007 cycle, in a reactor worker thread.

        Deliberately the SAME function the Ofelia worker runs -- see the
        module docstring. Everything is caught: a failed cycle must not take
        down the session, because the next tick is a minute away and a live
        account with open positions needs this process alive to manage them.
        BaseException too, specifically for the SystemExit _worker_s007 raises
        on a misconfigured row -- in a thread that would otherwise vanish
        silently.
        """
        session = None
        try:
            session = get_session()
            link = session.get(AccountStrategy, args.account_strategy_id)
            if link is None:
                logger.error("account_strategy row disappeared")
                return
            # budget_s=None: there is no coordinator kill-line here, so the
            # end-of-cycle position sync gets webapp.runner's full default.
            _worker_s007(link, session, None, client)
        except BaseException as exc:  # noqa: BLE001 -- see docstring
            logger.error("live daemon cycle failed", exc=exc)
            if session is not None:
                try:
                    session.rollback()
                    session.close()
                except Exception:
                    pass
        finally:
            busy["now"] = False

    def on_ready():
        logger.event("live_daemon_ready", text="session up, starting periodic ticks")
        task.LoopingCall(one_tick).start(args.tick_interval, now=True)

    def on_disconnected(reason):
        logger.error("live daemon session disconnected -- stopping (no auto-reconnect "
                     "by design, the container restart brings up a fresh session)",
                     exc=reason.value if hasattr(reason, "value") else None)
        _hard_exit_after_grace()

    def handle_signal(signum, _frame):
        logger.event("live_daemon_stopping", signal=signum)
        _hard_exit_after_grace()
        client.stop_persistent()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        client._run_persistent(on_ready, on_disconnected=on_disconnected)
    finally:
        logger.event("live_daemon_stopped", text="reactor exited")


if __name__ == "__main__":
    main()
