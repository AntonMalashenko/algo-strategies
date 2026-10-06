"""Python mirror of mt5/MQL5/Include/AlgoCore/Clock.mqh.

The MQL5 side cannot use a timezone database, so broker server time is
described by an explicit rule (ENUM_TZ_RULE). This module implements the
SAME rules with the SAME arithmetic, so the parity/fixture tools convert
server time exactly the way the EA does. tests/mt5/test_clock.py proves the
rules equal real zoneinfo zones (America/New_York + 7h, Europe/Athens,
Europe/Prague) hour by hour.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

SECONDS_PER_HOUR = 3600
SUNDAY = 6                      # datetime.weekday()

# DST transition instants in UTC hours (rules valid from 2007 on) --
# keep equal to CLOCK_US_DST_* / CLOCK_EU_DST_UTC_HOUR in Clock.mqh.
US_DST_START_UTC_HOUR = 7       # 2nd Sunday of March, 02:00 EST
US_DST_END_UTC_HOUR = 6         # 1st Sunday of November, 02:00 EDT
EU_DST_UTC_HOUR = 1             # last Sunday of March / October, 01:00 UTC

RULE_UTC = "UTC"
RULE_FIXED = "FIXED"
RULE_EET_US_DST = "EET_US_DST"  # UTC+2, UTC+3 while US DST (typical MT5 "NY close" server)
RULE_EET_EU_DST = "EET_EU_DST"  # UTC+2, UTC+3 while EU DST (Europe/Athens-like)
RULE_CET_EU_DST = "CET_EU_DST"  # UTC+1, UTC+2 while EU DST (Europe/Prague-like)
RULE_EST_US_DST = "EST_US_DST"  # UTC-5, UTC-4 while US DST (America/New_York)
RULES = (RULE_UTC, RULE_FIXED, RULE_EET_US_DST, RULE_EET_EU_DST, RULE_CET_EU_DST,
         RULE_EST_US_DST)

STANDARD_OFFSET_HOURS = {
    RULE_UTC: 0,
    RULE_EET_US_DST: 2,
    RULE_EET_EU_DST: 2,
    RULE_CET_EU_DST: 1,
    RULE_EST_US_DST: -5,
}


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> int:
    first = datetime(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return 1 + delta + 7 * (nth - 1)


def _last_weekday(year: int, month: int, weekday: int) -> int:
    next_month = datetime(year + (month == 12), month % 12 + 1, 1)
    last = next_month - timedelta(days=1)
    delta = (last.weekday() - weekday) % 7
    return last.day - delta


def is_us_dst(utc: datetime) -> bool:
    year = utc.year
    start = datetime(year, 3, _nth_weekday(year, 3, SUNDAY, 2), US_DST_START_UTC_HOUR)
    stop = datetime(year, 11, _nth_weekday(year, 11, SUNDAY, 1), US_DST_END_UTC_HOUR)
    return start <= utc < stop


def is_eu_dst(utc: datetime) -> bool:
    year = utc.year
    start = datetime(year, 3, _last_weekday(year, 3, SUNDAY), EU_DST_UTC_HOUR)
    stop = datetime(year, 10, _last_weekday(year, 10, SUNDAY), EU_DST_UTC_HOUR)
    return start <= utc < stop


def standard_offset_seconds(rule: str, fixed_hours: int = 0) -> int:
    if rule == RULE_FIXED:
        return fixed_hours * SECONDS_PER_HOUR
    if rule not in STANDARD_OFFSET_HOURS:
        raise ValueError(f"unknown tz rule {rule!r}; valid: {RULES}")
    return STANDARD_OFFSET_HOURS[rule] * SECONDS_PER_HOUR


def offset_seconds_at_utc(rule: str, utc: datetime, fixed_hours: int = 0) -> int:
    """Offset (local - UTC) in seconds that `rule` has at the naive-UTC instant."""
    standard = standard_offset_seconds(rule, fixed_hours)
    if rule in (RULE_EET_US_DST, RULE_EST_US_DST):
        return standard + (SECONDS_PER_HOUR if is_us_dst(utc) else 0)
    if rule in (RULE_EET_EU_DST, RULE_CET_EU_DST):
        return standard + (SECONDS_PER_HOUR if is_eu_dst(utc) else 0)
    return standard


def local_to_utc(rule: str, local: datetime, fixed_hours: int = 0) -> datetime:
    """Same two-step resolution as ClockLocalToUtc (unambiguous off-weekend)."""
    guess = offset_seconds_at_utc(
        rule, local - timedelta(seconds=standard_offset_seconds(rule, fixed_hours)), fixed_hours)
    utc = local - timedelta(seconds=guess)
    check = offset_seconds_at_utc(rule, utc, fixed_hours)
    if check != guess:
        utc = local - timedelta(seconds=check)
    return utc


def utc_to_local(rule: str, utc: datetime, fixed_hours: int = 0) -> datetime:
    return utc + timedelta(seconds=offset_seconds_at_utc(rule, utc, fixed_hours))


def index_local_to_utc(index: pd.DatetimeIndex, rule: str, fixed_hours: int = 0) -> pd.DatetimeIndex:
    """Vectorised-enough conversion of a naive server-time index to naive UTC.

    The offset only changes at DST transitions, so it is resolved once per
    distinct local calendar day (weekend transitions never split a trading
    day) and broadcast -- fast on millions of M1 bars.
    """
    days = index.normalize()
    offsets: dict = {}
    for day in days.unique():
        # noon of the local day is never inside a DST gap/overlap
        noon_local = day.to_pydatetime() + timedelta(hours=12)
        utc = local_to_utc(rule, noon_local, fixed_hours)
        offsets[day] = (noon_local - utc).total_seconds()
    seconds = days.map(offsets).to_numpy()
    return index - pd.to_timedelta(seconds, unit="s")


def index_utc_to_local(index: pd.DatetimeIndex, rule: str, fixed_hours: int = 0) -> pd.DatetimeIndex:
    days = index.normalize()
    offsets: dict = {}
    for day in days.unique():
        noon_utc = day.to_pydatetime() + timedelta(hours=12)
        offsets[day] = offset_seconds_at_utc(rule, noon_utc, fixed_hours)
    seconds = days.map(offsets).to_numpy()
    return index + pd.to_timedelta(seconds, unit="s")
