"""scripts/podman_healthcheck.py -- watchdog for the Podman-machine
clock-freeze-after-sleep bug found live 2026-08-10 (see that module's
docstring for the incident: the VM's clock froze at the moment of a host
sleep and silently stayed 18+ hours behind, with `podman ps` reporting
healthy the whole time, silently stopping S007/S009 dispatch).

subprocess.run is monkeypatched throughout -- these tests must never touch
a real Podman machine.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import scripts.podman_healthcheck as hc  # noqa: E402


def _proc(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _log(tmp_path):
    from utils.trade_logger import StrategyLogger
    return StrategyLogger("HealthcheckTest", log_root=str(tmp_path), console=False)


def test_in_sync_clock_is_a_quiet_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    now = int(time.time())
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append(args) or _proc(f"{now}\n"))

    hc.check_and_heal()

    assert len(calls) == 1   # only the read check -- no machine stop/start/compose calls
    assert calls[0][:3] == [hc.PODMAN, "machine", "ssh"]


def test_drifted_clock_triggers_machine_restart_and_compose_up(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    stale = int(time.time()) - 3600   # 1h behind -- past DRIFT_THRESHOLD_SECONDS
    fresh = int(time.time())
    responses = iter([_proc(f"{stale}\n"), _proc(), _proc(), _proc(), _proc(f"{fresh}\n")])
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return next(responses)

    monkeypatch.setattr(subprocess, "run", fake_run)
    hc.check_and_heal()

    kinds = [c[1:3] for c in calls]
    assert ["machine", "stop"] in kinds
    assert ["machine", "start"] in kinds
    assert ["compose", "up"] in kinds
    # order matters: stop must come before start
    assert kinds.index(["machine", "stop"]) < kinds.index(["machine", "start"])


def test_heal_success_logs_healed(tmp_path, monkeypatch):
    log = _log(tmp_path)
    monkeypatch.setattr(hc, "LOG", log)
    stale = int(time.time()) - 3600
    fresh = int(time.time())
    responses = iter([_proc(f"{stale}\n"), _proc(), _proc(), _proc(), _proc(f"{fresh}\n")])
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: next(responses))

    hc.check_and_heal()

    events = (tmp_path / "HealthcheckTest" / f"events-{time.strftime('%Y-%m-%d')}.jsonl").read_text()
    assert '"kind": "healed"' in events


def test_heal_failure_is_logged_not_raised(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    stale = int(time.time()) - 3600
    still_stale = int(time.time()) - 3600
    # stop/start/compose "succeed" (return 0) but the post-heal check still
    # shows the machine drifted -- must not raise, must log loudly instead
    responses = iter([_proc(f"{stale}\n"), _proc(), _proc(), _proc(), _proc(f"{still_stale}\n")])
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: next(responses))

    hc.check_and_heal()   # must not raise


def test_unreachable_machine_triggers_heal_same_as_drift(tmp_path, monkeypatch):
    # Found live 2026-08-10: a fully-stopped machine also fails the SSH read
    # (returncode != 0), and that case IS healable via machine start -- the
    # old behavior (log and give up) left the watchdog doing nothing for
    # 25+ minutes during live trading. See check_and_heal()'s docstring.
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    fresh = int(time.time())
    # 1st call: read fails (unreachable) -> heal attempted -> stop, start,
    # compose up, then a final read that now succeeds (healed).
    responses = iter([_proc(returncode=1), _proc(), _proc(), _proc(), _proc(f"{fresh}\n")])
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return next(responses)

    monkeypatch.setattr(subprocess, "run", fake_run)
    hc.check_and_heal()

    kinds = [c[1:3] for c in calls]
    assert ["machine", "start"] in kinds
    assert ["compose", "up"] in kinds


def test_ssh_exception_treated_same_as_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))

    def raise_timeout(args, **kw):
        raise subprocess.TimeoutExpired(cmd=args, timeout=30)

    monkeypatch.setattr(subprocess, "run", raise_timeout)
    hc.check_and_heal()   # must not raise
