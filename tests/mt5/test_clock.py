"""mt5/tools/clock.py (and therefore AlgoCore/Clock.mqh, which uses the same rules)
against real zoneinfo zones, hour by hour, 2019-2027."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from mt5.tools import clock
from mt5.tools.s021_fixtures import RULE_REFERENCE_ZONES, reference_offset_seconds

FIRST_HOUR = datetime(2019, 1, 1)
LAST_HOUR = datetime(2028, 1, 1)
WEEKEND = (5, 6)


def _hours():
    hour = FIRST_HOUR
    while hour < LAST_HOUR:
        yield hour
        hour += timedelta(hours=1)


@pytest.mark.parametrize("rule", sorted(RULE_REFERENCE_ZONES))
def test_rule_offsets_equal_zoneinfo_every_hour(rule):
    mismatches = [hour for hour in _hours()
                  if clock.offset_seconds_at_utc(rule, hour) != reference_offset_seconds(rule, hour)]
    assert mismatches == []


@pytest.mark.parametrize("rule", sorted(RULE_REFERENCE_ZONES))
def test_local_to_utc_round_trips_on_trading_days(rule):
    # the DST hour itself is ambiguous/non-existent, but always on a weekend
    for hour in _hours():
        local = clock.utc_to_local(rule, hour)
        if local.weekday() in WEEKEND:
            continue
        assert clock.local_to_utc(rule, local) == hour, (rule, hour)


def test_fixed_rule_uses_the_given_hours():
    instant = datetime(2025, 7, 1, 12)
    assert clock.offset_seconds_at_utc(clock.RULE_FIXED, instant, fixed_hours=3) == 3 * 3600
    assert clock.local_to_utc(clock.RULE_FIXED, instant, fixed_hours=-5) == instant + timedelta(hours=5)


def test_unknown_rule_is_rejected():
    with pytest.raises(ValueError):
        clock.offset_seconds_at_utc("NOT_A_RULE", datetime(2025, 1, 1))


@pytest.mark.parametrize("rule", [clock.RULE_EET_US_DST, clock.RULE_EET_EU_DST])
def test_index_conversion_matches_scalar(rule, fixed_est_m1):
    utc_index = fixed_est_m1.index + pd.Timedelta(hours=5)
    local = clock.index_utc_to_local(utc_index, rule)
    back = clock.index_local_to_utc(local, rule)
    assert (back == utc_index).all()
    sample = utc_index[:: max(1, len(utc_index) // 500)]
    expected = [clock.utc_to_local(rule, ts.to_pydatetime()) for ts in sample]
    assert list(clock.index_utc_to_local(sample, rule).to_pydatetime()) == expected
