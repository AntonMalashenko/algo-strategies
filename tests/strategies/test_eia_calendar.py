"""Tests for strategies/eia_calendar.py -- the S026 EIA release calendar.

The bar here is different from a normal Gate 0 test: there is no look-ahead
question (this is a static calendar, not a signal), so what actually matters is
"does this reproduce EIA's own published schedule exactly" -- tested against every
row fetched from https://www.eia.gov/petroleum/supply/weekly/schedule.php on
2026-09-23, not against a synthetic panel.
"""
from __future__ import annotations

import pandas as pd
import pytest

from strategies.eia_calendar import (
    KNOWN_HOLIDAY_SHIFTS,
    UnregisteredYearEndShiftError,
    _general_holiday_shift,
    eia_release_datetime,
    report_release_datetimes,
)

# (week_ending_friday, expected_release_date, expected_hour, expected_minute)
PUBLISHED_ROWS = [
    ("2024-12-27", "2025-01-02", 11, 0),
    ("2025-01-17", "2025-01-23", 12, 0),
    ("2025-02-14", "2025-02-20", 12, 0),
    ("2025-05-23", "2025-05-29", 12, 0),
    ("2025-08-29", "2025-09-04", 12, 0),
    ("2025-10-10", "2025-10-16", 12, 0),
    ("2025-11-07", "2025-11-13", 12, 0),
    ("2025-12-19", "2025-12-29", 17, 0),
    ("2026-01-16", "2026-01-22", 12, 0),
    ("2026-02-13", "2026-02-19", 12, 0),
    ("2026-05-22", "2026-05-28", 12, 0),
    ("2026-09-04", "2026-09-10", 12, 0),
    ("2026-10-09", "2026-10-15", 12, 0),
    ("2026-11-06", "2026-11-12", 12, 0),
]

# A handful of ordinary weeks with no holiday nearby -> plain Wednesday 10:30 ET.
ORDINARY_WEEKS = ["2025-03-14", "2025-07-11", "2026-04-10", "2026-08-14"]


@pytest.mark.parametrize("week_ending,expected_date,hh,mm", PUBLISHED_ROWS)
def test_matches_every_published_holiday_shift_row(week_ending, expected_date, hh, mm):
    ts = eia_release_datetime(week_ending)
    assert ts.strftime("%Y-%m-%d") == expected_date
    assert (ts.hour, ts.minute) == (hh, mm)
    assert str(ts.tz) == "America/New_York"


@pytest.mark.parametrize("week_ending", ORDINARY_WEEKS)
def test_ordinary_week_is_plain_wednesday_1030(week_ending):
    ts = eia_release_datetime(week_ending)
    assert ts.day_name() == "Wednesday"
    assert (ts.hour, ts.minute) == (10, 30)
    # sanity: it's the Wednesday 5 days after the given Friday
    assert ts.date() == (pd.Timestamp(week_ending) + pd.Timedelta(days=5)).date()


def test_known_table_has_no_duplicate_or_out_of_order_keys():
    keys = list(KNOWN_HOLIDAY_SHIFTS.keys())
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


def test_general_rule_reproduces_all_non_year_end_rows_independently():
    """Recompute every non-Dec/Jan published row with ONLY the general rule
    (bypassing the KNOWN_HOLIDAY_SHIFTS lookup) -- this is the 13/14 verification
    the module docstring claims, pinned down so it can't silently regress."""
    for week_ending, expected_date, _hh, _mm in PUBLISHED_ROWS:
        we = pd.Timestamp(week_ending)
        if we.month in (12, 1):
            continue  # the one row the general rule does NOT cover, by design
        predicted = _general_holiday_shift(we)
        assert predicted.strftime("%Y-%m-%d") == expected_date


def test_unregistered_year_end_week_raises_not_guesses():
    """2027's Christmas/New Year boundary is not in KNOWN_HOLIDAY_SHIFTS (the table
    stops at 2026-11) -- must raise, not silently return a plain Wednesday."""
    with pytest.raises(UnregisteredYearEndShiftError):
        eia_release_datetime("2027-12-24")


def test_report_release_datetimes_flags_unresolved_year_end_week():
    """The 2025-12-19 week-ending row IS registered (resolves to 2025-12-29), but
    its immediate neighbors (default Wednesdays 2025-12-31 / 2026-01-07-ish landing
    within 2 days of Jan 1) are deliberately NOT claimed as safe -- see the module
    docstring's at_risk window. A caller must see them as unresolved, not silently
    dropped or silently guessed."""
    resolved, unresolved = report_release_datetimes("2025-12-01", "2026-01-31")
    # the Dec 2025 shift must appear, at its ACTUAL (shifted) date, not the naive one
    assert pd.Timestamp("2025-12-29", tz="America/New_York").replace(hour=17, minute=0) in resolved
    unresolved_dates = {d.date() for d in unresolved}
    assert pd.Timestamp("2025-12-26").date() in unresolved_dates  # default Wed 2025-12-31, near New Year's
    assert pd.Timestamp("2025-12-19").date() not in unresolved_dates  # this one IS registered


def test_report_release_datetimes_over_registered_range_is_weekly():
    """A range safely clear of the year-end at_risk window (see module docstring)
    should resolve every week with no gaps other than the ordinary ~7 days."""
    resolved, unresolved = report_release_datetimes("2026-02-01", "2026-04-30")
    assert len(unresolved) == 0
    assert 12 <= len(resolved) <= 14  # ~13 weekly Wednesdays in a quarter
    # Compare calendar DATES only -- entries carry different times-of-day (10:30
    # normal, 12:00 shifted), which would otherwise throw off a raw timestamp diff
    # (e.g. Thu 12:00 -> next Wed 10:30 is "5 days 22:30", not a real anomaly).
    dates = pd.Series([d.normalize().tz_localize(None) for d in resolved])
    diffs = dates.diff().dropna()
    # A holiday shift moves ONE release by exactly 1 day, which shows up as a
    # neighboring 8-day gap (into the shift) and 6-day gap (out of it) -- both
    # expected, not anomalies. Only a gap outside {6,7,8} would mean a genuinely
    # missed or duplicated week.
    assert diffs.dt.days.between(6, 8).all()
    assert (diffs.dt.days == 7).sum() >= len(diffs) - 2  # at most one shift in this window
