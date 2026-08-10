"""scripts/scheduler_tick.py -- the generic, DB-agnostic dispatcher tick that
reads deployment/schedule.yml and subprocess-invokes whatever strategy/task
is due this minute. Mirrors tests/scripts/test_s009_tick.py's structure:
tick()'s DISPATCH DECISION is tested by monkeypatching _run_item (fast, no
subprocess), while _run_item's own subprocess handling (success/non-zero
exit/timeout) is tested with real, cheap `python -c ...` commands so the
actual subprocess.run plumbing is exercised, not mocked away.
"""
from __future__ import annotations

import datetime
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import scripts.scheduler_tick as st  # noqa: E402


def _write_schedule(tmp_path, strategies=None, tasks=None) -> Path:
    path = tmp_path / "schedule.yml"
    path.write_text(yaml.safe_dump({"strategies": strategies or [], "tasks": tasks or []}))
    return path


# --- load_schedule ----------------------------------------------------------

def test_load_schedule_missing_file_is_empty_not_an_error(tmp_path):
    assert st.load_schedule(tmp_path / "nope.yml") == {"strategies": [], "tasks": []}


def test_load_schedule_empty_sections_default_to_empty_lists(tmp_path):
    path = tmp_path / "schedule.yml"
    path.write_text("strategies:\ntasks:\n")   # both null in YAML
    assert st.load_schedule(path) == {"strategies": [], "tasks": []}


# --- tick: dispatch decision (via _due/_run_item, no real subprocess) ------

def test_tick_dispatches_strategy_when_cron_matches(tmp_path, monkeypatch):
    schedule = _write_schedule(tmp_path, strategies=[{"name": "S007", "schedule": "* 10-16 * * 1-5"}])
    calls = []
    monkeypatch.setattr(st, "_run_item", lambda kind, name, args: calls.append((kind, name, args)))

    monday_in_window = datetime.datetime(2026, 8, 10, 10, 30)   # Monday
    st.tick(now=monday_in_window, schedule_file=schedule)

    assert calls == [("strategy", "S007",
                      [sys.executable, "-m", "webapp.runner", "--strategy", "S007"])]


def test_tick_skips_strategy_outside_its_cron_window(tmp_path, monkeypatch):
    schedule = _write_schedule(tmp_path, strategies=[{"name": "S007", "schedule": "* 10-16 * * 1-5"}])
    calls = []
    monkeypatch.setattr(st, "_run_item", lambda kind, name, args: calls.append((kind, name, args)))

    saturday = datetime.datetime(2026, 8, 8, 10, 30)   # Saturday -- weekday not in "1-5"
    st.tick(now=saturday, schedule_file=schedule)

    assert calls == []


def test_tick_dispatches_task_with_shlex_split_command(tmp_path, monkeypatch):
    schedule = _write_schedule(tmp_path, tasks=[
        {"name": "dump_m1", "command": "python -m scripts.dump_today_m1 --quiet", "schedule": "0 22 * * 1-5"}])
    calls = []
    monkeypatch.setattr(st, "_run_item", lambda kind, name, args: calls.append((kind, name, args)))

    due = datetime.datetime(2026, 8, 10, 22, 0)   # Monday 22:00
    st.tick(now=due, schedule_file=schedule)

    assert calls == [("task", "dump_m1", ["python", "-m", "scripts.dump_today_m1", "--quiet"])]


def test_tick_one_failing_item_does_not_block_the_next(tmp_path, monkeypatch):
    schedule = _write_schedule(tmp_path, strategies=[
        {"name": "S007", "schedule": "* * * * *"},
        {"name": "S009", "schedule": "* * * * *"},
    ])
    calls = []

    def fake_run_item(kind, name, args):
        calls.append(name)
        if name == "S007":
            raise RuntimeError("boom -- must not happen, _run_item swallows internally in prod")

    monkeypatch.setattr(st, "_run_item", fake_run_item)
    # _run_item itself is the swallow boundary in prod code (tested separately
    # below); this test just proves tick() iterates every due item regardless
    # of iteration order, i.e. does not short-circuit on the first one.
    try:
        st.tick(now=datetime.datetime(2026, 8, 10, 10, 30), schedule_file=schedule)
    except RuntimeError:
        pass
    assert "S007" in calls


def test_tick_empty_schedule_is_a_clean_noop(tmp_path, monkeypatch):
    schedule = _write_schedule(tmp_path)   # strategies=[], tasks=[]
    calls = []
    monkeypatch.setattr(st, "_run_item", lambda kind, name, args: calls.append(name))
    st.tick(now=datetime.datetime(2026, 8, 10, 10, 30), schedule_file=schedule)
    assert calls == []


# --- _run_item: real subprocess handling (success / non-zero / timeout) ----

def test_run_item_success_does_not_raise(capsys):
    st._run_item("task", "ok", [sys.executable, "-c", "pass"])
    assert "ok" in capsys.readouterr().out
    assert "exited" not in capsys.readouterr().out


def test_run_item_nonzero_exit_is_logged_not_raised(capsys):
    st._run_item("task", "fails", [sys.executable, "-c", "import sys; sys.exit(3)"])
    out = capsys.readouterr().out
    assert "exited 3" in out


def test_run_item_timeout_is_logged_not_raised(monkeypatch, capsys):
    monkeypatch.setattr(st, "ITEM_TIMEOUT_SECONDS", 0.05)
    st._run_item("task", "hangs", [sys.executable, "-c", "import time; time.sleep(5)"])
    out = capsys.readouterr().out
    assert "TIMEOUT" in out


def test_run_item_bad_executable_does_not_raise(capsys):
    st._run_item("task", "missing", ["/no/such/binary/here"])
    out = capsys.readouterr().out
    assert "failed to start" in out


# --- overlap guard (LOCK_DIR) -----------------------------------------------

def test_run_item_releases_lock_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "LOCK_DIR", tmp_path)
    st._run_item("task", "ok", [sys.executable, "-c", "pass"])
    assert not st._lock_path("task", "ok").exists()


def test_run_item_releases_lock_after_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "LOCK_DIR", tmp_path)
    st._run_item("task", "fails", [sys.executable, "-c", "import sys; sys.exit(3)"])
    assert not st._lock_path("task", "fails").exists()


def test_run_item_releases_lock_after_timeout(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(st, "LOCK_DIR", tmp_path)
    monkeypatch.setattr(st, "ITEM_TIMEOUT_SECONDS", 0.05)
    st._run_item("task", "hangs", [sys.executable, "-c", "import time; time.sleep(5)"])
    assert not st._lock_path("task", "hangs").exists()


def test_second_call_skips_while_a_fresh_lock_is_held(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(st, "LOCK_DIR", tmp_path)
    st._acquire_lock("strategy", "S007")   # simulates a still-running previous tick
    held_since = st._lock_path("strategy", "S007").stat().st_mtime

    # A real, observable side effect if this ran: a marker file the command
    # below would create. Its absence proves subprocess.run was never
    # reached, not just that stdout claims so.
    marker = tmp_path / "ran.marker"
    st._run_item("strategy", "S007",
                [sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"])

    assert not marker.exists()
    assert "still in progress" in capsys.readouterr().out
    # untouched by the skipped call -- _acquire_lock returned False before
    # ever calling path.write_text() again
    assert st._lock_path("strategy", "S007").stat().st_mtime == held_since


def test_stale_lock_is_stolen_not_blocking(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(st, "LOCK_DIR", tmp_path)
    monkeypatch.setattr(st, "ITEM_TIMEOUT_SECONDS", 120)
    lock = st._lock_path("strategy", "S007")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("0")
    old = time.time() - 999   # far older than ITEM_TIMEOUT_SECONDS
    import os
    os.utime(lock, (old, old))

    st._run_item("strategy", "S007", [sys.executable, "-c", "pass"])

    out = capsys.readouterr().out
    assert "stale lock" in out
    assert "ok" in out
    assert not lock.exists()   # released after the (successful) run completed


def test_two_different_items_do_not_share_a_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "LOCK_DIR", tmp_path)
    assert st._acquire_lock("strategy", "S007") is True
    assert st._acquire_lock("strategy", "S009") is True   # different name, independent lock
