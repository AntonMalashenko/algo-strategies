"""S007 watchdog (ALGODEV-45 step 1): detects a stalled scheduler tick.

Read-only w.r.t. the broker -- reads AccountStrategy.last_cycle_at from the
DB, nothing else, and NEVER places/cancels/modifies an order. Dispatched the
same stateless-tick way as every other entry in deployment/schedule.yml (see
that file's own docstring) -- no long-lived process, no state kept in
memory, a fresh invocation decides fresh every time from `data/`.

Step 1 of ALGODEV-45 (persistent cTrader session): detection only. There is
nothing to kill/restart yet -- that only exists from Step 2/3 of that ticket
onward, once a persistent daemon exists. Building and proving "notice a
stall" FIRST, on the current already-safe stateless model, is deliberate:
the whole reason ALGODEV-45 exists is a past ~17h hang of a long-lived
process (see that ticket), so the safety net has to work BEFORE the riskier
architecture change, not after.

Two outputs, kept deliberately separate (Anton, 2026-09-17):
  - ALERT_FILE (data/watchdog_alerts.log): a small, normally-EMPTY sentinel
    file. Checking "is this file empty" is a one-glance answer; mixing
    alerts into the busy per-minute events-<date>.jsonl would bury them.
  - the strategy's own events-<date>.jsonl (kind="watchdog_alert" /
    "watchdog_recovered"), for time-correlating a gap against that day's
    other cycle events when actually investigating an incident.

STALE_THRESHOLD_MINUTES=3 (Anton): S007 ticks every minute during its
session window, so 3 missed minutes is already an anomaly worth a line, not
scheduler jitter.

Usage (manual, one-shot):
    python3 scripts/s007_watchdog.py

Usage (supervised, invoked every 1-2 minutes): see deployment/schedule.yml.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
KYIV = ZoneInfo("Europe/Kyiv")
ALERT_FILE = ROOT / "data" / "watchdog_alerts.log"
STATE_FILE = ROOT / "data" / ".s007_watchdog_state.json"

STRATEGY_NAME = "S007"
STRATEGY_LOG_GROUP = "S007-acct47939312"  # matches StrategyLogger's group name elsewhere for this account

STALE_THRESHOLD_MINUTES = 3

# Matches deployment/schedule.yml's S007 cron ("* 10-16 * * 1-5") -- the
# window the SCHEDULER dispatches S007 in, not bot/s007_config.py's
# narrower TRADE_START/EXIT_END trading window (a cycle outside trading
# hours but inside this window is an expected cheap no-op, not silence).
SESSION_START_HOUR, SESSION_END_HOUR = 10, 16
SESSION_WEEKDAYS = range(0, 5)  # Monday=0 .. Friday=4, per datetime.weekday()


def expected_to_run(now_utc: datetime) -> bool:
    """True while the scheduler is expected to be ticking S007 at all (the
    cron window), regardless of whether today's trading session is open.

    `now_utc`: naive UTC (matching AccountStrategy.last_cycle_at, always
    written as datetime.now(timezone.utc) -- see webapp/runner.py). The
    cron window itself is defined in LOCAL Kyiv wall-clock time (matches
    deployment/schedule.yml's own convention and the container's TZ), so
    this converts before checking weekday/hour -- comparing a UTC value
    directly against Kyiv-local bounds silently shifted every check by the
    UTC+2/+3 offset (found testing against the real DB 2026-09-17: a
    perfectly healthy, seconds-old cycle read back as "180.8 min stalled",
    exactly the summer UTC+3 offset in minutes)."""
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(KYIV)
    return local.weekday() in SESSION_WEEKDAYS and SESSION_START_HOUR <= local.hour <= SESSION_END_HOUR


def is_settled_for_today(status: str | None, now_utc: datetime) -> bool:
    """True when webapp/runner.py's own per-day short-circuit (_worker_s007:
    'settled = day_done or filtered or manual_stop', then
    link.status = f"settled:{today_local}") has already fired -- S007
    legitimately stopped trading for the rest of today (target reached, the
    day's setup got filtered out, or a manual stop) and every subsequent
    tick this whole session is an intentional, correct no-op that opens no
    broker session and updates nothing, INCLUDING last_cycle_at.

    Found live 2026-09-18 (Anton: "а сегодня он пытался торговать - я
    перезапускал контейнеры" -- turned out unrelated to any container
    restart): S007 hit day_done at 11:16 Kyiv, correctly went quiet for the
    day, and this watchdog -- which only knew about last_cycle_at staleness,
    not about the settled short-circuit -- raised a false "S007 stalled"
    alert 4 minutes later. A frozen last_cycle_at during a legitimately
    settled day is expected, not a stall; only a frozen last_cycle_at while
    NOT settled is worth a human's attention.

    `today_local` uses the SAME Europe/Kyiv wall-clock date webapp/runner.py
    itself stamps the marker with (host-local, not UTC) -- see
    expected_to_run's docstring for why now_utc needs converting first."""
    if not status:
        return False
    today_local = now_utc.replace(tzinfo=timezone.utc).astimezone(KYIV).date().isoformat()
    return status.startswith(f"settled:{today_local}")


def evaluate(now_utc: datetime, last_cycle_at: datetime | None, status: str | None,
            prior_state: dict) -> tuple[dict, str | None]:
    """Pure decision logic -- no I/O, no broker, unit-testable and safely
    replayable against real historical last_cycle_at gaps. Returns
    (new_state, alert_line_or_None): alert_line is a ready-to-write,
    human-readable line, or None if nothing should be written this run.

    `now_utc` and `last_cycle_at` are both naive UTC -- see
    expected_to_run's docstring for why this matters. `status` is
    AccountStrategy.status as-is (see is_settled_for_today).

    prior_state: {"alerted_for": "<last_cycle_at.isoformat()>" or None} --
    the last_cycle_at value already alerted about, so a multi-hour outage
    (last_cycle_at frozen the whole time) produces exactly ONE alert line
    at the start, not one per 1-2 minute check, plus one RECOVERED line
    once a fresh last_cycle_at appears (or the window closes mid-alert).
    """
    now = now_utc
    if (last_cycle_at is None or not expected_to_run(now)
            or is_settled_for_today(status, now)):
        if prior_state.get("alerted_for"):
            line = (f"{now.isoformat()}Z RECOVERED (session window ended, or S007 "
                    f"settled for today) -- S007 last_cycle_at={last_cycle_at}Z status={status!r}")
            return {"alerted_for": None}, line
        return prior_state, None

    age = now - last_cycle_at
    key = last_cycle_at.isoformat()

    if age > timedelta(minutes=STALE_THRESHOLD_MINUTES):
        if prior_state.get("alerted_for") == key:
            return prior_state, None  # same outage already alerted, don't spam
        line = (f"{now.isoformat()}Z ALERT S007 stalled -- last_cycle_at={key}Z "
                f"({age.total_seconds() / 60:.1f} min ago, threshold={STALE_THRESHOLD_MINUTES}min)")
        return {"alerted_for": key}, line

    if prior_state.get("alerted_for"):
        line = f"{now.isoformat()}Z RECOVERED S007 -- last_cycle_at={key}Z"
        return {"alerted_for": None}, line
    return {"alerted_for": None}, None


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _write_alert(account_strategy_id: int, line: str) -> None:
    ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ALERT_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    from utils.trade_logger import StrategyLogger
    logger = StrategyLogger(STRATEGY_LOG_GROUP, log_root=str(ROOT / "reports" / "logs"))
    kind = "watchdog_recovered" if "RECOVERED" in line else "watchdog_alert"
    logger.event(kind, text=line, account_strategy_id=account_strategy_id)


def main() -> None:
    from webapp.db import get_session
    from webapp.models import AccountStrategy, Strategy

    session = get_session()
    try:
        links = (session.query(AccountStrategy)
                 .join(Strategy)
                 .filter(Strategy.name == STRATEGY_NAME, AccountStrategy.enabled == True)  # noqa: E712
                 .all())
        # naive UTC, matching AccountStrategy.last_cycle_at exactly (see
        # expected_to_run's docstring) -- NOT datetime.now(), which is
        # container-local (Europe/Kyiv, UTC+2/+3) and would misjudge every
        # gap by the current DST offset.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        state = _load_state()
        changed = False
        for link in links:
            key = str(link.id)
            prior = state.get(key, {})
            new_state, alert_line = evaluate(now, link.last_cycle_at, link.status, prior)
            if new_state != prior:
                state[key] = new_state
                changed = True
            if alert_line:
                _write_alert(link.id, alert_line)
        if changed:
            STATE_FILE.write_text(json.dumps(state))
    finally:
        session.close()


if __name__ == "__main__":
    main()
