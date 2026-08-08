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


def load_schedule(path: Path) -> dict:
    """{"strategies": [...], "tasks": [...]} -- missing file or empty
    sections read as "nothing to do" rather than an error, so a fresh
    checkout with no schedule.yml yet (or one mid-edit) doesn't crash the
    tick, it just no-ops."""
    if not path.exists():
        return {"strategies": [], "tasks": []}
    data = yaml.safe_load(path.read_text()) or {}
    return {"strategies": data.get("strategies") or [], "tasks": data.get("tasks") or []}


def _due(schedule_expr: str, now: datetime.datetime) -> bool:
    return croniter.match(schedule_expr, now)


def _run_item(kind: str, name: str, args: list[str]) -> None:
    """Run one strategy/task as an isolated subprocess. Never raises -- a
    failure here is that one item's problem, not the whole tick's; every
    outcome (ok, non-zero exit, timeout, failed to start) is logged and
    swallowed so the loop keeps going to the next item."""
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


def tick(now: datetime.datetime | None = None, schedule_file: Path | None = None) -> None:
    """One scheduling decision, plus (maybe) several dispatches -- no
    sleeping, no loop, meant to return within ITEM_TIMEOUT_SECONDS * (number
    of due items) at worst. `now`/`schedule_file` are overridable for tests;
    production calls tick() with no arguments and gets the real clock and
    deployment/schedule.yml. `now` is naive local wall-clock time, matching
    every cron string in schedule.yml (evaluated in the container's local
    TZ, see docker-compose.yml's ofelia service) and every other tick
    script's convention in this repo."""
    now = now or datetime.datetime.now()
    schedule_file = schedule_file or DEFAULT_SCHEDULE_FILE
    sched = load_schedule(schedule_file)

    for s in sched["strategies"]:
        name = s["name"]
        if _due(s["schedule"], now):
            _run_item("strategy", name,
                      [sys.executable, "-m", "webapp.runner", "--strategy", name])

    for t in sched["tasks"]:
        name = t["name"]
        if _due(t["schedule"], now):
            _run_item("task", name, shlex.split(t["command"]))


def main() -> None:
    ap = argparse.ArgumentParser(description="Generic schedule-driven dispatcher tick.")
    ap.add_argument("--schedule", type=Path, default=DEFAULT_SCHEDULE_FILE)
    args = ap.parse_args()
    tick(schedule_file=args.schedule)


if __name__ == "__main__":
    main()
