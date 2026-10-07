"""Capital.com standalone tool: session selection and the New York window, on a stub feed."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from mt5.tools import s021_capital as cap
from strategies.orb_intraday.config import ORB_BASE

FULL_SESSION_BARS = 78          # 390 minutes / 5


class StubFeed:
    """Serves synthetic in-session bars; `short_days` get a holiday-sized session."""

    def __init__(self, short_days: set[date] = frozenset(), missing_days: set[date] = frozenset()):
        self.short_days = set(short_days)
        self.missing_days = set(missing_days)
        self.asked: list[date] = []

    def bars(self, epic, start, end, resolution):
        day = start.date()
        self.asked.append(day)
        if day in self.missing_days:
            return []
        if resolution == "MINUTE":
            return [{"time": start, "open": 20_000.0, "high": 20_010.0, "low": 19_990.0}]
        count = 10 if day in self.short_days else FULL_SESSION_BARS
        # range = day-of-month points, so each session is identifiable in the result
        width = float(day.day)
        return [{"time": start + timedelta(minutes=5 * i), "open": 20_000.0,
                 "high": 20_000.0 + width, "low": 20_000.0} for i in range(count)]


def test_session_window_is_390_minutes_and_follows_us_dst():
    summer_start, summer_end = cap.session_window_utc(date(2026, 7, 15))
    winter_start, winter_end = cap.session_window_utc(date(2026, 12, 15))
    assert (summer_end - summer_start) == timedelta(minutes=390)
    assert (winter_end - winter_start) == timedelta(minutes=390)
    assert summer_start == datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    assert winter_start == datetime(2026, 12, 15, 14, 30, tzinfo=timezone.utc)


def test_window_ends_one_minute_after_the_last_session_bar():
    """session_close is the last M1 bar's own minute, so the half-open end is 16:00."""
    _, end = cap.session_window_utc(date(2026, 7, 15))
    last_bar = datetime.combine(date(2026, 7, 15), ORB_BASE.session_close, tzinfo=cap.NY)
    assert end == (last_bar + timedelta(minutes=1)).astimezone(timezone.utc)


def test_session_ranges_skips_weekends_and_returns_oldest_first():
    feed = StubFeed()
    ranges = cap.session_ranges(feed, "US100", date(2026, 10, 7), ORB_BASE.adr_window)
    days = [d for d, _ in ranges]
    assert len(days) == ORB_BASE.adr_window
    assert days == sorted(days)
    assert all(d.weekday() < 5 for d in days)
    assert max(days) < date(2026, 10, 7)      # strictly causal: today is never in the window


def test_a_half_day_is_dropped_and_replaced_by_an_older_session():
    half_day = date(2026, 10, 6)
    ranges = cap.session_ranges(StubFeed(short_days={half_day}), "US100",
                                date(2026, 10, 7), ORB_BASE.adr_window)
    days = [d for d, _ in ranges]
    assert half_day not in days
    assert len(days) == ORB_BASE.adr_window


def test_range_is_session_high_minus_low():
    ranges = cap.session_ranges(StubFeed(), "US100", date(2026, 10, 7), 1)
    day, width = ranges[0]
    assert width == pytest.approx(float(day.day))


def test_open_price_reads_the_bid_open_of_the_0930_bar():
    assert cap.open_price(StubFeed(), "US100", date(2026, 10, 7)) == pytest.approx(20_000.0)


def test_open_price_gives_up_with_a_clear_message_on_a_closed_day(monkeypatch):
    monkeypatch.setattr(cap, "OPEN_BAR_ATTEMPTS", 2)
    monkeypatch.setattr(cap, "OPEN_BAR_RETRY_S", 0)
    with pytest.raises(cap.CapitalError, match="no 09:30 bar"):
        cap.open_price(StubFeed(missing_days={date(2026, 12, 25)}), "US100", date(2026, 12, 25))


def test_connect_names_the_missing_variables_without_touching_their_values(monkeypatch):
    for name in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cap, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(cap.CapitalError) as excinfo:
        cap.connect()
    assert "CAPITAL_API_KEY" in str(excinfo.value)
