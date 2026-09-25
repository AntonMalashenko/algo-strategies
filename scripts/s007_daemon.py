"""ALGODEV-45 step 2: S007 persistent-session OBSERVATION daemon.

Connects to the SAME account S007 already trades (ctrader-47939312, via the
SAME DB-stored credentials webapp/runner.py uses -- accounts.credentials_enc,
account id per --account-id below) and keeps that ONE session open
indefinitely, polling for symbol/balance/M1 bars/positions/closing deals
every tick_interval_s seconds -- the exact same broker calls a real cycle
makes, on the exact same account, just without reconnecting each time.

SAFETY INVARIANT (do not weaken without discussing with Anton first): this
process NEVER calls decide() and has NO code path to
place_market/amend/close anything. It only calls
CTraderS007.shadow_tick_step, which is read-only by construction (see that
method's own docstring in bot/ctrader_s007.py). The real, order-placing
path (webapp.runner -> bot.s007_paper.run_cycle_for_account ->
CTraderS007.run_live_cycle) is completely untouched by this file and keeps
running exactly as before, dispatched by Ofelia every minute as it already
is -- this daemon is a SEPARATE, parallel, non-trading observer, not a
replacement (that only happens at step 4 of ALGODEV-45, and only after this
step is proven and the old path is explicitly turned off first, never both
placing orders at once).

What this step is actually proving: does holding one cTrader session open
and reusing it, tick after tick, for hours, work without hanging or leaking
-- and does the daemon correctly notice an operator's config change (Stop /
preset) via its own periodic DB read, not just at startup.

WINDOW-AWARE (Anton, 2026-09-17: "пусть работает по графику стратегии"):
only does real work (the broker fetch) during S007's own trading window
(reuses scripts.s007_watchdog.expected_to_run -- same Kyiv 10-16 weekday
window as deployment/schedule.yml's S007 cron). Outside that window the
session stays OPEN (that overnight/weekend idle-connection endurance is
itself part of what this step needs to prove) but this process does not
hit the broker at all -- no point burning API calls or log volume for a
window nothing is supposed to happen in.

Containerized 2026-09-23 as the `s007-daemon` docker-compose service
(restart: unless-stopped -- see that service's own comment for why this
does not contradict the "no auto-reconnect" invariant above: a container
restart is a fresh process/session, not an in-process retry). Still not in
deployment/schedule.yml -- that file is Ofelia's per-strategy CRON dispatch
for stateless ticks, which this isn't (one long-lived process, not a fresh
container per minute).

Usage (manual, e.g. for a one-off run outside the compose service):
    python -m scripts.s007_daemon --account-strategy-id 1

Ctrl+C (SIGINT) or SIGTERM triggers a clean shutdown (stops the internal
LoopingCall, closes the session, exits) -- see _install_signal_handlers.
"""
from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_TICK_INTERVAL_S = 60


def _load_creds_and_config(account_strategy_id: int) -> dict:
    """One read of everything needed to start: broker credentials (fixed
    for the process lifetime -- see module docstring's known-limitation
    note) plus the initial enabled/preset snapshot. Re-checked every tick
    by _refresh_config below, same DB, same fields."""
    from webapp.db import get_session
    from webapp.models import AccountStrategy

    session = get_session()
    try:
        link = session.get(AccountStrategy, account_strategy_id)
        if link is None:
            raise SystemExit(f"account_strategy {account_strategy_id} not found")
        if link.strategy.name != "S007":
            raise SystemExit(
                f"account_strategy {account_strategy_id} is strategy "
                f"{link.strategy.name!r}, not S007 -- this daemon is S007-only")
        acc = link.account
        creds_row = acc.credentials
        return dict(
            creds=dict(client_id=creds_row.get("client_id"),
                      client_secret=creds_row.get("client_secret"),
                      access_token=creds_row.get("access_token"),
                      account_id=int(acc.external_account_id) if acc.external_account_id else None,
                      host=acc.broker_host),
            account_label=acc.label or acc.external_account_id,
            enabled=link.enabled,
            preset=link.preset,
        )
    finally:
        session.close()


def _refresh_config(account_strategy_id: int) -> dict:
    """Re-read the parts of the DB config that CAN change while this daemon
    is running (enabled/preset/status) -- called every internal tick, not
    just at startup. This is the same "UI writes to the DB, the runner
    reads it live every cycle" contract webapp/runner.py's stateless workers
    already follow (Stop / risk-pct changes from the web panel must reach a
    running cycle within one tick, not require a restart) -- a persistent
    daemon must keep that contract too, not silently freeze it at whatever
    the config was when the process started."""
    from webapp.db import get_session
    from webapp.models import AccountStrategy

    session = get_session()
    try:
        link = session.get(AccountStrategy, account_strategy_id)
        if link is None:
            return dict(enabled=False, preset=None)
        return dict(enabled=link.enabled, preset=link.preset)
    finally:
        session.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-strategy-id", type=int, required=True)
    ap.add_argument("--tick-interval", type=float, default=DEFAULT_TICK_INTERVAL_S)
    args = ap.parse_args()

    from bot.ctrader_s007 import CTraderS007
    from bot import s007_config as C
    from utils.trade_logger import StrategyLogger
    from scripts.s007_watchdog import expected_to_run

    startup = _load_creds_and_config(args.account_strategy_id)
    logger = StrategyLogger("S007-daemon-shadow", log_root=str(ROOT / "reports" / "logs"))
    logger.event("daemon_start", account_strategy_id=args.account_strategy_id,
                account=startup["account_label"], tick_interval_s=args.tick_interval)

    client = CTraderS007(creds=startup["creds"])

    from twisted.internet import reactor, task

    # None until the first tick decides either way -- lets the very first
    # tick always log its window state once, instead of only logging on a
    # TRANSITION (which would stay silent forever if the daemon happens to
    # start already outside the window, e.g. started in the evening for an
    # overnight idle-connection soak per Anton's 2026-09-17 request).
    window_state = {"active": None}

    def one_tick():
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, matches expected_to_run
        active = expected_to_run(now_utc)
        if active != window_state["active"]:
            logger.event("daemon_window_change", active=active)
            window_state["active"] = active
        if not active:
            # Session stays open (that's the whole point -- see module
            # docstring's WINDOW-AWARE note) but no broker call outside
            # S007's own trading window: nothing to observe there that
            # isn't already covered by "the session is still connected".
            return

        cfg = _refresh_config(args.account_strategy_id)
        if not cfg["enabled"]:
            logger.event("daemon_tick_skipped", reason="account_strategy disabled")
            return
        d = client.shadow_tick_step(C.SYMBOL_CANDIDATES, C.HISTORY_DAYS)

        def ok(summary):
            logger.event("daemon_tick_ok", **summary, preset=cfg["preset"])

        def err(failure):
            logger.error("daemon tick failed", exc=failure.value)

        d.addCallbacks(ok, err)

    def on_ready():
        logger.event("daemon_ready", text="session up, starting periodic ticks")
        looper = task.LoopingCall(one_tick)
        looper.start(args.tick_interval, now=True)  # first tick immediately, proves the session works right away

    def on_disconnected(reason):
        logger.error("daemon session disconnected -- stopping (no auto-reconnect by design)",
                     exc=reason.value if hasattr(reason, "value") else None)

    def handle_signal(signum, _frame):
        logger.event("daemon_stopping", signal=signum)
        client.stop_persistent()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        client._run_persistent(on_ready, on_disconnected=on_disconnected)
    finally:
        logger.event("daemon_stopped", text="reactor exited")


if __name__ == "__main__":
    main()
