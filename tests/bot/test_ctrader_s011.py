"""Regression test for the live incident found 2026-08-19/2026-09-09: a
still-forming current-UTC-day D1 bar leaking into `last_price` (used to size
and price real market orders in `CTraderS011.run_live_cycle_multi`) produced
a ~5.7x oversized CAC40 order -- the RSI signal path already dropped this
same forming bar (bot/s011_paper.py::decide), but the order-sizing price did
not, so the mispricing was invisible in the logs.

No network/broker session needed: `_drop_forming_bar`/`_last_closed_price`
are pure functions over a DataFrame.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from bot.ctrader_s011 import CTraderS011


def _bars(closes: list[float], last_is_today: bool) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    n = len(closes)
    # oldest..newest, ending either yesterday (closed) or today (forming)
    last_ts = now if last_is_today else now - timedelta(days=1)
    idx = pd.to_datetime([last_ts - timedelta(days=i) for i in range(n - 1, -1, -1)])
    return pd.DataFrame({"close": closes}, index=idx)


def test_drop_forming_bar_removes_only_todays_row():
    df = _bars([100.0, 200.0, 999999.0], last_is_today=True)
    out = CTraderS011._drop_forming_bar(df)
    assert list(out["close"]) == [100.0, 200.0]


def test_drop_forming_bar_is_a_noop_when_last_bar_already_closed():
    df = _bars([100.0, 200.0], last_is_today=False)
    out = CTraderS011._drop_forming_bar(df)
    assert list(out["close"]) == [100.0, 200.0]


def test_drop_forming_bar_handles_empty_df():
    out = CTraderS011._drop_forming_bar(pd.DataFrame())
    assert out.empty


def test_last_closed_price_skips_a_forming_todays_bar():
    """The live bug: without this guard, a still-forming bar's (possibly
    anomalous) close would price the order instead of the last real close."""
    df = _bars([100.0, 200.0, 999999.0], last_is_today=True)
    assert CTraderS011._last_closed_price(df) == 200.0


def test_last_closed_price_uses_last_close_when_nothing_is_forming():
    df = _bars([100.0, 200.0], last_is_today=False)
    assert CTraderS011._last_closed_price(df) == 200.0


def test_last_closed_price_returns_none_for_empty_or_all_forming():
    assert CTraderS011._last_closed_price(pd.DataFrame()) is None
    # a single bar for "today" and nothing else -- all forming, nothing closed
    df = _bars([999999.0], last_is_today=True)
    assert CTraderS011._last_closed_price(df) is None
