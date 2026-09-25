"""Generic scheduler tick -- reads deployment/schedule.yml and, for each
strategy/task whose cron schedule matches the current minute, runs its
command as an isolated subprocess. Invoked every minute by Ofelia (see
docker-compose.yml's single `dispatch` job) -- the same stateless-tick
pattern as scripts/s007_tick.py / scripts/s009_tick.py (decisions-log.md
2026-07-23: no long-lived process, an external scheduler invokes a short
script that decides fresh every time), generalized to the WHOLE project
instead of one strategy. Adding a new strategy's schedule or a background
task is a deployment/schedule.yml edit + code review -- docker-compose.yml
never needs to change again.

No direct DB access here, deliberately: `webapp.runner --strategy <name>`
already queries the DB for enabled (account, strategy) rows and fans out
per account itself (webapp/runner.py's coordinator/worker split). This
script only decides WHEN to invoke that (and any background task's
command) -- it never touches account/strategy/credential data, so it stays
usable even before/without a DB migration for any given strategy.

Usage (manual, one-shot):
    python3 scripts/scheduler_tick.py
    python3 scripts/scheduler_tick.py --schedule path/to/schedule.yml

Usage (supervised, invoked every minute by Ofelia): see docker-compose.yml.
"""
from __future__ import annotations

import argparse
import datetime
import shlex
import subprocess
import sys
import time
import zoneinfo
from pathlib import Path

import yaml
from croniter import croniter

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEDULE_FILE = ROOT / "deployment" / "schedule.yml"

# Generous but not open-ended -- matches the sibling tick scripts' own
# per-cycle timeouts (scripts/s007_tick.py's CYCLE_TIMEOUT_SECONDS=120,
# scripts/s009_tick.py's =300). `webapp.runner --strategy X` has its own
# internal per-account budget (DEFAULT_TIMEOUT_S in webapp/runner.py) well
# under this, so this is a backstop against a fully wedged subprocess, not
# the normal-case ceiling.
ITEM_TIMEOUT_SECONDS = 120

# Found live 2026-08-10: Ofelia fires a fresh job-run container every minute
# (docker-compose.yml's outer "0 * * * * *") regardless of whether the
# PREVIOUS minute's scheduler_tick is still running -- normally a cycle
# takes a few seconds, but under degraded conditions (that day: cTrader
# session setup slowed to 55-118s after a Podman-machine restart) a strategy
# ended up dispatched by two overlapping ticks at once. No duplicate order
# resulted that time (the worker re-reads real broker state each cycle
# rather than trusting its own prior intent), but nothing structural
# prevented it. LOCK_DIR lives on the same bind-mounted, host-persistent
# volume as the DB (data/, see docker-compose.yml's dispatch job volumes) so
# a lock survives across the ephemeral per-tick containers that create it.
LOCK_DIR = ROOT / "data" / ".scheduler_locks"


def load_schedule(path: Path) -> dict:
    """{"strategies": [...], "tasks": [...]} -- missing file or empty
    sections read as "nothing to do" rather than an error, so a fresh
    checkout with no schedule.yml yet (or one mid-edit) doesn't crash the
    tick, it just no-ops."""
    if not path.exists():
        return {"strategies": [], "tasks": []}
    data = yaml.safe_load(path.read_text()) or {}
    return {"strategies": data.get("strategies") or [], "tasks": data.get("tasks") or []}


# The container's own wall-clock timezone (see docker-compose.yml's ofelia
# service TZ=Europe/Kyiv, which every un-tagged schedule.yml entry has always
# matched against). Deliberately a literal here rather than read from the
# environment: an entry that opts into `tz:` should convert against a known-
# correct value even if the container's own TZ var is ever missing or wrong.
# If docker-compose.yml's TZ ever changes, this constant must change with it.
CONTAINER_TZ = zoneinfo.ZoneInfo("Europe/Kyiv")


def _effective_now(now: datetime.datetime, tz_name: str | None) -> datetime.datetime:
    """`now` is always naive local (container/Kyiv) wall-clock time -- see
    tick()'s docstring. An entry with no `tz:` key (tz_name is None) is a
    total no-op here and matches exactly as it always has -- S007/S009/S011/
    watchdog/deribit_snapshot are all unaffected by this function existing.

    An entry that sets `tz: <IANA name>` (e.g. "UTC") gets `now` reinterpreted
    as CONTAINER_TZ-aware, converted into the requested zone, and returned
    naive again in THAT zone -- so its cron string can be written directly in
    its own target timezone rather than the container's, and croniter (which
    only compares naive field values) still matches it correctly.

    Added 2026-09-22 for S021: its session is anchored to a fixed EST/UTC
    clock that never drifts (strategy-passport-S021.md sec 0/2), but the
    container's cron clock is Kyiv-local, which DOES drift on Kyiv's own DST
    twice a year -- see decisions-log.md 2026-09-22 for the fuller writeup of
    why a wide Kyiv-local window was the interim fix and this is the real
    one. Deliberately scoped to opt-in per entry: S007's Kyiv anchor is not a
    bug (Kyiv and Frankfurt/DAX share EU DST dates, so it's a stable,
    zero-drift proxy for DAX local time -- converting it to UTC would need a
    hand-maintained seasonal offset instead, trading one drift problem for
    another), and S009/S011 have no Kyiv dependency left to convert."""
    if tz_name is None:
        return now
    aware = now.replace(tzinfo=CONTAINER_TZ)
    converted = aware.astimezone(zoneinfo.ZoneInfo(tz_name))
    return converted.replace(tzinfo=None)


def _due(schedule_expr: str, now: datetime.datetime) -> bool:
    return croniter.match(schedule_expr, now)


def _lock_path(kind: str, name: str) -> Path:
    return LOCK_DIR / f"{kind}-{name}.lock"


def _acquire_lock(kind: str, name: str) -> bool:
    """True if no other tick's dispatch of this same (kind, name) is still
    in flight, and this call has now claimed it. A lock file older than
    ITEM_TIMEOUT_SECONDS is treated as abandoned rather than blocking
    forever: _run_item's own subprocess.run always releases it (successful
    return, non-zero exit, or its own TimeoutExpired all hit the `finally`
    below) within that ceiling, so a lock that outlives it can only mean the
    process that held it was killed from outside (OOM, container hard-kill)
    before reaching the `finally` -- same self-healing-over-blocking
    philosophy as S009's own catch-up-on-next-run design, not a new one."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = _lock_path(kind, name)
    if path.exists():
        age = time.time() - path.stat().st_mtime
        if age < ITEM_TIMEOUT_SECONDS:
            print(f"[scheduler] {kind} {name!r}: previous run still in progress "
                  f"({age:.0f}s old), skipping this tick")
            return False
        print(f"[scheduler] {kind} {name!r}: stale lock ({age:.0f}s old), stealing it")
    path.write_text(str(time.time()))
    return True


def _release_lock(kind: str, name: str) -> None:
    _lock_path(kind, name).unlink(missing_ok=True)


def _run_item(kind: str, name: str, args: list[str]) -> None:
    """Run one strategy/task as an isolated subprocess. Never raises -- a
    failure here is that one item's problem, not the whole tick's; every
    outcome (ok, non-zero exit, timeout, failed to start) is logged and
    swallowed so the loop keeps going to the next item.

    Guarded by a per-(kind, name) lock so a slow cycle (see LOCK_DIR's
    comment) can never be dispatched twice concurrently by two overlapping
    ticks."""
    if not _acquire_lock(kind, name):
        return
    try:
        proc = subprocess.run(args, cwd=str(ROOT), capture_output=True,
                               text=True, timeout=ITEM_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            print(f"[scheduler] {kind} {name!r} exited {proc.returncode}: "
                  f"{(proc.stderr or '')[-1000:]}")
        else:
            print(f"[scheduler] {kind} {name!r} ok")
    except subprocess.TimeoutExpired:
        print(f"[scheduler] {kind} {name!r} TIMEOUT after {ITEM_TIMEOUT_SECONDS}s")
    except Exception as exc:                     # noqa: BLE001 -- see docstring
        print(f"[scheduler] {kind} {name!r} failed to start: {exc!r}")
    finally:
        _release_lock(kind, name)


def tick(now: datetime.datetime | None = None, schedule_file: Path | None = None) -> None:
    """One scheduling decision, plus (maybe) several dispatches -- no
    sleeping, no loop, meant to return within ITEM_TIMEOUT_SECONDS * (number
    of due items) at worst. `now`/`schedule_file` are overridable for tests;
    production calls tick() with no arguments and gets the real clock and
    deployment/schedule.yml. `now` is naive local wall-clock time, matching
    every cron string in schedule.yml (evaluated in the container's local
    TZ, see docker-compose.yml's ofelia service) and every other tick
    script's convention in this repo -- UNLESS an entry sets an optional
    `tz:` key (e.g. `tz: UTC`), in which case its cron string is matched
    against `now` converted into THAT zone instead (see _effective_now()).
    Entries with no `tz:` key -- i.e. every entry except S021 as of
    2026-09-22 -- are completely unaffected."""
    now = now or datetime.datetime.now()
    schedule_file = schedule_file or DEFAULT_SCHEDULE_FILE
    sched = load_schedule(schedule_file)

    for s in sched["strategies"]:
        name = s["name"]
        if _due(s["schedule"], _effective_now(now, s.get("tz"))):
            _run_item("strategy", name,
                      [sys.executable, "-m", "webapp.runner", "--strategy", name])

    for t in sched["tasks"]:
        name = t["name"]
        if _due(t["schedule"], _effective_now(now, t.get("tz"))):
            _run_item("task", name, shlex.split(t["command"]))


def main() -> None:
    ap = argparse.ArgumentParser(description="Generic schedule-driven dispatcher tick.")
    ap.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE_FILE)
    args = ap.parse_args()
    tick(schedule_file=args.schedule)


if __name__ == "__main__":
    main()
