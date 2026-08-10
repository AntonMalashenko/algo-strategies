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

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from utils.trade_logger import StrategyLogger  # noqa: E402

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

    for args, timeout in (
        ([PODMAN, "machine", "stop", MACHINE_NAME], MACHINE_STOP_TIMEOUT_SECONDS),
        ([PODMAN, "machine", "start", MACHINE_NAME], MACHINE_START_TIMEOUT_SECONDS),
        ([PODMAN, "compose", "up", "-d", "ofelia"], COMPOSE_TIMEOUT_SECONDS),
    ):
        try:
            subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
        except Exception as exc:
            LOG.error(f"heal step {args!r} failed", exc=exc)

    new_vm_epoch = _vm_epoch()
    if new_vm_epoch is not None and abs(int(time.time()) - new_vm_epoch) <= DRIFT_THRESHOLD_SECONDS:
        LOG.event("healed")
    else:
        LOG.error("podman machine restart did not fix it -- needs manual attention",
                  exc=RuntimeError(f"still unreachable or drifted after restart (new_vm_epoch={new_vm_epoch})"))


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
        return

    drift = abs(host_epoch - vm_epoch)
    if drift <= DRIFT_THRESHOLD_SECONDS:
        return   # healthy -- quiet by default, matches the other tick scripts' convention

    _heal(f"podman machine clock drifted {drift}s behind host "
          f"(host_epoch={host_epoch} vm_epoch={vm_epoch}) -- restarting machine + ofelia")


if __name__ == "__main__":
    check_and_heal()
