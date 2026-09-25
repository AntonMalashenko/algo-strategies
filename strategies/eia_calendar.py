"""S026 -- EIA Weekly Petroleum Status Report release calendar.

Idea (see Confluence "Strategies" -> P12 / S025+S026+S027, page id 24838145): S026's
whole mechanic is a pre-event range breakout around the EIA Petroleum Status
Report's exact release moment. The spec itself flags calendar precision as the
single biggest non-statistical risk ("ошибка на день здесь буквально меняет,
торгуем мы реальное событие или случайный день") -- this module exists purely to
get that one fact right, with nothing derived by guesswork where a guess could be
wrong.

Ground truth, fetched 2026-09-23 from EIA's own published schedule page
(https://www.eia.gov/petroleum/supply/weekly/schedule.php):
  "The wpsrsummary.pdf, overview.pdf, and Tables 1-14 ... are released ... after
  10:30 a.m. eastern time on Wednesday." That is the DEFAULT. The same page
  separately maintains a "Holiday Release Schedule" table of every week where this
  default does not hold -- reproduced verbatim below as KNOWN_HOLIDAY_SHIFTS, dated
  2024-12 through 2026-11 (whatever EIA had published as of the fetch date). This
  table is NOT re-derivable from a generic holiday rule for every row (see below) --
  treat it as ground truth, not as an algorithm's output, and refresh it from the
  live page before trusting any date beyond what's listed here.

What IS a reliable general rule (verified against all 14 known rows, 13/14 match
exactly -- see decisions/change-log for the check): if the US federal holiday
calendar puts a holiday on the Monday, Tuesday, OR Wednesday of the report's normal
Wednesday-release week, EIA delays that release to Thursday (same report, one
business day late). This holds whether the holiday falls on the Monday (MLK,
Presidents, Memorial, Labor, Columbus) or lands mid-week (Veterans Day, which moves
around the calendar and hit a Tuesday in 2025, a Wednesday in 2026). Implemented as
``_general_holiday_shift`` using the ``holidays`` package's US federal calendar, and
used ONLY as a fallback for weeks not already covered by KNOWN_HOLIDAY_SHIFTS.

The one case the general rule does NOT reproduce: the year-end Christmas/New Year
week, where EIA skips straight to the following Monday (10 calendar days out, not
1 business day) rather than just bumping to Thursday -- confirmed by the one
verified instance in the table (week ending 2025-12-19 -> released 2025-12-29, not
2025-12-24/25). This is a genuinely irregular, non-rule-derivable production gap
(the report covering the missed week's data is folded into the next one), so every
future year's December/January transition MUST be added to KNOWN_HOLIDAY_SHIFTS by
hand from EIA's page before this module is trusted for that week -- the code
deliberately raises rather than silently guessing when it hits an un-registered
December/January boundary (see ``eia_release_datetime``).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import holidays
import pandas as pd

ET = ZoneInfo("America/New_York")

DEFAULT_RELEASE_WEEKDAY = 2   # Wednesday (Monday=0)
DEFAULT_RELEASE_TIME_ET = (10, 30)   # 10:30 a.m. ET, per EIA's own schedule page

# Verbatim from https://www.eia.gov/petroleum/supply/weekly/schedule.php, fetched
# 2026-09-23. Key = the week-ending Friday of the data the report covers (this is
# how EIA's own table indexes it); value = (actual release date, (hour, minute) ET).
# EXTEND THIS TABLE FROM THE LIVE PAGE -- do not extrapolate past its last entry.
KNOWN_HOLIDAY_SHIFTS: dict[str, tuple[str, tuple[int, int]]] = {
    "2024-12-27": ("2025-01-02", (11, 0)),    # New Year's Day
    "2025-01-17": ("2025-01-23", (12, 0)),    # MLK Day / Inauguration
    "2025-02-14": ("2025-02-20", (12, 0)),    # Presidents' Day
    "2025-05-23": ("2025-05-29", (12, 0)),    # Memorial Day
    "2025-08-29": ("2025-09-04", (12, 0)),    # Labor Day
    "2025-10-10": ("2025-10-16", (12, 0)),    # Columbus Day
    "2025-11-07": ("2025-11-13", (12, 0)),    # Veterans Day
    "2025-12-19": ("2025-12-29", (17, 0)),    # Christmas/New Year -- the irregular one
    "2026-01-16": ("2026-01-22", (12, 0)),    # MLK Day
    "2026-02-13": ("2026-02-19", (12, 0)),    # Presidents' Day
    "2026-05-22": ("2026-05-28", (12, 0)),    # Memorial Day
    "2026-09-04": ("2026-09-10", (12, 0)),    # Labor Day
    "2026-10-09": ("2026-10-15", (12, 0)),    # Columbus Day
    "2026-11-06": ("2026-11-12", (12, 0)),    # Veterans Day
}

_US_HOLIDAYS = holidays.US(years=range(2015, 2031))


class UnregisteredYearEndShiftError(RuntimeError):
    """Raised when eia_release_datetime hits a December/January week-ending Friday
    that is not in KNOWN_HOLIDAY_SHIFTS -- the year-end shift is not rule-derivable
    (see module docstring), so guessing here would silently produce a wrong date
    for exactly the report the strategy most needs to get right."""


def _week_ending_friday(any_date: pd.Timestamp) -> pd.Timestamp:
    """The Friday of the ISO week containing ``any_date`` (Mon=0 .. Fri=4)."""
    monday = any_date - pd.Timedelta(days=any_date.weekday())
    return monday + pd.Timedelta(days=4)


def _default_wednesday(week_ending_friday: pd.Timestamp) -> pd.Timestamp:
    return week_ending_friday + pd.Timedelta(days=5)


def _general_holiday_shift(week_ending_friday: pd.Timestamp) -> pd.Timestamp:
    """Wed -> Thu if a US federal holiday falls on the Mon/Tue/Wed of the release
    week; else the plain Wednesday. Verified 13/14 against KNOWN_HOLIDAY_SHIFTS --
    the sole miss is the year-end case this function is never asked to handle
    (callers check KNOWN_HOLIDAY_SHIFTS and the December/January guard first)."""
    wednesday = _default_wednesday(week_ending_friday)
    monday = wednesday - pd.Timedelta(days=2)
    span = [monday + pd.Timedelta(days=i) for i in range(3)]
    if any(d.date() in _US_HOLIDAYS for d in span):
        return wednesday + pd.Timedelta(days=1)
    return wednesday


def eia_release_datetime(week_ending_friday: str | pd.Timestamp) -> pd.Timestamp:
    """The EIA Weekly Petroleum Status Report's actual release timestamp (US/Eastern,
    tz-aware) for the report covering the week ending on the given Friday.

    Looks up KNOWN_HOLIDAY_SHIFTS first (ground truth); falls back to the verified
    general Mon/Tue/Wed-holiday-shift rule; raises UnregisteredYearEndShiftError for
    an un-registered December/January boundary rather than guessing.
    """
    we = pd.Timestamp(week_ending_friday).normalize()
    key = we.strftime("%Y-%m-%d")

    if key in KNOWN_HOLIDAY_SHIFTS:
        date_str, (hh, mm) = KNOWN_HOLIDAY_SHIFTS[key]
        return pd.Timestamp(date_str, tz=ET).replace(hour=hh, minute=mm)

    default_wed = _default_wednesday(we)
    # Narrow, deliberately conservative window: only the default Wednesdays that
    # land within 2 days of Christmas or New Year's Day itself -- exactly the shape
    # of the one confirmed anomaly (default Wed 2025-12-24, one day before Christmas
    # Thu 2025-12-25). Weeks safely inside December or January but away from this
    # window (e.g. default Wed 2026-01-07) are left to the general rule, which is
    # verified to handle them correctly (see test_ordinary_week_is_plain_wednesday).
    at_risk = (
        (default_wed.month == 12 and default_wed.day >= 24)
        or (default_wed.month == 1 and default_wed.day <= 2)
    )
    if at_risk:
        raise UnregisteredYearEndShiftError(
            f"week ending {we.date()} has default Wednesday {default_wed.date()}, "
            f"within 2 days of Christmas or New Year's Day; EIA's actual behavior "
            f"here is not rule-derivable (see module docstring) and this exact "
            f"date is not in KNOWN_HOLIDAY_SHIFTS -- confirm from "
            f"https://www.eia.gov/petroleum/supply/weekly/schedule.php (it may "
            f"turn out to be an ordinary Wednesday, or a multi-day merge like "
            f"2025-12-29 -- don't assume either without checking) and add it."
        )

    shifted = _general_holiday_shift(we)
    hh, mm = DEFAULT_RELEASE_TIME_ET if shifted == default_wed else (12, 0)
    return pd.Timestamp(shifted.strftime("%Y-%m-%d"), tz=ET).replace(hour=hh, minute=mm)


def report_release_datetimes(start: str, end: str) -> tuple[list[pd.Timestamp], list[pd.Timestamp]]:
    """Every EIA Weekly Petroleum Status Report release timestamp whose covered
    week-ending Friday falls within [start, end] (inclusive), in order. This is the
    event calendar S026's pre-event-range/breakout logic would trade around --
    price data is a SEPARATE, currently unsolved problem (see backtest runner).

    Returns (resolved, unresolved_week_ending_fridays): unresolved is non-empty only
    when the range crosses a December/January boundary not yet in
    KNOWN_HOLIDAY_SHIFTS -- callers MUST check it rather than assume a complete
    calendar, since silently dropping an event week would understate n and misstate
    which trading day is "the event" around that boundary."""
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    first_friday = _week_ending_friday(start_ts)
    fridays = pd.date_range(first_friday, end_ts, freq="W-FRI")
    resolved, unresolved = [], []
    for friday in fridays:
        try:
            resolved.append(eia_release_datetime(friday))
        except UnregisteredYearEndShiftError:
            unresolved.append(friday)
    return resolved, unresolved
