"""scripts/s007_tick.py -- the stateless per-invocation scheduler that
replaced scripts/s007_loop.sh (a bash loop with a ~17h `sleep` that silently
failed to wake, see decisions-log.md 2026-07-23) and an intermediate
always-alive Python loop design. tick() is meant to be invoked by launchd
every 60s and always returns quickly -- these tests cover its branches
(outside session / already settled / needs a cycle) using an isolated
StrategyLogger + tmp_path, exactly like tests/test_label_was_closed.py, and
never touch the real reports/logs/S007 directory.

Note on time in these tests: utils/trade_logger.py names its JSONL files by
the REAL wall-clock date (datetime.now()), not by any date tick() is handed
-- that's a property of the shared, already-tested StrategyLogger, not
something this suite should work around by hand-picking calendar dates.
in_session()'s own weekday/hour logic is tested directly (in_session takes
`now` as a plain argument, no file I/O involved, safe with any fixed date).
For tick()-level tests that also need real logger writes to line up, we use
the real `datetime.now()` for `now` (so file-naming always matches) and
monkeypatch in_session() itself to force the in-session/out-of-session
branch -- this avoids the suite becoming flaky depending on which real
weekday it happens to run on.
"""
from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import scripts.s007_tick as tick_module  # noqa: E402
from scripts.s007_tick import (  # noqa: E402
    _has_event_today, _parse_status_line, in_session, run_cycle, tick,
)
from utils.trade_logger import StrategyLogger  # noqa: E402


def _dt(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm)


def _log(tmp_path):
    return StrategyLogger("S007TEST", log_root=str(tmp_path), console=False)


# --- in_session (pure function of `now`, no file I/O -- fixed dates are fine) ---

def test_in_session_true_during_weekday_trading_hours():
    assert in_session(_dt(2026, 7, 22, 12, 0)) is True     # Wed


def test_in_session_false_on_weekend():
    assert in_session(_dt(2026, 7, 25, 12, 0)) is False    # Sat
    assert in_session(_dt(2026, 7, 26, 12, 0)) is False    # Sun


def test_in_session_false_before_trade_start():
    assert in_session(_dt(2026, 7, 22, 9, 59)) is False


def test_in_session_true_at_exit_end_hour():
    assert in_session(_dt(2026, 7, 22, 16, 59)) is True    # EXIT_END="16:59"


def test_in_session_false_after_exit_end_hour():
    assert in_session(_dt(2026, 7, 22, 17, 0)) is False


# --- _has_event_today ---------------------------------------------------------
# Uses datetime.date.today() throughout (matching the real file StrategyLogger
# actually writes to), not a hand-picked calendar date.

def test_has_event_today_false_when_log_missing(tmp_path):
    assert _has_event_today(tmp_path, "S007TEST", datetime.date.today(), "loop_settled") is False


def test_has_event_today_true_after_matching_event(tmp_path):
    log = _log(tmp_path)
    log.event("loop_settled", reason="day_done", today=str(datetime.date.today()))
    assert _has_event_today(tmp_path, "S007TEST", datetime.date.today(), "loop_settled") is True


def test_has_event_today_ignores_other_kinds(tmp_path):
    log = _log(tmp_path)
    log.event("state", symbol="DE40")
    assert _has_event_today(tmp_path, "S007TEST", datetime.date.today(), "loop_settled") is False


def test_has_event_today_respects_since_cutoff(tmp_path):
    log = _log(tmp_path)
    log.event("loop_heartbeat", now="earlier")
    now = datetime.datetime.now()
    assert _has_event_today(tmp_path, "S007TEST", now.date(), "loop_heartbeat",
                             since=now + datetime.timedelta(hours=1)) is False
    assert _has_event_today(tmp_path, "S007TEST", now.date(), "loop_heartbeat",
                             since=now - datetime.timedelta(hours=1)) is True


# --- _parse_status_line / run_cycle -------------------------------------------

def test_parse_status_line_extracts_booleans():
    stdout = "cycle done: 0 actions\nSTATUS day_done=False in_window=True filtered=True actions=0\n"
    assert _parse_status_line(stdout) == dict(day_done=False, in_window=True, filtered=True, actions="0")


def test_parse_status_line_missing_returns_empty():
    assert _parse_status_line("some unrelated crash traceback\n") == {}


def test_run_cycle_returns_parsed_status_on_success(monkeypatch):
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(
            args=a, returncode=0,
            stdout="STATUS day_done=True in_window=True filtered=False actions=2\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_cycle()
    assert result["day_done"] is True
    assert result["filtered"] is False


def test_run_cycle_returns_empty_on_timeout(monkeypatch):
    def fake_run(*a, **k):
        raise subprocess.TimeoutExpired(cmd="bot.s007_paper", timeout=120)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_cycle() == {}


def test_run_cycle_returns_empty_on_crash_before_status_line(monkeypatch):
    def fake_run(*a, **k):
        return subprocess.CompletedProcess(args=a, returncode=1, stdout="", stderr="Traceback...\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_cycle() == {}


# --- tick ----------------------------------------------------------------------
# in_session() is monkeypatched to a fixed True/False so these tests exercise
# tick()'s branching regardless of the real weekday/hour the suite runs at;
# `now` stays real (datetime.now()) so the logger's file-naming lines up with
# what _has_event_today looks for.

def test_tick_outside_session_does_not_run_a_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_module, "in_session", lambda now: False)
    called = []
    monkeypatch.setattr(tick_module, "run_cycle", lambda: called.append(1) or {})
    tick(now=datetime.datetime.now(), log=_log(tmp_path), log_root=tmp_path, strategy="S007TEST")
    assert called == []


def test_tick_outside_session_logs_one_heartbeat_not_every_time(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_module, "in_session", lambda now: False)
    monkeypatch.setattr(tick_module, "run_cycle", lambda: {})
    log = _log(tmp_path)
    now = datetime.datetime.now()
    tick(now=now, log=log, log_root=tmp_path, strategy="S007TEST")
    tick(now=now + datetime.timedelta(minutes=1), log=log, log_root=tmp_path, strategy="S007TEST")
    path = tmp_path / "S007TEST" / f"events-{now.date().isoformat()}.jsonl"
    heartbeats = [line for line in path.read_text().splitlines() if '"kind": "loop_heartbeat"' in line]
    assert len(heartbeats) == 1   # second tick, 1 minute later, is inside HEARTBEAT_WINDOW


def test_tick_in_session_and_not_settled_runs_a_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_module, "in_session", lambda now: True)
    called = []
    monkeypatch.setattr(tick_module, "run_cycle", lambda: called.append(1) or {})
    tick(now=datetime.datetime.now(), log=_log(tmp_path), log_root=tmp_path, strategy="S007TEST")
    assert called == [1]


def test_tick_records_loop_settled_when_cycle_reports_filtered(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_module, "in_session", lambda now: True)
    monkeypatch.setattr(tick_module, "run_cycle", lambda: {"filtered": True})
    log = _log(tmp_path)
    now = datetime.datetime.now()
    tick(now=now, log=log, log_root=tmp_path, strategy="S007TEST")
    assert _has_event_today(tmp_path, "S007TEST", now.date(), "loop_settled") is True


def test_tick_already_settled_today_does_not_run_a_cycle_again(tmp_path, monkeypatch):
    monkeypatch.setattr(tick_module, "in_session", lambda now: True)
    called = []
    monkeypatch.setattr(tick_module, "run_cycle", lambda: called.append(1) or {})
    log = _log(tmp_path)
    now = datetime.datetime.now()
    log.event("loop_settled", reason="day_done", today=str(now.date()))
    tick(now=now, log=log, log_root=tmp_path, strategy="S007TEST")
    assert called == []


def test_tick_crashed_cycle_is_not_treated_as_settled(tmp_path, monkeypatch):
    # run_cycle() returns {} on a crash/timeout -- must NOT be read as day_done/filtered.
    monkeypatch.setattr(tick_module, "in_session", lambda now: True)
    monkeypatch.setattr(tick_module, "run_cycle", lambda: {})
    log = _log(tmp_path)
    now = datetime.datetime.now()
    tick(now=now, log=log, log_root=tmp_path, strategy="S007TEST")
    assert _has_event_today(tmp_path, "S007TEST", now.date(), "loop_settled") is False
