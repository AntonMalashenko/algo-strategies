"""ALGODEV-45: S007 persistent-session daemon (observation -> paper trading).

Connects to the SAME account S007 already trades (ctrader-47939312, via the
SAME DB-stored credentials webapp/runner.py uses -- accounts.credentials_enc,
account id per --account-id below) and keeps that ONE session open
indefinitely, polling for symbol/balance/M1 bars/positions/closing deals
every tick_interval_s seconds -- the exact same broker calls a real cycle
makes, on the exact same account, just without reconnecting each time.

TWO MODES:
  default   -- step 2's pure observer: fetch, log the summary, decide nothing.
  --paper   -- step 3: run the REAL decision step (bot.s007_paper.make_decide,
               the identical closure the live order-placing path uses) against
               a VIRTUAL book (bot.s007_paper_book.PaperBook) that stands in
               for the broker: it holds the positions, honours their
               server-side SL/TP, and keeps its own balance. Trades are
               recorded to reports/logs/S007-paper/ and nowhere else.

SAFETY INVARIANT (do not weaken without discussing with Anton first): this
process places NO broker order in EITHER mode. It only ever calls
CTraderS007.shadow_tick_step, which is read-only by construction (see that
method's own docstring in bot/ctrader_s007.py); in --paper mode every write
goes to the virtual book instead of the wire. This is asserted by
tests/test_s007_paper_book.py::test_paper_modules_cannot_send_orders rather
than left to code review, because "there's no such call" stops being true the
moment someone adds an import. The real, order-placing path
(webapp.runner -> bot.s007_paper.run_cycle_for_account ->
CTraderS007.run_live_cycle) is completely untouched by this file and keeps
running exactly as before, dispatched by Ofelia every minute as it already
is -- this daemon is a SEPARATE, parallel, non-trading process, not a
replacement (that only happens at step 4 of ALGODEV-45, and only after this
step is proven and the old path is explicitly turned off first, never both
placing orders at once).

What this is actually proving: (step 2) does holding one cTrader session open
and reusing it, tick after tick, for hours, work without hanging or leaking,
and does the daemon correctly notice an operator's config change (Stop /
preset) via its own periodic DB read; (step 3) does the shared decision step
behave identically when driven by that persistent session, and how far does
its paper P&L sit from the live account's -- the gap IS the slippage and
fill-quality cost, which nothing else measures.

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
    python -m scripts.s007_daemon --account-strategy-id 1 [--paper]

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

# ALGODEV-45 step 3: where the paper daemon's virtual book is persisted. Under
# data/ (a bind mount on the compose service) so the book survives the
# container restarts `restart: unless-stopped` will inevitably cause -- a book
# that reset on every crash would fake a clean slate and bias the results.
PAPER_BOOK_PATH = ROOT / "data" / "s007_paper_book.json"
PAPER_STRATEGY_NAME = "S007-paper"

# Renew the cTrader access token at startup if it would expire within this
# window (ALGODEV-48): the persistent session lives for hours, so the per-
# minute workers' leeway is far too short here. One day covers a session.
DAEMON_TOKEN_LEEWAY_S = 24 * 3600


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
        # ALGODEV-48: central, locked refresh with a DAY-long leeway -- this
        # process opens one session and keeps it, so the token must outlive
        # the session, not just the next minute. A restart (container
        # `restart: unless-stopped`) re-runs this and picks up a fresh pair.
        from webapp.ctrader_tokens import fresh_ctrader_creds
        return dict(
            creds=fresh_ctrader_creds(session, acc, leeway_s=DAEMON_TOKEN_LEEWAY_S),
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
    ap.add_argument("--paper", action="store_true",
                    help="ALGODEV-45 step 3: run the strategy for real against a "
                         "VIRTUAL book (own positions/balance/risk budget) and "
                         "record the trades to the log. Still places no broker "
                         "order of any kind -- see the module docstring's SAFETY "
                         "INVARIANT. Without this flag the daemon stays the "
                         "step-2 read-only observer.")
    ap.add_argument("--paper-balance", type=float, default=None,
                    help="Starting balance for a BRAND-NEW paper book. Ignored "
                         "once a book exists (a restored book keeps its own "
                         "balance). Defaults to the real account's balance on "
                         "the first tick.")
    args = ap.parse_args()

    from bot.ctrader_s007 import CTraderS007
    from bot import s007_config as C
    from bot.s007_paper_book import PaperBook
    from bot.s007_paper_cycle import run_paper_cycle
    from utils.trade_logger import StrategyLogger
    from scripts.s007_watchdog import expected_to_run

    startup = _load_creds_and_config(args.account_strategy_id)
    strategy_name = PAPER_STRATEGY_NAME if args.paper else "S007-daemon-shadow"
    logger = StrategyLogger(strategy_name, log_root=str(ROOT / "reports" / "logs"))
    logger.event("daemon_start", account_strategy_id=args.account_strategy_id,
                account=startup["account_label"], tick_interval_s=args.tick_interval,
                mode="paper" if args.paper else "shadow")

    client = CTraderS007(creds=startup["creds"])

    # Loaded lazily on the first tick: a brand-new book needs the broker's
    # balance and lotSize, which are not known until a read comes back.
    paper = {"book": None}

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

        def ok(payload):
            # The raw market data rides along with the summary (see
            # shadow_tick_step) but must not reach the log: a DataFrame in
            # events-<date>.jsonl would be unreadable and enormous.
            m1 = payload.pop("m1", None)
            payload.pop("positions", None)
            payload.pop("closed_deals", None)
            logger.event("daemon_tick_ok", **payload, preset=cfg["preset"])
            if args.paper:
                _paper_tick(payload, m1, cfg["preset"])

        def err(failure):
            logger.error("daemon tick failed", exc=failure.value)

        d.addCallbacks(ok, err)

    def _paper_tick(summary, m1, preset):
        """Trade the cycle against the virtual book. Wrapped in its own
        try/except because a paper bookkeeping bug must never take down the
        session this daemon exists to keep alive -- the connection endurance
        result (step 2) stays valid even if a paper cycle fails."""
        try:
            book = paper["book"]
            if book is None:
                start_balance = (args.paper_balance if args.paper_balance is not None
                                 else summary["balance"])
                book = PaperBook.load(
                    PAPER_BOOK_PATH, balance=start_balance,
                    # Account currency: the broker's lotSize is in the QUOTE
                    # currency (EUR for DE40) but the balance is in USD, and
                    # the book books realised P&L straight onto that balance.
                    # decide() gets the RAW value below and converts itself.
                    money_per_point_per_lot=summary["lot_size"] * C.EUR_TO_USD_FX_RATE_APPROX)
                paper["book"] = book
                logger.event("paper_book_loaded", balance=round(book.balance, 2),
                             open_positions=len(book.positions),
                             restored=PAPER_BOOK_PATH.exists())
            out = run_paper_cycle(book, symbol=summary["symbol"], m1=m1,
                                  logger=logger, preset=preset,
                                  money_per_point_per_lot=summary["lot_size"],
                                  initial_balance=book.initial_balance,
                                  broker_min_lot=summary.get("broker_min_lot"))
            book.save(PAPER_BOOK_PATH)
            return out
        except Exception as exc:  # noqa: BLE001 -- see docstring
            logger.error("paper cycle failed", exc=exc)
            return None

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
