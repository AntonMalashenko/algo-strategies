"""Podman-machine clock-drift healthcheck/watchdog.

Found live 2026-08-10: the Podman machine (a lightweight VM hosting the
Ofelia scheduler container -- see docker-compose.yml) does not reliably
resume its internal clock after this Mac wakes from sleep. Its clock froze
at the exact moment of a host sleep and silently stayed 18+ hours behind,
while `podman ps` / `podman machine inspect` both kept reporting it as
healthy/running the whole time -- no crash, no error, nothing to alert on.
Ofelia's own cron scheduler runs on that frozen clock, so it silently
stopped firing S007/S009 dispatch ticks -- exactly the failure class the
original launchd stateless-tick redesign (decisions-log.md 2026-07-23) was
built to eliminate, reintroduced one layer down by moving scheduling into a
VM that doesn't share launchd's sleep-resilience.

launchd itself IS proven immune to exactly this (that is the whole reason
it was chosen originally, and why S007/S009's own tick scripts still use
it at the outer layer even after their trading logic moved into Docker).
So this healthcheck runs AS a launchd job
(deployment/com.algo.podman-healthcheck.plist, every 5 minutes via
StartCalendarInterval) supervising the Docker/Podman layer from OUTSIDE
it, instead of trying to make the VM itself sleep-resistant (attempted and
failed live: `chronyc makestep` / restarting chronyd inside the VM did not
fix it -- the drift appears to be below chrony's own userspace visibility,
likely a hypervisor-level stuck clock source; only a full
`podman machine stop` + `start` reliably resynced it).

Usage (manual, one-shot):
    python3 scripts/podman_healthcheck.py
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from utils.trade_logger import StrategyLogger  # noqa: E402
from scripts.scheduler_tick import load_schedule, DEFAULT_SCHEDULE_FILE  # noqa: E402

# Absolute path, not "podman" via $PATH lookup: launchd invokes this job
# with a minimal environment (PATH=/usr/gnu/bin:/usr/local/bin:/bin:/usr/bin,
# no ~/.zshrc), which does not include /opt/podman/bin -- found live
# 2026-08-10 as a silent "podman machine unreachable" false-positive
# (subprocess.run(["podman", ...]) raised FileNotFoundError, caught by the
# broad except below and misreported as an unreachable machine).
PODMAN = "/opt/podman/bin/podman"
MACHINE_NAME = "podman-machine-default"
# Generous: a real freeze observed live was 18+ hours; this only needs to
# catch "frozen", not chase NTP-jitter-scale offsets.
DRIFT_THRESHOLD_SECONDS = 120
SSH_TIMEOUT_SECONDS = 30
MACHINE_STOP_TIMEOUT_SECONDS = 60
MACHINE_START_TIMEOUT_SECONDS = 90
COMPOSE_TIMEOUT_SECONDS = 60

LOG = StrategyLogger("PodmanHealthcheck", log_root=str(ROOT / "reports" / "logs"), console=False)


def _vm_epoch() -> int | None:
    """Current Unix time inside the Podman machine, or None if it could not
    be reached at all (SSH failure -- e.g. the machine is fully stopped, not
    just clock-drifted)."""
    try:
        proc = subprocess.run(
            [PODMAN, "machine", "ssh", MACHINE_NAME, "date +%s"],
            capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS)
        if proc.returncode != 0:
            return None
        return int(proc.stdout.strip())
    except Exception:
        return None


def _compose_env() -> dict:
    """os.environ plus DOCKER_HOST pointed at whatever socket THIS machine
    start actually bound to.

    Found live 2026-08-13: `machine start` does not always bind the
    standard /var/run/docker.sock -- if anything (even a since-exited
    process) held that address at start time, podman logs "Another process
    was listening on the default Docker API socket address" and silently
    falls back to a per-start temp path instead
    (.../T/podman/<machine>-api.sock). `podman compose` shells out to the
    docker-compose binary, which only speaks to DOCKER_HOST or the default
    socket -- with no DOCKER_HOST set it fails outright
    ("Cannot connect to the Docker daemon") against that fallback path, so
    the "bring ofelia back up" heal step was silently doing nothing on any
    restart that hit the fallback. `machine inspect` reports the ACTUAL
    bound path regardless of which case happened, so reading it fresh here
    (rather than assuming the default) makes this correct either way."""
    env = dict(os.environ)
    try:
        proc = subprocess.run([PODMAN, "machine", "inspect", MACHINE_NAME],
                              capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS)
        info = json.loads(proc.stdout)[0]
        sock_path = info["ConnectionInfo"]["PodmanSocket"]["Path"]
        env["DOCKER_HOST"] = f"unix://{sock_path}"
    except Exception as exc:
        LOG.error("could not resolve the machine's forwarded socket path -- "
                  "falling back to the default DOCKER_HOST", exc=exc)
    return env


def _ofelia_running() -> bool:
    try:
        proc = subprocess.run(
            [PODMAN, "ps", "--filter", "name=algo-ofelia", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=SSH_TIMEOUT_SECONDS)
        return bool(proc.stdout.strip())
    except Exception:
        return False


def _heal(reason: str) -> None:
    """stop+start the machine, bring ofelia back up, verify. Shared by both
    failure modes below -- `machine start` on an already-stopped machine is a
    harmless no-op-ish path (same command either way), so one heal routine
    covers "clock drifted" and "machine unreachable/stopped" alike.

    Each step is independently best-effort: a stuck/failing `machine stop`
    (e.g. it was already stopped and the command itself hangs briefly) must
    not prevent trying `machine start` right after, and none of these three
    may ever propagate -- this function's whole job is to be the safe
    fallback path, so it has to survive the exact subprocess failures
    (TimeoutExpired, FileNotFoundError, ...) that _vm_epoch() already
    tolerates."""
    LOG.error(reason, exc=RuntimeError(reason))

    for args, timeout, env in (
        ([PODMAN, "machine", "stop", MACHINE_NAME], MACHINE_STOP_TIMEOUT_SECONDS, None),
        ([PODMAN, "machine", "start", MACHINE_NAME], MACHINE_START_TIMEOUT_SECONDS, None),
        ([PODMAN, "compose", "up", "-d", "ofelia"], COMPOSE_TIMEOUT_SECONDS, _compose_env()),
    ):
        try:
            subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout,
                           env=env)
        except Exception as exc:
            LOG.error(f"heal step {args!r} failed", exc=exc)

    # Retry the compose step alone once with a freshly-resolved socket path --
    # covers the case where `machine start` itself only settled (bound its
    # real socket) after the first compose attempt had already raced past it.
    ofelia_ok = _ofelia_running()
    if not ofelia_ok:
        try:
            subprocess.run([PODMAN, "compose", "up", "-d", "ofelia"], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=COMPOSE_TIMEOUT_SECONDS,
                           env=_compose_env())
        except Exception as exc:
            LOG.error("ofelia retry-start failed", exc=exc)
        ofelia_ok = _ofelia_running()

    new_vm_epoch = _vm_epoch()
    clock_ok = (new_vm_epoch is not None
               and abs(int(time.time()) - new_vm_epoch) <= DRIFT_THRESHOLD_SECONDS)
    if clock_ok and ofelia_ok:
        LOG.event("healed")
    else:
        LOG.error("podman machine restart did not fully fix it -- needs manual attention",
                  exc=RuntimeError(f"clock_ok={clock_ok} (new_vm_epoch={new_vm_epoch}) "
                                   f"ofelia_ok={ofelia_ok}"))


DERIBIT_STATE_PATH = ROOT / "data" / "state" / "deribit_snapshot.json"
# Local wall-clock hour the deribit_snapshot task fires (deployment/
# schedule.yml's "0 19 * * *", evaluated in the container's local TZ, same
# convention documented there) -- used to decide "today's window has passed,
# and it still hasn't run" without re-deriving the DST-shifting UTC hour.
DERIBIT_FIRE_HOUR_LOCAL = 19
# Once-daily strategies (S009, S011, ...) self-heal by design on their next
# scheduled run (see each worker's own docstring) -- a few hours' lag is
# normal catch-up, not an incident. Only alert once a link has gone stale
# long enough to mean "missed today's AND yesterday's fire", i.e. roughly
# 1.5 cron periods for a daily job.
DAILY_STRATEGY_STALE_HOURS = 36
# S007 is the one intraday/every-minute strategy (schedule.yml's
# "* 10-16 * * 1-5") -- checked separately and tightly (see
# _check_s007_freshness) instead of the loose daily threshold above, since a
# gap here means real time-in-market lost, not a designed catch-up delay.
S007_STALE_MINUTES = 15


def _check_s007_freshness(now: datetime.datetime, links) -> None:
    """Alert if an enabled S007 link has no recent cycle while its own
    schedule.yml window says it should be ticking every minute. Uses
    croniter.match (same helper scheduler_tick.py's own _due() uses) so the
    window definition never drifts out of sync with the actual schedule."""
    from croniter import croniter

    sched = load_schedule(DEFAULT_SCHEDULE_FILE)
    cron = next((s["schedule"] for s in sched["strategies"] if s["name"] == "S007"), None)
    if cron is None or not croniter.match(cron, now):
        return   # outside S007's own session window -- silence is correct, not stale
    for link in links:
        if link.strategy.name != "S007":
            continue
        # Mirrors webapp/runner.py's own day-done short-circuit exactly
        # (link.status = f"settled:{today_local}"): once a day settles, the
        # worker deliberately stops opening broker sessions/ticking
        # last_cycle_at for the rest of that day -- a designed silence, not
        # staleness. Found live 2026-08-20: without this guard, every
        # settled day fired a false "no cycle" alert every 5 minutes for
        # the rest of the session window.
        if (link.status or "").startswith(f"settled:{now.date().isoformat()}"):
            continue
        last = _to_local_naive(link.last_cycle_at)
        if last is None or (now - last) > datetime.timedelta(minutes=S007_STALE_MINUTES):
            LOG.error(
                f"S007 account_strategy {link.id} ({link.account.label or link.account.id}): "
                f"no cycle in the last {S007_STALE_MINUTES}min during its own session window "
                f"(last_cycle_at={link.last_cycle_at}) -- likely the same ofelia/podman gap "
                f"this healthcheck exists to close",
                exc=RuntimeError("S007 stale during session window"))


def _to_local_naive(ts: datetime.datetime | None) -> datetime.datetime | None:
    """AccountStrategy.last_cycle_at is written as datetime.now(timezone.utc)
    (webapp/runner.py) but SQLite/SQLAlchemy round-trips it back tz-naive --
    still UTC-valued, just missing the label. Normalize both that case and a
    hypothetical tz-aware read to this host's local naive wall-clock, so
    comparisons against `datetime.now()` (this script's convention) are
    correct regardless of which shape the driver happens to return."""
    if ts is None:
        return None
    if ts.tzinfo is not None:
        return ts.astimezone().replace(tzinfo=None)
    local_offset = datetime.datetime.now().astimezone().utcoffset()
    return ts + local_offset


def _check_daily_strategy_freshness(now: datetime.datetime, links) -> None:
    """Loose staleness check for once-a-day strategies (everything except
    S007) -- see DAILY_STRATEGY_STALE_HOURS' comment for why the threshold
    is this generous."""
    for link in links:
        if link.strategy.name == "S007":
            continue
        last = _to_local_naive(link.last_cycle_at)
        if last is None or (now - last) > datetime.timedelta(hours=DAILY_STRATEGY_STALE_HOURS):
            LOG.error(
                f"{link.strategy.name} account_strategy {link.id} "
                f"({link.account.label or link.account.id}): no cycle in over "
                f"{DAILY_STRATEGY_STALE_HOURS}h (last_cycle_at={link.last_cycle_at}) -- past "
                f"its own catch-up-next-run design margin, needs a look",
                exc=RuntimeError(f"{link.strategy.name} stale beyond self-heal margin"))


def _check_scheduled_strategies() -> None:
    """DB-driven strategies (S007/S009/S011/...) only resume ticking once
    ofelia is actually back up -- a heal above fixes the container, but the
    NEXT cron minute is what resumes each job, so staleness can briefly
    outlive a heal. Alert-only (no remediation here): resuming IS what the
    next scheduled tick already does once ofelia is healthy, so doing
    anything more here would just race scheduler_tick.py's own dispatch."""
    try:
        from webapp.db import get_session
        from webapp.models import AccountStrategy
        session = get_session()
        links = (session.query(AccountStrategy)
                 .filter(AccountStrategy.enabled.is_(True)).all())
        now = datetime.datetime.now()
        _check_s007_freshness(now, links)
        _check_daily_strategy_freshness(now, links)
        session.close()
    except Exception as exc:
        LOG.error("scheduled-strategy freshness check itself failed", exc=exc)


def _check_deribit_snapshot() -> None:
    """ALGODEV-9's daily snapshot has NO catch-up path (a missed day is
    permanently lost -- expired options vanish from Deribit's API, see
    scripts/fetch_deribit_snapshot.py's module docstring), unlike the
    strategies above. So this one gets active remediation, not just an
    alert: once today's local fire hour has passed and the watermark still
    shows an earlier date, run the (documented standalone-safe) collector
    directly. Found live 2026-08-19: ofelia missed this job's exact cron
    minute on 2 consecutive days during a podman outage and nothing else
    would have ever retried it.

    Rate-limited via the script's OWN watermark file (last_attempt_ts, moved
    on every attempt whether it succeeds or not) rather than any state kept
    here -- avoids hammering Deribit's API every 5 minutes for hours if it's
    genuinely down, without needing a second piece of debounce state."""
    now = datetime.datetime.now()
    if now.hour < DERIBIT_FIRE_HOUR_LOCAL:
        return
    today_utc = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    try:
        state = json.loads(DERIBIT_STATE_PATH.read_text()) if DERIBIT_STATE_PATH.exists() else {}
    except Exception as exc:
        LOG.error("could not read deribit_snapshot.json watermark", exc=exc)
        return
    if state.get("last_snapshot_date") == today_utc:
        return   # already captured today -- quiet, matches the rest of this script

    last_attempt = state.get("last_attempt_ts")
    if last_attempt:
        try:
            attempted_at = datetime.datetime.fromisoformat(last_attempt)
            if datetime.datetime.now(datetime.timezone.utc) - attempted_at < datetime.timedelta(minutes=55):
                return   # already tried recently this hour -- let that attempt's own retry cadence work
        except ValueError:
            pass

    LOG.error(f"deribit_snapshot watermark stale (last_snapshot_date={state.get('last_snapshot_date')!r}, "
              f"today={today_utc!r}) past its {DERIBIT_FIRE_HOUR_LOCAL}:00 local fire hour -- "
              f"attempting a rescue capture (missed days cannot be recovered later)",
              exc=RuntimeError("deribit_snapshot missed its scheduled window"))
    try:
        proc = subprocess.run([sys.executable, "-m", "scripts.fetch_deribit_snapshot"],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            LOG.event("deribit_snapshot_rescued")
        else:
            LOG.error(f"deribit_snapshot rescue attempt exited {proc.returncode}: "
                      f"{(proc.stderr or '')[-500:]}", exc=RuntimeError("rescue attempt failed"))
    except Exception as exc:
        LOG.error("deribit_snapshot rescue attempt failed to start", exc=exc)


def check_and_heal() -> None:
    """One check, and (only if unhealthy) one heal attempt. No sleeping, no
    loop -- meant to return within a couple minutes at worst (a machine
    stop+start), invoked periodically by launchd.

    Found live 2026-08-10: the original version of this function treated
    "machine unreachable" (SSH itself fails) as a DIFFERENT, non-healable
    case from "machine reachable but clock drifted" -- reasoning that there
    was "nothing useful to restart-heal if the machine can't even be
    reached." That reasoning was wrong in the most common real cause of
    unreachability: the machine had fully STOPPED (`podman machine inspect`
    showed State=stopped), which `machine start` fixes directly. With the
    old logic this healthcheck sat there logging "unreachable" every 5
    minutes for 25+ minutes straight, during a live trading session with an
    open real position, doing nothing -- the exact silent-coverage-gap
    failure class this whole watchdog exists to close. Both branches now
    call the same `_heal()`."""
    host_epoch = int(time.time())
    vm_epoch = _vm_epoch()
    if vm_epoch is None:
        _heal("podman machine unreachable -- attempting restart")
    else:
        drift = abs(host_epoch - vm_epoch)
        if drift > DRIFT_THRESHOLD_SECONDS:
            _heal(f"podman machine clock drifted {drift}s behind host "
                  f"(host_epoch={host_epoch} vm_epoch={vm_epoch}) -- restarting machine + ofelia")
        # else: healthy -- quiet by default, matches the other tick scripts' convention

    # Independent of whether a heal just ran above -- these catch the actual
    # symptom (a job that silently stopped firing), not just the VM/container
    # layer a heal directly controls. See each function's own docstring for
    # why the strategies are alert-only while Deribit gets active rescue.
    _check_scheduled_strategies()
    _check_deribit_snapshot()


if __name__ == "__main__":
    check_and_heal()
