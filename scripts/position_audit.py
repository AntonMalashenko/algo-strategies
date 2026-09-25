"""Pre-session stale-position audit for intraday-only cTrader strategies.

Found live 2026-09-24: S021 left a position open overnight (2026-09-23's
23:59 Kyiv cycle -- the LAST one in that day's cron window -- was 2 minutes
short of its own 15:59 fixed-EST time-exit; see bot/orb_config.py::
LIVE_EXIT_BUFFER_MIN for the same-day fix to that root cause). Worse: once
the next day's session recomputes "today"'s labels, decide() can no longer
recognize a PRIOR day's label at all, so it will never close it on its own
-- an intraday strategy's own code has no path back to a stale position once
the date rolls over. Anton's explicit ask, 2026-09-24: (1) close 10 minutes
earlier live (done, bot/orb_config.py/bot/orb_signals.py), (2) audit each
strategy's positions before its own session opens and close anything that
mismatches expectations.

Scope: S007 and S021 only -- both are DESIGNED to be flat outside their own
session window (S007: bot/s007_config.py TRADE_START/EXIT_END; S021: fixed
09:30-15:59 EST). S009/S011 are explicitly NOT in scope: both are
multi-day/portfolio strategies that are SUPPOSED to carry positions across
days (S009's daily-rebalanced book, S011's RSI(2) holds until its own exit
signal) -- an "open position before session start" is their normal resting
state, not a defect. Do not add either here without discussing the
"what counts as stale" definition for a multi-day strategy first (it is NOT
"any open position", unlike S007/S021).

Invariant this relies on instead of date/timezone arithmetic (deliberately
simpler and more robust than parsing each label's own date and comparing to
"today" in that strategy's own clock, which would need Europe/Bucharest for
S007 vs a fixed UTC-5 offset for S021): this script is scheduled to run
STRICTLY BEFORE each strategy's own session opens each day (see
deployment/schedule.yml's audit task entries) -- so ANY open position found
at that moment cannot belong to a session that has not started yet, and is
therefore stale by construction, regardless of what label it carries or
what calendar date that label encodes.

Usage (manual, one-off):
    python -m scripts.position_audit --account-strategy-id 1   # S007
    python -m scripts.position_audit --account-strategy-id 5   # S021
Normal use is scheduled (see deployment/schedule.yml), not manual.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.trade_logger import StrategyLogger  # noqa: E402


def audit_account_strategy(account_strategy_id: int) -> int:
    """Read every open position on this (account, strategy)'s broker account
    and close whatever is still open -- see module docstring for why "any
    open position at audit time" is a safe stale-position test here.
    Never raises: a broker/DB failure is logged and reported via the return
    code, same "never crash a scheduled task silently" contract as
    scripts/podman_healthcheck.py.

    Returns the count of positions closed (0 on a clean pre-session state,
    which is the expected/normal outcome every day this bug doesn't recur)."""
    from webapp.db import get_session
    from webapp.models import AccountStrategy

    session = get_session()
    try:
        link = session.get(AccountStrategy, account_strategy_id)
        if link is None:
            raise SystemExit(f"account_strategy {account_strategy_id} not found")
        strategy_name = link.strategy.name
        if strategy_name not in ("S007", "S021"):
            raise SystemExit(
                f"account_strategy {account_strategy_id} is strategy {strategy_name!r} -- "
                f"this audit only covers S007/S021 (see module docstring: S009/S011 are "
                f"supposed to hold positions across days, 'any open position' is not a "
                f"valid staleness test for them)")
        acc = link.account
        creds_row = acc.credentials
        creds = dict(client_id=creds_row.get("client_id"), client_secret=creds_row.get("client_secret"),
                    access_token=creds_row.get("access_token"),
                    account_id=int(acc.external_account_id) if acc.external_account_id else None,
                    host=acc.broker_host)
        account_label = acc.label or acc.external_account_id
        # Must match webapp/runner.py's own StrategyLogger group key EXACTLY
        # (f"{strategy}-acct{external_account_id or id}", see _worker_s007/
        # _worker_orb) so this audit's log lines land in the SAME directory
        # the live worker already writes to, not a second, disconnected one
        # keyed by acc.label instead (found live 2026-09-24: the first two
        # scheduled runs silently wrote into new S007-acctctrader-47939312/
        # S021-acctctrader-47939312 directories, fragmenting the audit
        # trail away from reports/logs/S007-acct47939312/ etc).
        log_key = acc.external_account_id or acc.id
    finally:
        session.close()

    logger = StrategyLogger(f"{strategy_name}-acct{log_key}",
                            log_root=str(ROOT / "reports" / "logs"))

    if strategy_name == "S007":
        from bot.ctrader_s007 import CTraderS007
        client = CTraderS007(creds=creds)
    else:
        from bot.ctrader_orb import CTraderORB
        client = CTraderORB(creds=creds)

    # Ownership filter (Anton, 2026-09-24: "если позиции будут открыты
    # другой торговой стратегией то их закрывать не стоит") -- S007 and
    # S021 share the SAME cTrader account (ctrader-47939312), so a naive
    # "close every open position" here would reach across and close the
    # OTHER strategy's position too. Every label this codebase writes is
    # "<MAGIC>:<date>:<...>" (bot/orb_signals.py::_today_label and the
    # equivalent S007 construction) -- only touch positions whose label
    # starts with THIS strategy's own name. A position with no matching
    # prefix is left alone AND logged loudly (not silently skipped) so a
    # human still sees it -- it might be a manual test position, a
    # not-yet-audited third strategy sharing this account later, or a
    # labeling bug elsewhere; this script's job is only to clean up ITS
    # OWN strategy's leftovers, never to guess about anyone else's.
    own_prefix = f"{strategy_name}:"

    cid = logger.cycle_start(mode="position-audit", account=account_label)
    closed = 0
    try:
        positions = client.open_positions()
        own, foreign = [], []
        for p in positions:
            (own if p["label"].startswith(own_prefix) else foreign).append(p)
        if foreign:
            logger.error(
                f"{strategy_name} pre-session audit: {len(foreign)} open position(s) on this "
                f"account do NOT belong to {strategy_name} ({[p['label'] for p in foreign]}) "
                f"-- NOT touching them, flagging for a human look",
                exc=RuntimeError("foreign position found during audit"), cycle=cid)
        if not own:
            logger.event("position_audit_clean", cycle=cid, symbol=strategy_name)
        for p in own:
            logger.error(
                f"{strategy_name} pre-session audit: position {p['label']!r} "
                f"(position_id={p['position_id']}) still open before today's session -- "
                f"closing (see scripts/position_audit.py's module docstring for why this "
                f"can happen and why 'still open at audit time' is treated as stale)",
                exc=RuntimeError("stale pre-session position"), cycle=cid)
            try:
                res = client.close_position(p["position_id"], p["volume"])
                logger.order(p["label"], "close_position", cycle=cid,
                            request=dict(position_id=p["position_id"], volume=p["volume"]),
                            result=res)
                logger.position(p["label"], "close", cycle=cid, reason="stale_position_audit")
                closed += 1
            except Exception as exc:
                logger.error(f"{strategy_name} audit: failed to close stale position "
                            f"{p['label']!r}", exc=exc, cycle=cid)
    except Exception as exc:
        logger.error(f"{strategy_name} position audit itself failed", exc=exc, cycle=cid)
    logger.cycle_end(cid, status=f"audit: {closed} stale position(s) closed")
    return closed


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--account-strategy-id", type=int, required=True)
    args = ap.parse_args()
    n = audit_account_strategy(args.account_strategy_id)
    print(f"account_strategy {args.account_strategy_id}: {n} stale position(s) closed")
