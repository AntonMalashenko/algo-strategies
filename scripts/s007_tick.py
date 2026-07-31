"""S007 live scheduler -- a single stateless "tick", invoked periodically by
launchd (StartCalendarInterval, see scripts/com.anton.algo.s007bot.plist), NOT a
long-running loop process.

This replaces an earlier design (scripts/s007_loop.py, since removed) that
kept one Python process alive ~24h/day, sleeping in short chunks between
checks. Anton's point in review: don't keep a process alive for a near-full
day at all -- have the OS scheduler invoke a short script periodically and
let IT decide, each time, whether there is anything to do. That is a
strictly stronger fix than "sleep in shorter chunks": there is no process
lifetime here for macOS to throttle/App-Nap in the first place, because
there is no persistent process. launchd's StartCalendarInterval is itself
immune to that class of throttling (it is the system component other apps
get napped relative to), and per Apple's documented behavior it catches up
on missed runs after the Mac wakes from sleep, unlike plain cron. It also
does not drift the way StartInterval would: StartInterval computes each
firing as (previous actual start time) plus N seconds, so any per-cycle
overhead compounds over thousands of firings into a monotonic offset from
wall-clock time with no daily reset. StartCalendarInterval with an empty
match dict fires once every real wall-clock minute instead, computed from
the absolute calendar clock each time, so there is nothing to accumulate
(see decisions-log.md 2026-07-23, follow-up on interval drift).

Root incident this whole scheduling rework replies to (2026-07-22/23, see
decisions-log.md): the old bash loop's `sleep 61172` (~17h, computed once at
17:00 to carry it to the next session) never returned -- process alive in
`ps` (STAT=SN), zero new log lines for ~17h. Likely cause: macOS throttling a
long-lived timer in an unsupervised background process. This design removes
the "long-lived process" premise entirely rather than just shortening the
sleep inside one.

Each invocation of tick():
  1. Reads the wall clock once.
  2. Outside the session window -> log a heartbeat at most once per
     HEARTBEAT_WINDOW (so idle hours produce occasional, not zero and not
     spammy, evidence the scheduler is actually being invoked) and exit.
  3. Inside the window but today is already settled (a previous tick this
     same day already saw day_done, a height-filtered day -- see
     bot/s007_signals.py::plan_now -- or a manual --stop-today flag) ->
     heartbeat-throttled exit, same as above, no broker call.
  4. Otherwise -> run exactly one `--live` reconcile cycle as a fresh
     subprocess (see run_cycle()'s docstring for why a subprocess, not an
     in-process import) and record loop_settled if that cycle reports
     day_done, filtered, or manual_stop.

There is no in-memory state carried between invocations (each tick() call
may be a brand-new process) -- "already settled today" and "last heartbeat"
are both read back out of today's own event log
(reports/logs/S007/events-<date>.jsonl), which every StrategyLogger call
already writes. The log IS the persisted state; no separate marker file.

Usage (manual, one-shot, for testing):
    python3 scripts/s007_tick.py

Usage (supervised, invoked every 60s by launchd) -- see
scripts/s007_loop_install.sh and scripts/com.anton.algo.s007bot.plist.
"""
from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot import s007_config as C  # noqa: E402
from utils.trade_logger import StrategyLogger  # noqa: E402

STRATEGY_NAME = "S007"
LOG_ROOT = ROOT / "reports" / "logs"
LOG = StrategyLogger(STRATEGY_NAME, log_root=str(LOG_ROOT))

CYCLE_TIMEOUT_SECONDS = 120     # kill a wedged --live subprocess rather than block forever
HEARTBEAT_WINDOW = datetime.timedelta(minutes=30)   # log "scheduler alive" at most this often


def in_session(now: datetime.datetime) -> bool:
    """Mon-Fri, TRADE_START..EXIT_END by wall-clock hour (reuses the named
    session-window constants bot/s007_config.py already defines -- no
    separate literal "10"/"17" here)."""
    start_h = int(C.TRADE_START.split(":")[0])
    end_h = int(C.EXIT_END.split(":")[0]) + 1   # EXIT_END="16:59" -> hour 16 still counts
    return now.weekday() < 5 and start_h <= now.hour < end_h


def _events_path(log_root: Path, strategy: str, day: datetime.date) -> Path:
    return Path(log_root) / strategy / f"events-{day.isoformat()}.jsonl"


def _has_event_today(log_root: Path, strategy: str, day: datetime.date, kind: str,
                      since: datetime.datetime | None = None) -> bool:
    """True if today's event log already has a record of `kind` (optionally
    only counting records at/after `since`). This is the persisted-state
    read: since tick() may run as a brand-new process every single
    invocation, "did we already settle/heartbeat today" cannot live in a
    Python variable -- it lives in the log that a previous invocation of
    this same script already wrote, via the same StrategyLogger every other
    S007 code path already uses."""
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


def _last_event_ts(log_root: Path, strategy: str, day: datetime.date,
                    kind: str) -> datetime.datetime | None:
    """Timestamp of the LAST `kind` event today, or None. Used to let a later
    `loop_resumed` (bot/s007_paper.py --resume-today) un-settle a day that saw
    `loop_settled` earlier -- a plain "did loop_settled ever appear today"
    check would otherwise keep the scheduler silent for the rest of the day
    even after the user clears a manual stop mid-session."""
    path = _events_path(log_root, strategy, day)
    if not path.exists():
        return None
    last = None
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
            ts = datetime.datetime.fromisoformat(rec["ts"])
            if last is None or ts > last:
                last = ts
    return last


def _parse_status_line(stdout: str) -> dict:
    """Parse the `STATUS key=val key=val ...` line bot/s007_paper.py::live()
    prints (see its docstring for the contract). Returns {} if no such line
    is present -- e.g. the cycle raised before reaching a broker session at
    all, which tick() must treat as "not settled", never as done."""
    for line in stdout.splitlines():
        if line.startswith("STATUS "):
            fields = dict(kv.split("=", 1) for kv in line[len("STATUS "):].split())
            return {k: (v == "True" if v in ("True", "False") else v) for k, v in fields.items()}
    return {}


def run_cycle() -> dict:
    """Run one `--live` reconcile cycle as a fresh subprocess -- deliberately,
    not by importing bot.s007_paper.live() in-process. bot/ctrader.py's
    session model (CTraderAdapter._run) calls Twisted's `reactor.run()` once
    per cycle; a Twisted reactor cannot be restarted within the same process
    (raises ReactorNotRestartable on a second call within one process). A
    fresh subprocess gives every cycle its own never-yet-run reactor, and
    means a wedged cycle can be killed on CYCLE_TIMEOUT_SECONDS without
    taking down the caller. Returns {} on crash/timeout -- never treated as
    day_done/filtered, so a crashed cycle just means "try again next tick",
    not "stop for today"."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "bot.s007_paper", "--live"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=CYCLE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        LOG.error("live cycle subprocess timed out",
                  exc=RuntimeError(f"no STATUS line within {CYCLE_TIMEOUT_SECONDS}s"))
        return {}
    if proc.returncode != 0:
        LOG.error("live cycle subprocess exited non-zero",
                  exc=RuntimeError((proc.stderr or "")[-2000:] or f"exit code {proc.returncode}"))
    return _parse_status_line(proc.stdout)


def tick(now: datetime.datetime | None = None, log: StrategyLogger | None = None,
         log_root: Path | None = None, strategy: str = STRATEGY_NAME) -> None:
    """One scheduling decision, plus (maybe) one trading cycle. No sleeping,
    no loop -- meant to return within seconds every time. `now`/`log`/
    `log_root` are overridable for tests; production calls tick() with no
    arguments and gets the real clock and the real S007 log."""
    now = now or datetime.datetime.now()
    log = log or LOG
    log_root = log_root or LOG_ROOT
    today = now.date()

    if not in_session(now):
        if not _has_event_today(log_root, strategy, today, "loop_heartbeat", since=now - HEARTBEAT_WINDOW):
            log.event("loop_heartbeat", now=now.isoformat(timespec="seconds"), in_session=False)
        return

    settled_ts = _last_event_ts(log_root, strategy, today, "loop_settled")
    resumed_ts = _last_event_ts(log_root, strategy, today, "loop_resumed")
    if settled_ts is not None and (resumed_ts is None or resumed_ts < settled_ts):
        if not _has_event_today(log_root, strategy, today, "loop_heartbeat", since=now - HEARTBEAT_WINDOW):
            log.event("loop_heartbeat", now=now.isoformat(timespec="seconds"), in_session=True, settled=True)
        return

    result = run_cycle()
    if result.get("day_done") or result.get("filtered") or result.get("manual_stop"):
        reason = ("day_done" if result.get("day_done") else
                  "filtered" if result.get("filtered") else "manual_stop")
        log.event("loop_settled", reason=reason, today=str(today))


if __name__ == "__main__":
    tick()
