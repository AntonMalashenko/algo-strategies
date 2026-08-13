"""S009 funding-carry live scheduler -- a single stateless "tick", invoked
periodically by launchd (StartCalendarInterval, see
deployment/com.algo.s009-paper.plist), NOT a long-running loop process.

DEPRECATED as the live path (2026-08-13): superseded by the Docker/Ofelia
dispatcher (webapp/runner.py's _worker_s009, scheduled via
deployment/schedule.yml + scripts/scheduler_tick.py). Real order placement
for Bybit-algo009 (the account this launchd job used to drive directly with
`--broker execute --allow-mainnet`) is now gated per-account_strategy-row via
AccountStrategy.broker_mode="execute" on account_strategy id=3 (see
webapp/schemas/enums.py::BrokerMode and _worker_s009's own docstring) --
verified against the live account (equity fetch + computed rebalance plan
matched the legacy path's own target book) before cutover. The launchd job
this module depends on (com.algo.s009-paper.plist) is deliberately NOT
installed in ~/Library/LaunchAgents/ anymore -- both paths independently
manage the SAME real Bybit mainnet account, so running both at once means
two processes racing to open/close/rebalance the same positions. This file
is kept only as a manual break-glass fallback: `scripts/s009_tick_install.sh
install`, and ONLY after first setting account_strategy id=3's broker_mode
back to "off" or "dry" (or otherwise confirming the Docker path is stopped
for this account) -- see deployment/com.algo.s009-paper.plist's own
warning. Do not re-install this plist as a "just in case" convenience;
every extra place it can be loaded from is exactly the risk this
deprecation removes.

Why this exists instead of `bot/s009_paper.py --loop`: that flag still works
for manual/foreground testing, but it is the same architecture (one process
alive for ~24h/day, sleeping in short chunks between checks) that S007's
original scheduler used and that failed in production -- see
decisions-log.md 2026-07-23: a long-lived bash `sleep 61172` (~17h) never
returned; the process stayed alive in `ps` (STAT=SN) but produced zero log
lines for ~17h, most likely macOS App Nap / power-management throttling an
unsupervised background process. S007's fix was not "sleep in shorter
chunks" (a KeepAlive-based Python loop with ProcessType=Interactive was
tried first and still replaced) -- it was to remove the long-lived-process
premise entirely: let launchd's StartCalendarInterval invoke a short script
periodically and have IT decide, fresh, every time, whether there is
anything to do. StartCalendarInterval is itself immune to that throttling
class (other apps get App-Napped relative to it, not the reverse) and, per
Apple's documented behavior, catches up on missed runs after the Mac wakes
from sleep -- unlike plain cron or a sleeping process. This applies to S009
at least as much as S007: S009 rebalances once a DAY, so a silently frozen
loop process here could sit idle for 24h+ before anyone notices a missed
rebalance, worse detection latency than S007's intraday cadence.

Each invocation of tick():
  1. Reads bot/s009_paper.py's own persisted state (reports/paper_s009/state.json)
     -- last_day already processed -- and computes the expected last CLOSED
     UTC day the same way `bot.s009_paper._expected_last_closed_day()` does.
  2. If we are already caught up (last_day >= expected), this is a near-instant
     no-op: log an occasional heartbeat (at most once per HEARTBEAT_WINDOW, so
     idle hours produce occasional, not zero and not spammy, evidence the
     scheduler is actually being invoked) and exit. No network call.
  3. Otherwise a new day has closed and needs processing: run exactly one
     `bot.s009_paper --once` cycle as a fresh subprocess (see run_cycle()'s
     docstring for why a subprocess) and, if state.json shows it advanced as
     expected, record loop_settled.

Unlike S007's tick, S009 has no Twisted reactor to avoid re-entering (Bybit
execution is a plain `requests`-based REST client, bot/bybit_exec.py) -- the
subprocess boundary here is a deliberate safety margin (bound a possibly
slow/hanging multi-symbol Bybit fetch with a hard timeout, and keep a crashed
cycle from taking the scheduler down with it), not a hard technical
requirement the way it is for S007.

There is no in-memory state carried between invocations (each tick() call may
be a brand-new process) -- "already caught up today" is read back from
bot/s009_paper.py's own state.json (the same file `--status`/`--once` use),
and "last heartbeat" is read back out of today's own event log
(reports/logs/S009/events-<date>.jsonl), which every StrategyLogger call
already writes. Both are the persisted state; no separate marker file.

Usage (manual, one-shot, for testing):
    python3 scripts/s009_tick.py --broker off

Usage (supervised, invoked every wall-clock minute by launchd) -- see
scripts/s009_tick_install.sh and deployment/com.algo.s009-paper.plist.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.s009_paper import load_state, _expected_last_closed_day  # noqa: E402
from utils.trade_logger import StrategyLogger  # noqa: E402

STRATEGY_NAME = "S009"
LOG_ROOT = ROOT / "reports" / "logs"
LOG = StrategyLogger(STRATEGY_NAME, log_root=str(LOG_ROOT))

CYCLE_TIMEOUT_SECONDS = 300          # generous: ~24 symbols x 2 Bybit endpoints, worst case
HEARTBEAT_WINDOW = datetime.timedelta(minutes=30)   # log "scheduler alive" at most this often


def _events_path(log_root: Path, strategy: str, day: datetime.date) -> Path:
    return Path(log_root) / strategy / f"events-{day.isoformat()}.jsonl"


def _has_event_today(log_root: Path, strategy: str, day: datetime.date, kind: str,
                      since: datetime.datetime | None = None) -> bool:
    """True if today's event log already has a record of `kind` (optionally
    only counting records at/after `since`). Mirrors scripts/s007_tick.py's
    helper of the same name -- same reasoning: tick() may run as a brand-new
    process every invocation, so this state has to live in the log a
    previous invocation already wrote, not in a Python variable."""
    path = _events_path(log_root, strategy, day)
    if not path.exists():
        return False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") != kind:
                continue
            if since is None:
                return True
            ts = datetime.datetime.fromisoformat(rec["ts"])
            if ts >= since:
                return True
    return False


def run_cycle(broker: str, allow_mainnet: bool) -> subprocess.CompletedProcess | None:
    """Run one `bot.s009_paper --once` cycle as a fresh subprocess. Returns
    None (and logs the timeout) if it does not finish within
    CYCLE_TIMEOUT_SECONDS -- the caller must treat that the same as a crash:
    not settled, try again next tick."""
    args = [sys.executable, "-m", "bot.s009_paper", "--once", "--broker", broker]
    if allow_mainnet:
        args.append("--allow-mainnet")
    try:
        return subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True,
                               timeout=CYCLE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        LOG.error("S009 cycle subprocess timed out",
                  exc=RuntimeError(f"no return within {CYCLE_TIMEOUT_SECONDS}s"))
        return None


def tick(now: datetime.datetime | None = None, log: StrategyLogger | None = None,
         log_root: Path | None = None, strategy: str = STRATEGY_NAME,
         broker: str = "off", allow_mainnet: bool = False) -> None:
    """One scheduling decision, plus (maybe) one daily rebalance cycle. No
    sleeping, no loop -- meant to return within seconds every time except the
    (at most once/day) tick that actually runs a cycle. `now`/`log`/
    `log_root` are overridable for tests; production calls tick() with no
    arguments and gets the real clock and the real S009 log. `now` is naive
    local wall-clock time (not UTC) to match StrategyLogger's own `ts` field
    (utils/trade_logger.py writes `datetime.now()`, naive local) -- mirrors
    scripts/s007_tick.py's same default. It is used only for heartbeat
    dedup/log-file day-naming here; the actual UTC trading-day boundary
    comes from `_expected_last_closed_day()`, independent of this clock.
    A UTC-aware default previously caused `_has_event_today`'s comparison
    against the logger's naive `ts` to raise
    "can't compare offset-naive and offset-aware datetimes" on every idle
    tick once a heartbeat had been logged for the day (see the repeated
    "S009 cycle subprocess exited non-zero" entries in S009.log 2026-08-03)."""
    now = now or datetime.datetime.now()
    log = log or LOG
    log_root = log_root or LOG_ROOT
    today = now.date()

    st = load_state()
    exp = _expected_last_closed_day()
    if st.get("last_day") is not None and st["last_day"] >= exp:
        if not _has_event_today(log_root, strategy, today, "loop_heartbeat", since=now - HEARTBEAT_WINDOW):
            log.event("loop_heartbeat", now=now.isoformat(timespec="seconds"),
                      up_to_date=True, last_day=st["last_day"])
        return

    proc = run_cycle(broker, allow_mainnet)
    if proc is None:
        return  # timeout already logged by run_cycle()
    if proc.returncode != 0:
        log.error("S009 cycle subprocess exited non-zero",
                  exc=RuntimeError((proc.stderr or "")[-2000:] or f"exit code {proc.returncode}"))
        return

    st2 = load_state()
    if st2.get("last_day") is not None and st2["last_day"] >= exp:
        log.event("loop_settled", today=str(today), last_day=st2["last_day"], broker=broker)
    else:
        # Cycle exited 0 but state didn't advance as expected -- surface it
        # loudly rather than silently retrying forever on the same tick.
        log.error("S009 cycle finished but state did not advance as expected",
                  exc=RuntimeError(f"last_day={st2.get('last_day')!r} expected>={exp}"))


def main() -> None:
    ap = argparse.ArgumentParser(description="S009 funding-carry live scheduler tick.")
    ap.add_argument("--broker", choices=["off", "dry", "execute"], default="off",
                    help="off=shadow only; dry=compute+log intended demo orders; execute=place demo orders")
    ap.add_argument("--allow-mainnet", action="store_true", help="DANGER: permit real mainnet orders")
    args = ap.parse_args()
    tick(broker=args.broker, allow_mainnet=args.allow_mainnet)


if __name__ == "__main__":
    main()
