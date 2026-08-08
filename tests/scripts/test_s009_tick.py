"""scripts/s009_tick.py -- the stateless per-invocation scheduler for S009
(funding-carry), same StartCalendarInterval design as scripts/s007_tick.py
(see that module's docstring, and decisions-log.md 2026-07-23, for why a
long-lived sleeping loop was replaced by this pattern).

Regression coverage for the 2026-08-07 bug: tick()'s default `now` was
`datetime.datetime.now(timezone.utc)` (aware) while StrategyLogger writes
event `ts` fields via naive local `datetime.now()` (utils/trade_logger.py) --
`_has_event_today` then compared an aware `since` against a naive `ts` and
raised "can't compare offset-naive and offset-aware datetimes" on every idle
tick once a heartbeat had already been logged for the day. This is the same
failure signature as the two "S009 cycle subprocess exited non-zero" entries
in reports/logs/S009/S009.log on 2026-08-03. The fix makes `now` default to
naive local time, matching the logger and scripts/s007_tick.py's own default.
"""
from __future__ import annotations

import datetime
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import scripts.s009_tick as tick_module  # noqa: E402
from scripts.s009_tick import _has_event_today, tick  # noqa: E402
from utils.trade_logger import StrategyLogger  # noqa: E402


def _log(tmp_path):
    return StrategyLogger("S009TEST", log_root=str(tmp_path), console=False)


def _up_to_date(monkeypatch, last_day=100):
    monkeypatch.setattr(tick_module, "load_state", lambda: {"last_day": last_day})
    monkeypatch.setattr(tick_module, "_expected_last_closed_day", lambda: last_day)


def _needs_cycle(monkeypatch, before=99, after=100):
    state = {"last_day": before}
    monkeypatch.setattr(tick_module, "load_state", lambda: dict(state))
    monkeypatch.setattr(tick_module, "_expected_last_closed_day", lambda: after)
    return state


# --- _has_event_today (naive `since`, matching the logger's naive `ts`) -------

def test_has_event_today_respects_since_cutoff(tmp_path):
    log = _log(tmp_path)
    log.event("loop_heartbeat", now="earlier")
    now = datetime.datetime.now()
    assert _has_event_today(tmp_path, "S009TEST", now.date(), "loop_heartbeat",
                             since=now + datetime.timedelta(hours=1)) is False
    assert _has_event_today(tmp_path, "S009TEST", now.date(), "loop_heartbeat",
                             since=now - datetime.timedelta(hours=1)) is True


# --- tick: up-to-date branch ----------------------------------------------------

def test_tick_up_to_date_does_not_run_a_cycle(tmp_path, monkeypatch):
    _up_to_date(monkeypatch)
    called = []
    monkeypatch.setattr(tick_module, "run_cycle", lambda *a, **k: called.append(1) or None)
    tick(now=datetime.datetime.now(), log=_log(tmp_path), log_root=tmp_path, strategy="S009TEST")
    assert called == []


def test_tick_up_to_date_two_consecutive_ticks_do_not_crash_and_dedupe_heartbeat(tmp_path, monkeypatch):
    # The actual regression: a second up-to-date tick, shortly after the first
    # already logged a heartbeat, used to raise TypeError comparing the
    # (aware) `since` cutoff against the (naive) `ts` already on disk.
    _up_to_date(monkeypatch)
    log = _log(tmp_path)
    now = datetime.datetime.now()
    tick(now=now, log=log, log_root=tmp_path, strategy="S009TEST")
    tick(now=now + datetime.timedelta(minutes=1), log=log, log_root=tmp_path, strategy="S009TEST")
    path = tmp_path / "S009TEST" / f"events-{now.date().isoformat()}.jsonl"
    heartbeats = [line for line in path.read_text().splitlines() if '"kind": "loop_heartbeat"' in line]
    assert len(heartbeats) == 1   # second tick, 1 minute later, is inside HEARTBEAT_WINDOW


def test_tick_up_to_date_default_now_does_not_crash(tmp_path, monkeypatch):
    # Exercises the real default (`now=None` -> datetime.now()), not a
    # test-supplied clock, since the bug was specifically in that default.
    _up_to_date(monkeypatch)
    log = _log(tmp_path)
    tick(log=log, log_root=tmp_path, strategy="S009TEST")
    tick(log=log, log_root=tmp_path, strategy="S009TEST")


# --- tick: needs-a-cycle branch --------------------------------------------------

def test_tick_needs_cycle_runs_a_cycle_and_settles(tmp_path, monkeypatch):
    state = _needs_cycle(monkeypatch, before=99, after=100)

    def fake_run_cycle(broker, allow_mainnet):
        state["last_day"] = 100   # simulate bot/s009_paper.py --once advancing state.json
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(tick_module, "run_cycle", fake_run_cycle)

    log = _log(tmp_path)
    now = datetime.datetime.now()
    tick(now=now, log=log, log_root=tmp_path, strategy="S009TEST", broker="off")
    assert _has_event_today(tmp_path, "S009TEST", now.date(), "loop_settled") is True


def test_tick_cycle_timeout_does_not_crash_or_settle(tmp_path, monkeypatch):
    _needs_cycle(monkeypatch, before=99, after=100)
    monkeypatch.setattr(tick_module, "run_cycle", lambda broker, allow_mainnet: None)
    log = _log(tmp_path)
    now = datetime.datetime.now()
    tick(now=now, log=log, log_root=tmp_path, strategy="S009TEST")
    assert _has_event_today(tmp_path, "S009TEST", now.date(), "loop_settled") is False


def test_tick_cycle_nonzero_exit_logs_error_not_settled(tmp_path, monkeypatch):
    _needs_cycle(monkeypatch, before=99, after=100)
    monkeypatch.setattr(
        tick_module, "run_cycle",
        lambda broker, allow_mainnet: subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom"))
    log = _log(tmp_path)
    now = datetime.datetime.now()
    tick(now=now, log=log, log_root=tmp_path, strategy="S009TEST")
    assert _has_event_today(tmp_path, "S009TEST", now.date(), "loop_settled") is False


def test_tick_cycle_exits_zero_but_state_did_not_advance_is_not_settled(tmp_path, monkeypatch):
    # Cycle claims success but state.json didn't move -- must be surfaced
    # loudly (an error), not silently recorded as settled.
    _needs_cycle(monkeypatch, before=99, after=100)
    monkeypatch.setattr(
        tick_module, "run_cycle",
        lambda broker, allow_mainnet: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""))
    log = _log(tmp_path)
    now = datetime.datetime.now()
    tick(now=now, log=log, log_root=tmp_path, strategy="S009TEST")
    assert _has_event_today(tmp_path, "S009TEST", now.date(), "loop_settled") is False
