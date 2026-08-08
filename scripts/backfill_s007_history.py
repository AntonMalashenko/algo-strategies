"""One-off backfill: import historical S007 position/error events from
reports/logs/S007/events-*.jsonl into the webapp DB (Position + LogEntry),
for the account/strategy pair created by scripts/migrate_accounts_yml.py.
Also sets Strategy.description for S007/S009 (short human-readable blurb --
until now only the code name existed, e.g. Strategy.name == "S007").

Why not a straight replay: two known contamination sources in these files,
filtered out here rather than imported:
  - label containing "2024-05-10" -- the fixture date used by
    `bot/s007_paper.py --dry-run --at "2024-05-10 ..."`; a handful of
    "open"/mode=live position events with this exact date exist in the SAME
    log files, from an earlier test/dev run that pointed StrategyLogger at
    the production log directory instead of a temp one (separate hygiene
    issue, not fixed here -- worth a follow-up so tests stop writing into
    reports/logs/S007/).
  - `cycle: null` position events -- same contamination source; no real
    cycle_start ever produced them.

Because the file logs record a bot-initiated "close" (day target / end of
session / manual stop) but NOT a broker-side stop-loss/take-profit fill
(those just stop appearing in the next reconcile -- see decide()'s
"broker_side_close_detected" backfill logic in bot/s007_paper.py), most
historical "open" events here have no matching "close" event on file. S007
flattens everything by end-of-session every trading day (EXIT_END=16:59), so
anything from a day before today is certainly closed by now; today's
positions may still be genuinely open. Rule applied per label:
  - an explicit "close" event on file -> closed, with its logged reason
  - "open" only, label's date < today -> closed, reason="historical_import"
    (the exact fill/exit price is NOT recoverable from these logs -- this is
    for populating the UI/audit trail with realistic history, not for P&L
    reconstruction)
  - "open" only, label's date == today -> left status="open"

Re-running is safe for positions (skips any label that already has a
Position row for this account/strategy) and for errors (skips the whole
error import if any ERROR LogEntry already exists for this account/strategy).

Usage:
    python -m scripts.backfill_s007_history
"""
from __future__ import annotations

import glob
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from webapp.db import get_session  # noqa: E402
from webapp.models import Account, AccountStrategy, LogEntry, Position, Strategy  # noqa: E402
from webapp.schemas import LogEntryCreate, LogKind, LogLevel  # noqa: E402

LOG_DIR = ROOT / "reports" / "logs" / "S007"
BAD_LABEL_DATE = "2024-05-10"   # bot/s007_paper.py --dry-run fixture date

STRATEGY_DESCRIPTIONS = {
    "S007": "GER40 (DAX) London x Frankfurt breakout with pyramiding, M1 intraday.",
    "S009": "Crypto funding-carry (crowding-reversal), Bybit perps, daily rebalance, market-neutral.",
}


def _label_date(label: str) -> date | None:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", label)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d").date()


def _iter_events(kind: str):
    for fn in sorted(glob.glob(str(LOG_DIR / "events-*.jsonl"))):
        for line in open(fn, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("kind") != kind:
                continue
            if kind == "position":
                label = str(r.get("label", ""))
                if BAD_LABEL_DATE in label or r.get("cycle") is None:
                    continue
            yield r


def _log_event(session, kind: LogKind, *, message: str | None = None,
               level: LogLevel = LogLevel.INFO, account: Account | None = None,
               strategy: Strategy | None = None, cycle_id: str | None = None,
               payload: dict | None = None) -> None:
    v = LogEntryCreate(level=level, kind=kind, message=message, payload=payload,
                       cycle_id=cycle_id, account_id=account.id if account else None,
                       strategy_id=strategy.id if strategy else None)
    entry = LogEntry(level=v.level.value, kind=v.kind.value, message=v.message,
                     cycle_id=v.cycle_id, account_id=v.account_id, strategy_id=v.strategy_id)
    entry.payload = v.payload
    session.add(entry)


def backfill():
    session = get_session()

    for name, desc in STRATEGY_DESCRIPTIONS.items():
        st = session.query(Strategy).filter_by(name=name).one_or_none()
        if st and not st.description:
            st.description = desc
            print(f"  set {name}.description")
    session.commit()

    strat = session.query(Strategy).filter_by(name="S007").one_or_none()
    if strat is None:
        raise SystemExit("strategy 'S007' not found -- run scripts.migrate_accounts_yml first")
    link = session.query(AccountStrategy).filter_by(strategy_id=strat.id).first()
    if link is None:
        raise SystemExit("no S007 account_strategy row found -- run "
                         "scripts.migrate_accounts_yml first")
    acc = link.account
    today = datetime.now().date()

    by_label: dict[str, list[dict]] = {}
    for r in _iter_events("position"):
        by_label.setdefault(r["label"], []).append(r)

    created = skipped = 0
    for label, events in by_label.items():
        exists = session.query(Position).filter_by(
            account_id=acc.id, strategy_id=strat.id, label=label).first()
        if exists:
            skipped += 1
            continue
        opens = [e for e in events if e.get("action") == "open"]
        closes = [e for e in events if e.get("action") == "close"]
        if not opens:
            continue
        o = opens[0]   # first successful placement carries the real entry/sl/tp
        pos = Position(account_id=acc.id, strategy_id=strat.id, label=label,
                       side=o["side"], entry=o["entry"], sl=o["sl"], tp=o["tp"],
                       is_add=bool(o.get("is_add")), status="open",
                       opened_at=datetime.fromisoformat(o["ts"]))
        if closes:
            c = closes[-1]
            pos.status = "closed"
            pos.reason = c.get("reason") or "unknown"
            pos.closed_at = datetime.fromisoformat(c["ts"])
        else:
            lbl_date = _label_date(label)
            if lbl_date and lbl_date < today:
                pos.status = "closed"
                pos.reason = "historical_import"
                pos.closed_at = datetime.fromisoformat(o["ts"])
        session.add(pos)
        session.flush()
        _log_event(session, LogKind.POSITION_OPEN, message=label, account=acc,
                  strategy=strat, cycle_id=o.get("cycle"),
                  payload=dict(side=o["side"], entry=o["entry"], sl=o["sl"], tp=o["tp"],
                              is_add=bool(o.get("is_add"))))
        if pos.status == "closed":
            _log_event(session, LogKind.POSITION_CLOSE, message=label, account=acc,
                      strategy=strat,
                      cycle_id=(closes[-1].get("cycle") if closes else o.get("cycle")),
                      payload=dict(reason=pos.reason))
        created += 1
    session.commit()
    print(f"positions: {created} imported, {skipped} already present")

    already = session.query(LogEntry).filter_by(
        account_id=acc.id, strategy_id=strat.id, kind=LogKind.ERROR.value).count()
    if already:
        print(f"errors: {already} already present, skipping import")
    else:
        err_count = 0
        for r in _iter_events("error"):
            _log_event(session, LogKind.ERROR, level=LogLevel.ERROR,
                      message=str(r.get("error") or "error")[:500],
                      account=acc, strategy=strat, cycle_id=r.get("cycle"))
            err_count += 1
        session.commit()
        print(f"errors: {err_count} imported")
    session.close()


if __name__ == "__main__":
    backfill()
