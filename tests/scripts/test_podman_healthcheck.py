"""scripts/podman_healthcheck.py -- watchdog for the Podman-machine
clock-freeze-after-sleep bug found live 2026-08-10 (see that module's
docstring for the incident: the VM's clock froze at the moment of a host
sleep and silently stayed 18+ hours behind, with `podman ps` reporting
healthy the whole time, silently stopping S007/S009 dispatch).

subprocess.run is monkeypatched throughout -- these tests must never touch
a real Podman machine. fake_run() below dispatches by the podman subcommand
(not call position/order) so adding a new kind of call to _heal() doesn't
require rewriting every test's response sequence.
"""
from __future__ import annotations

import json
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


def _make_fake_run(vm_epochs, ofelia_running=True, calls=None):
    """vm_epochs: iterator of stdout values for successive `machine ssh ...
    date +%s` calls (the ONLY call whose response actually varies test to
    test). Every other podman subcommand gets a canned success response --
    `machine inspect` returns a fixed ConnectionInfo blob, `ps` reports
    ofelia as running/not per `ofelia_running`, everything else is a plain
    rc=0 no-op."""
    inspect_stdout = json.dumps([{"ConnectionInfo": {"PodmanSocket": {"Path": "/tmp/fake.sock"}}}])

    def fake_run(args, **kw):
        if calls is not None:
            calls.append(args)
        if args[1:3] == ["machine", "ssh"]:
            return next(vm_epochs)
        if args[1:3] == ["machine", "inspect"]:
            return _proc(inspect_stdout)
        if args[1] == "ps":
            return _proc("algo-ofelia-1\n" if ofelia_running else "")
        return _proc()

    return fake_run


def test_in_sync_clock_is_a_quiet_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    now = int(time.time())
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        _make_fake_run(iter([_proc(f"{now}\n")]), calls=calls))

    hc.check_and_heal()

    assert len(calls) == 1   # only the read check -- no machine stop/start/compose calls
    assert calls[0][:3] == [hc.PODMAN, "machine", "ssh"]


def test_drifted_clock_triggers_machine_restart_and_compose_up(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    stale = int(time.time()) - 3600   # 1h behind -- past DRIFT_THRESHOLD_SECONDS
    fresh = int(time.time())
    calls = []
    monkeypatch.setattr(subprocess, "run", _make_fake_run(
        iter([_proc(f"{stale}\n"), _proc(f"{fresh}\n")]), calls=calls))

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
    monkeypatch.setattr(subprocess, "run",
                        _make_fake_run(iter([_proc(f"{stale}\n"), _proc(f"{fresh}\n")])))

    hc.check_and_heal()

    events = (tmp_path / "HealthcheckTest" / f"events-{time.strftime('%Y-%m-%d')}.jsonl").read_text()
    assert '"kind": "healed"' in events


def test_heal_failure_is_logged_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    stale = int(time.time()) - 3600
    still_stale = int(time.time()) - 3600
    # stop/start/compose "succeed" (return 0) but the post-heal check still
    # shows the machine drifted -- must not raise, must log loudly instead
    monkeypatch.setattr(subprocess, "run",
                        _make_fake_run(iter([_proc(f"{stale}\n"), _proc(f"{still_stale}\n")])))

    hc.check_and_heal()   # must not raise


def test_unreachable_machine_triggers_heal_same_as_drift(tmp_path, monkeypatch):
    # Found live 2026-08-10: a fully-stopped machine also fails the SSH read
    # (returncode != 0), and that case IS healable via machine start -- the
    # old behavior (log and give up) left the watchdog doing nothing for
    # 25+ minutes during live trading. See check_and_heal()'s docstring.
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    fresh = int(time.time())
    calls = []
    monkeypatch.setattr(subprocess, "run", _make_fake_run(
        iter([_proc(returncode=1), _proc(f"{fresh}\n")]), calls=calls))

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


def test_compose_env_resolves_docker_host_from_machine_inspect(tmp_path, monkeypatch):
    """Found live 2026-08-13: `machine start` sometimes falls back to a
    per-start temp socket path instead of the default /var/run/docker.sock
    (logged by podman as "Another process was listening..."), and
    `podman compose` needs DOCKER_HOST pointed at whatever path was ACTUALLY
    bound or it fails outright. _compose_env() must read that real path
    from `machine inspect`, not assume the default."""
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    monkeypatch.setattr(subprocess, "run", _make_fake_run(iter([])))

    env = hc._compose_env()

    assert env["DOCKER_HOST"] == "unix:///tmp/fake.sock"


def test_ofelia_not_running_after_first_compose_triggers_retry(tmp_path, monkeypatch):
    """If ofelia isn't up after the main stop/start/compose sequence, heal
    must retry the compose step alone (with a freshly-resolved socket path)
    instead of declaring victory on clock-sync alone -- clock_ok was already
    logged as "healed" once live while ofelia itself stayed down (the
    compose call had silently failed on the socket-path mismatch above)."""
    monkeypatch.setattr(hc, "LOG", _log(tmp_path))
    stale = int(time.time()) - 3600
    fresh = int(time.time())
    calls = []

    ps_call_count = 0

    def fake_run(args, **kw):
        nonlocal ps_call_count
        calls.append(args)
        if args[1:3] == ["machine", "ssh"]:
            return _proc(f"{stale}\n") if len(
                [c for c in calls if c[1:3] == ["machine", "ssh"]]) == 1 else _proc(f"{fresh}\n")
        if args[1:3] == ["machine", "inspect"]:
            return _proc(json.dumps([{"ConnectionInfo": {"PodmanSocket": {"Path": "/tmp/fake.sock"}}}]))
        if args[1] == "ps":
            ps_call_count += 1
            # not running on the first check, running on the retry's re-check
            return _proc("" if ps_call_count == 1 else "algo-ofelia-1\n")
        return _proc()

    monkeypatch.setattr(subprocess, "run", fake_run)
    hc.check_and_heal()

    compose_calls = [c for c in calls if c[1:3] == ["compose", "up"]]
    assert len(compose_calls) == 2   # main pass + retry

    events = (tmp_path / "HealthcheckTest" / f"events-{time.strftime('%Y-%m-%d')}.jsonl").read_text()
    assert '"kind": "healed"' in events
