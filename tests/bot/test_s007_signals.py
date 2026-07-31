"""plan_now's `filtered` field (added 2026-07-23, see decisions-log.md and
scripts/s007_loop.py): must be True ONLY when the Frankfurt-range height
filter has DEFINITIVELY ruled out today (a verdict that cannot change later
in the same session, since the 09:00-09:59 range is already closed by the
time it's computed) -- never for the "not enough bars yet" case, which is
still pending and must keep being polled.
"""
from __future__ import annotations

import pandas as pd

from bot.s007_signals import plan_now


def _frankfurt_bars(date: str, n: int, low: float, high: float) -> pd.DataFrame:
    """n one-minute bars starting 09:00 on `date`, oscillating between
    `low` and `high` so the resulting Frankfurt range height is `high - low`."""
    idx = pd.date_range(f"{date} 09:00", periods=n, freq="1min")
    mid = (low + high) / 2
    rows = []
    for i in range(n):
        px = low if i % 2 == 0 else high
        rows.append(dict(open=mid, high=high if i % 2 else mid,
                          low=low if i % 2 == 0 else mid, close=px))
    return pd.DataFrame(rows, index=idx)


def test_wide_frankfurt_range_sets_filtered_true():
    # WORKING_S007 caps max_height at 100pt; 200pt range must trip it.
    bars = _frankfurt_bars("2026-01-05", n=60, low=25000.0, high=25200.0)
    res = plan_now(bars, now=pd.Timestamp("2026-01-05 10:05:00"), preset="WORKING_S007")
    assert res["filtered"] is True
    assert res["direction"] is None
    assert res["context"] is None
    assert res["in_window"] is True   # still a real trading-hours cycle, just a no-trade day


def test_narrow_frankfurt_range_does_not_set_filtered():
    bars = _frankfurt_bars("2026-01-05", n=60, low=25000.0, high=25010.0)  # 10pt, well under cap
    res = plan_now(bars, now=pd.Timestamp("2026-01-05 10:05:00"), preset="WORKING_S007")
    assert res.get("filtered", False) is False


def test_too_few_frankfurt_bars_does_not_set_filtered():
    # Only 10 of the required 45 min_fr_bars -- "not enough data yet", which
    # CAN resolve later (more bars may still arrive), unlike the height case.
    bars = _frankfurt_bars("2026-01-05", n=10, low=25000.0, high=25200.0)
    res = plan_now(bars, now=pd.Timestamp("2026-01-05 09:15:00"), preset="WORKING_S007")
    assert res.get("filtered", False) is False
    assert res["context"] is None
