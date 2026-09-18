"""scripts/s007_watchdog.py (ALGODEV-45 step 1) -- pure decision logic
(expected_to_run/evaluate/is_settled_for_today take plain, naive-UTC
arguments, no I/O), tested directly per the same style as
tests/scripts/test_s007_tick.py's in_session() tests.

All datetimes here are naive UTC, matching AccountStrategy.last_cycle_at
(always written as datetime.now(timezone.utc) -- see webapp/runner.py) and
what scripts/s007_watchdog.py::main() actually passes in. The trading-window
check converts to Europe/Kyiv internally (deployment/schedule.yml's cron
window is local-time) -- 2026-09-17 is EEST (summer, UTC+3), so a Kyiv
10:00-16:59 window is UTC 07:00-13:59 on these dates. Found and fixed
2026-09-17: an earlier version of this module compared UTC last_cycle_at
against container-LOCAL datetime.now() directly, misjudging every healthy
cycle as stalled by exactly the current DST offset (180.8 "minutes stale"
on a cycle that was seconds old) -- these tests exist specifically to catch
that class of bug again.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.s007_watchdog import (  # noqa: E402
    STALE_THRESHOLD_MINUTES, evaluate, expected_to_run, is_settled_for_today,
)

# Kyiv 10:00-16:59 on 2026-09-17 (EEST, UTC+3) == UTC 07:00-13:59.
UTC_SESSION_START = datetime(2026, 9, 17, 7, 0)
UTC_SESSION_MID = datetime(2026, 9, 17, 10, 30)
UTC_SESSION_END = datetime(2026, 9, 17, 13, 59)


class TestExpectedToRun:
    def test_weekday_in_hours(self):
        assert expected_to_run(UTC_SESSION_START)  # Thursday
        assert expected_to_run(UTC_SESSION_END)

    def test_weekday_before_open(self):
        assert not expected_to_run(UTC_SESSION_START - timedelta(minutes=1))

    def test_weekday_after_close(self):
        assert not expected_to_run(UTC_SESSION_END + timedelta(minutes=1))

    def test_weekend(self):
        assert not expected_to_run(datetime(2026, 9, 19, 9, 0))  # Saturday, UTC 09:00 = Kyiv noon
        assert not expected_to_run(datetime(2026, 9, 20, 9, 0))  # Sunday


class TestIsSettledForToday:
    def test_none_status_not_settled(self):
        assert not is_settled_for_today(None, UTC_SESSION_MID)

    def test_other_status_not_settled(self):
        assert not is_settled_for_today("idle", UTC_SESSION_MID)
        assert not is_settled_for_today("error", UTC_SESSION_MID)

    def test_settled_today_kyiv_date(self):
        # UTC_SESSION_MID (2026-09-17 10:30 UTC) is 2026-09-17 13:30 Kyiv --
        # same calendar date either way this time of day.
        assert is_settled_for_today("settled:2026-09-17", UTC_SESSION_MID)

    def test_settled_yesterday_is_not_settled_today(self):
        assert not is_settled_for_today("settled:2026-09-16", UTC_SESSION_MID)

    def test_settled_date_uses_kyiv_not_utc(self):
        # 2026-09-17 22:30 UTC is already 2026-09-18 01:30 Kyiv (EEST, +3) --
        # a status stamped for the Kyiv date "2026-09-18" must read as
        # settled against this UTC instant, not "settled:2026-09-17".
        late_utc = datetime(2026, 9, 17, 22, 30)
        assert is_settled_for_today("settled:2026-09-18", late_utc)
        assert not is_settled_for_today("settled:2026-09-17", late_utc)


class TestEvaluate:
    def test_never_run_yet_no_alert(self):
        now = UTC_SESSION_MID
        new_state, alert = evaluate(now, None, None, {})
        assert alert is None
        assert new_state == {}

    def test_fresh_cycle_no_alert(self):
        now = UTC_SESSION_MID
        last = now - timedelta(minutes=1)
        new_state, alert = evaluate(now, last, None, {})
        assert alert is None
        assert new_state == {"alerted_for": None}

    def test_stale_past_threshold_alerts_once(self):
        now = UTC_SESSION_MID
        last = now - timedelta(minutes=STALE_THRESHOLD_MINUTES + 1)
        state, alert = evaluate(now, last, None, {})
        assert alert is not None
        assert "ALERT" in alert
        assert state == {"alerted_for": last.isoformat()}

        # same outage, checked again a minute later -- must NOT re-alert
        now2 = now + timedelta(minutes=1)
        state2, alert2 = evaluate(now2, last, None, state)
        assert alert2 is None
        assert state2 == state

    def test_stale_exactly_at_threshold_is_not_yet_an_alert(self):
        now = UTC_SESSION_MID
        last = now - timedelta(minutes=STALE_THRESHOLD_MINUTES)
        _state, alert = evaluate(now, last, None, {})
        assert alert is None

    def test_recovery_after_alert(self):
        now = UTC_SESSION_MID
        stale_last = now - timedelta(minutes=STALE_THRESHOLD_MINUTES + 1)
        state, _alert = evaluate(now, stale_last, None, {})
        assert state["alerted_for"] == stale_last.isoformat()

        # a fresh cycle finally lands
        now2 = now + timedelta(minutes=1)
        fresh_last = now2 - timedelta(seconds=5)
        state2, alert2 = evaluate(now2, fresh_last, None, state)
        assert alert2 is not None
        assert "RECOVERED" in alert2
        assert state2 == {"alerted_for": None}

    def test_outside_window_never_alerted_stays_silent(self):
        now = datetime(2026, 9, 19, 9, 0)  # Saturday
        stale_last = now - timedelta(hours=40)
        state, alert = evaluate(now, stale_last, None, {})
        assert alert is None
        assert state == {}

    def test_outside_window_while_mid_alert_reports_once(self):
        # e.g. the trading window closed for the day while S007 was stalled
        now = UTC_SESSION_MID
        stale_last = now - timedelta(minutes=STALE_THRESHOLD_MINUTES + 1)
        state, _alert = evaluate(now, stale_last, None, {})

        after_hours = UTC_SESSION_END + timedelta(hours=1)
        state2, alert2 = evaluate(after_hours, stale_last, None, state)
        assert alert2 is not None
        assert "RECOVERED" in alert2 or "session window ended" in alert2
        assert state2 == {"alerted_for": None}

        # further checks after hours stay silent (already reported)
        state3, alert3 = evaluate(after_hours + timedelta(minutes=5), stale_last, None, state2)
        assert alert3 is None
        assert state3 == state2

    def test_settled_for_today_never_alerts_even_when_frozen_for_hours(self):
        """The actual live incident, 2026-09-18: S007 hit day_done at 11:16
        Kyiv (08:16 UTC) and correctly went quiet for the rest of the day
        (webapp/runner.py's own settled short-circuit) -- last_cycle_at
        froze there while every later cycle correctly opened no broker
        session and updated nothing. Before this fix the watchdog read that
        as a stall and alerted at 08:20 UTC; it must not."""
        last_cycle_at = datetime(2026, 9, 18, 8, 16, 9)
        status = "settled:2026-09-18"
        state: dict = {}
        t = last_cycle_at
        end_of_window = datetime(2026, 9, 18, 13, 59)
        while t <= end_of_window:
            state, alert = evaluate(t, last_cycle_at, status, state)
            assert alert is None, f"unexpected alert at {t}: {alert}"
            t += timedelta(minutes=2)

    def test_settled_yesterday_does_not_suppress_todays_real_stall(self):
        """A stale settled:<yesterday> marker (the account simply hasn't run
        a fresh cycle yet today) must NOT be mistaken for "settled today" --
        that would silently blind the watchdog on exactly the days it's
        most needed (the very first stall of a new trading day)."""
        now = UTC_SESSION_MID  # 2026-09-17
        stale_last = now - timedelta(minutes=STALE_THRESHOLD_MINUTES + 1)
        state, alert = evaluate(now, stale_last, "settled:2026-09-16", {})
        assert alert is not None
        assert "ALERT" in alert

    def test_real_2026_09_16_outage_replay(self):
        """Replays the actual live outage from this project's own history
        (S007-acct47939312.log: last real cycle 2026-09-16 12:41:13 Kyiv,
        next one only at 21:42 Kyiv -- a Podman-machine hang) through the
        pure logic, on a minute-by-minute grid across that day's remaining
        trading window. Confirms exactly one ALERT (a few minutes in, not
        immediately and not repeated) and no spam across the following
        hours -- the property that actually matters operationally."""
        last_good_kyiv_naive = datetime(2026, 9, 16, 12, 41, 13)
        last_good_utc = last_good_kyiv_naive - timedelta(hours=3)  # EEST offset
        end_of_window_utc = last_good_utc + timedelta(hours=4)  # well past Kyiv 16:59

        state: dict = {}
        t = last_good_utc
        alerts = []
        while t <= end_of_window_utc:
            state, alert = evaluate(t, last_good_utc, None, state)
            if alert:
                alerts.append((t, alert))
            t += timedelta(minutes=1)

        assert len(alerts) == 1
        first_alert_age_min = (alerts[0][0] - last_good_utc).total_seconds() / 60
        assert STALE_THRESHOLD_MINUTES < first_alert_age_min <= STALE_THRESHOLD_MINUTES + 1
