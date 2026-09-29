"""Regression test for the live incident found 2026-08-19/2026-09-09: a
still-forming current-UTC-day D1 bar leaking into `last_price` (used to size
and price real market orders in `CTraderS011.run_live_cycle_multi`) produced
a ~5.7x oversized CAC40 order -- the RSI signal path already dropped this
same forming bar (bot/s011_paper.py::decide), but the order-sizing price did
not, so the mispricing was invisible in the logs.

Plus (2026-09-09) `_session_dated_index`: this broker stamps every D1
trendbar at the broker-day OPEN (21:00 UTC summer / 22:00 UTC winter), so
the raw timestamp names the day BEFORE the session the bar covers. The
adapter relabels each bar to its session date; these tests pin that
relabelling (including the GMT-broker and DST edge cases) and confirm the
forming-bar filters still behave on the relabelled index.

No network/broker session needed: `_session_dated_index`/
`_drop_forming_bar`/`_last_closed_price` are pure functions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.ctrader_s011 import CTraderS011


# A fixed weekday afternoon: the broker day (closes 21:00/22:00 UTC) is still
# running, so the session labelled with today's date is the forming one.
# Pinned rather than read from the clock -- the old helper used the real UTC
# date and these tests would have failed if run between 22:00 and 24:00 UTC.
NOW = datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)


def _bars(closes: list[float], last_is_today: bool, now: datetime = NOW) -> pd.DataFrame:
    """Session-dated daily bars -- the shape `_get_daily_step` now returns
    after `_session_dated_index`: tz-naive, normalised to UTC midnight, one
    row per session, oldest..newest. The last row is either today's
    still-forming session or yesterday's closed one."""
    today_utc = pd.Timestamp(now.date())
    n = len(closes)
    last_session = today_utc if last_is_today else today_utc - timedelta(days=1)
    idx = pd.DatetimeIndex([last_session - timedelta(days=i) for i in range(n - 1, -1, -1)])
    return pd.DataFrame({"close": closes}, index=idx)


class TestSessionDatedIndex:
    """cTrader stamps D1 bars at the broker-day OPEN, so the raw timestamp is
    a day (or, over a weekend, ~2 calendar days) behind the session the bar
    covers. Confirmed live 2026-09-09 against IC Markets: every D1 bar came
    back stamped 21:00Z while H1 was fully current."""

    def test_summer_evening_open_rolls_to_the_next_days_session(self):
        """21:00Z open (broker midnight, EEST) -> the session that CLOSES the
        following day. This is the live CAC40 case: a bar stamped
        2026-09-07 21:00Z is the 2026-09-08 trading session."""
        ix = CTraderS011._session_dated_index(
            pd.to_datetime(["2026-09-07 21:00", "2026-09-08 21:00"], utc=True))
        assert list(ix) == [pd.Timestamp("2026-09-08"), pd.Timestamp("2026-09-09")]
        assert ix.tz is None

    def test_winter_evening_open_rolls_to_the_same_session_as_summer(self):
        """22:00Z (EET, winter) must land on the SAME session date 21:00Z
        (EEST, summer) does -- the DST shift must not move the label."""
        ix = CTraderS011._session_dated_index(pd.to_datetime(["2026-09-07 22:00"], utc=True))
        assert list(ix) == [pd.Timestamp("2026-09-08")]

    def test_midnight_open_keeps_its_own_date(self):
        """A GMT-based broker opening its day exactly at 00:00 UTC is already
        session-dated -- ceil() must leave it alone, not push it a day out."""
        ix = CTraderS011._session_dated_index(pd.to_datetime(["2026-09-07 00:00"], utc=True))
        assert list(ix) == [pd.Timestamp("2026-09-07")]

    def test_empty_input_gives_an_empty_index(self):
        ix = CTraderS011._session_dated_index(pd.to_datetime([], utc=True))
        assert len(ix) == 0
        assert ix.tz is None

    def test_accepts_a_series_of_unix_seconds_like_get_daily_step_builds(self):
        """`_get_daily_step` passes a pd.Series (df.pop("ts") converted), not
        an Index -- both shapes must work."""
        opens = pd.to_datetime(pd.Series([1788814800, 1788901200]), unit="s", utc=True)
        assert list(opens) == [pd.Timestamp("2026-09-07 21:00", tz="UTC"),
                               pd.Timestamp("2026-09-08 21:00", tz="UTC")]
        ix = CTraderS011._session_dated_index(opens)
        assert list(ix) == [pd.Timestamp("2026-09-08"), pd.Timestamp("2026-09-09")]

    def test_weekend_gap_is_preserved_not_collapsed(self):
        """Fri/Sun/Mon opens (the broker's own week shape) relabel to the
        Mon..Wed sessions -- i.e. relabelling shifts every bar by one, it
        does not merge or drop any."""
        ix = CTraderS011._session_dated_index(pd.to_datetime(
            ["2026-09-03 21:00", "2026-09-06 21:00", "2026-09-07 21:00"], utc=True))
        assert list(ix) == [pd.Timestamp("2026-09-04"), pd.Timestamp("2026-09-07"),
                            pd.Timestamp("2026-09-08")]


def test_drop_forming_bar_removes_only_todays_row():
    df = _bars([100.0, 200.0, 999999.0], last_is_today=True)
    out = CTraderS011._drop_forming_bar(df, NOW)
    assert list(out["close"]) == [100.0, 200.0]


def test_drop_forming_bar_is_a_noop_when_last_bar_already_closed():
    df = _bars([100.0, 200.0], last_is_today=False)
    out = CTraderS011._drop_forming_bar(df, NOW)
    assert list(out["close"]) == [100.0, 200.0]


def test_drop_forming_bar_handles_empty_df():
    out = CTraderS011._drop_forming_bar(pd.DataFrame(), NOW)
    assert out.empty


def test_last_closed_price_skips_a_forming_todays_bar():
    """The live bug: without this guard, a still-forming bar's (possibly
    anomalous) close would price the order instead of the last real close."""
    df = _bars([100.0, 200.0, 999999.0], last_is_today=True)
    assert CTraderS011._last_closed_price(df, NOW) == 200.0


def test_last_closed_price_uses_last_close_when_nothing_is_forming():
    df = _bars([100.0, 200.0], last_is_today=False)
    assert CTraderS011._last_closed_price(df, NOW) == 200.0


def test_last_closed_price_returns_none_for_empty_or_all_forming():
    assert CTraderS011._last_closed_price(pd.DataFrame(), NOW) is None
    # a single bar for "today" and nothing else -- all forming, nothing closed
    df = _bars([999999.0], last_is_today=True)
    assert CTraderS011._last_closed_price(df, NOW) is None


# --- 2026-09-29: the session that has JUST closed is not "forming" ---------
#
# Live every weeknight 2026-09-15..09-29: between the broker close (22:00 UTC)
# and UTC midnight the newest bar is labelled with today's UTC date but is
# already CLOSED; the old date-only filter dropped it, the stale-D1-feed guard
# fired ~8 times and the decision slipped to 00:00 UTC.

def test_session_that_just_closed_is_kept_after_the_broker_close():
    after_close = datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc)
    df = _bars([100.0, 200.0], last_is_today=True, now=after_close)   # last = 2026-09-24
    assert list(CTraderS011._drop_forming_bar(df, after_close)["close"]) == [100.0, 200.0]
    assert CTraderS011._last_closed_price(df, after_close) == 200.0


def test_next_session_opened_at_the_broker_midnight_is_still_dropped():
    """After the close the broker has already opened the next session,
    labelled tomorrow -- that one IS forming."""
    after_close = datetime(2026, 9, 24, 22, 30, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex(["2026-09-23", "2026-09-24", "2026-09-25"])
    df = pd.DataFrame({"close": [100.0, 200.0, 999999.0]}, index=idx)
    assert list(CTraderS011._drop_forming_bar(df, after_close)["close"]) == [100.0, 200.0]


def test_crypto_weekend_session_is_kept_once_closed():
    """Crypto prints weekend sessions; Saturday's closes at the Saturday broker
    midnight and must be usable on Sunday like any other day."""
    sunday = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex(["2026-09-25", "2026-09-26", "2026-09-27"])
    df = pd.DataFrame({"close": [1.0, 2.0, 999999.0]}, index=idx)
    assert list(CTraderS011._drop_forming_bar(df, sunday)["close"]) == [1.0, 2.0]
