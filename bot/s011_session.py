"""S011's D1 session calendar: which session-dated broker bar is CLOSED.

Pure, network-free, and deliberately free of any broker/Twisted import, so
both bot/s011_paper.py (signal + its cheap up-to-date pre-check) and
bot/ctrader_s011.py (order-sizing price) share ONE definition without the
paper module having to import the broker layer.

Convention (see bot/ctrader_s011.py::D1_SESSION_DATE_ROLL): each cTrader D1
bar opens at the broker's midnight (21:00 UTC summer / 22:00 UTC winter) and
is labelled with the UTC date its broker-day CLOSES on. So the bar labelled D
is finished once D's broker-day close has passed -- NOT once the UTC date has
moved past D.

The previous filter dropped every bar labelled with today's UTC date. Between
the broker close and UTC midnight that is the session that has JUST CLOSED,
so every weeknight 22:00-24:00 UTC the newest real bar was thrown away, the
stale-D1-feed guard fired ~8 times in a row (2026-09-15..09-29, every night)
and the day's decision slipped from ~22:00 to 00:00 UTC.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd

BROKER_DAY_CLOSE_UTC_HOUR = 22   # cTrader D1 closes at the broker's midnight:
                                 # 21:00 UTC summer / 22:00 UTC winter -- use the
                                 # later so we never treat a day's bar as available
                                 # before it has actually closed


def _utc_now(now: datetime | None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:            # tolerate a naive datetime (older callers / tests)
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def last_closed_session_date(now: datetime | None = None) -> date:
    """Newest session date whose D1 bar has certainly closed at `now`.

    Calendar-only: no weekend handling here, because crypto instruments do
    have weekend sessions -- the weekday-only expectation lives in
    bot/s011_paper.py::_expected_last_closed_trading_date."""
    return (_utc_now(now) - timedelta(hours=BROKER_DAY_CLOSE_UTC_HOUR)).date()


def drop_forming_bar(bars: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """`bars` (session-dated index) without any session that is still forming."""
    if bars.empty:
        return bars
    cutoff = last_closed_session_date(now)
    return bars[bars.index.date <= cutoff] if bars.index[-1].date() > cutoff else bars
