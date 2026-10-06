"""Measure which timezone the S004 M15 files are stamped in (ALGODEV-62 phase B).

    python -m mt5.tools.s004_clock_probe

Why this exists: the EA has to read the Asia window (00:00-06:59) and the 22:45
cutoff off the broker's clock exactly the way the backtest reads them off the
data's clock. ALGODEV-61 lost an hour per summer month by ASSUMING a fixed
offset, so strategies/s004_config.py::SESSION_TZ is a measured fact and this
script is the measurement. It exits 1 if the data stops agreeing with it.

Method -- a DST transition is a natural experiment. Take the activity-weighted
centroid of the European morning (data-clock hours 5-12, weighted by M15 bar
range) over the 14 days before and the 14 days after a transition:

  * EU transition: Europe moves one hour earlier in UTC. A clock that follows
    EU DST moves with it, so the centroid does NOT move; a fixed or US-DST
    clock would show the European morning an hour earlier (-1h).
  * US transition: nothing about the European morning changes in UTC. A clock
    that follows US DST would jump, showing it an hour later (+1h); an EU-DST
    or fixed clock does NOT move.

Both near zero => EET/EEST on European dates, which is what SESSION_TZ says.
The |shift| threshold is generous (0.5h) because the centroid also drifts with
news flow and seasonality; the signal being detected is a full hour.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta

import numpy as np

from backtest.s004_metalabel_data import load_combined
from mt5.tools.clock import SUNDAY, _last_weekday, _nth_weekday
from strategies.s004_config import SESSION_TZ

PROBE_PAIRS = ("EURUSD", "USDJPY", "GBPJPY")
EURO_MORNING = (5.0, 12.0)      # data-clock hours the centroid is taken over
WINDOW_DAYS = 14                # on each side of a transition
MIN_BARS = 150                  # ~10 trading days x 28 morning M15 bars; less = thinned by gaps
MAX_SHIFT_HOURS = 0.5           # a real DST mismatch is a full hour
FIRST_YEAR, LAST_YEAR = 2013, 2026
MINUTES_PER_HOUR = 60


def _morning_centroid(frame, lo: datetime, hi: datetime) -> float:
    """Range-weighted mean time-of-day of the European morning, in data hours."""
    index = frame.index
    hours = (index.hour + index.minute / MINUTES_PER_HOUR).to_numpy()
    weight = (frame["high"] - frame["low"]).abs().to_numpy()
    start, stop = EURO_MORNING
    mask = (index >= lo) & (index < hi) & (hours >= start) & (hours < stop)
    if mask.sum() < MIN_BARS:
        return float("nan")
    return float((weight[mask] * hours[mask]).sum() / weight[mask].sum())


def _shift_across(frame, day: datetime) -> float:
    before = _morning_centroid(frame, day - timedelta(days=WINDOW_DAYS), day)
    after = _morning_centroid(frame, day, day + timedelta(days=WINDOW_DAYS))
    return after - before


def transition_shifts(frame) -> dict[str, list[float]]:
    """Centroid shift across every EU and US spring-forward in the data."""
    shifts: dict[str, list[float]] = {"EU": [], "US": []}
    for year in range(FIRST_YEAR, LAST_YEAR):
        days = {"EU": datetime(year, 3, _last_weekday(year, 3, SUNDAY)),
                "US": datetime(year, 3, _nth_weekday(year, 3, SUNDAY, 2))}
        for tag, day in days.items():
            shift = _shift_across(frame, day)
            if shift == shift:          # not NaN
                shifts[tag].append(shift)
    return shifts


def run() -> int:
    print(f"S004 session clock probe -- strategies/s004_config.py::SESSION_TZ = {SESSION_TZ}")
    print(f"{'pair':>8} {'EU shift':>10} {'US shift':>10} {'transitions':>12}  verdict")
    failed = []
    for pair in PROBE_PAIRS:
        shifts = transition_shifts(load_combined(pair))
        means = {tag: float(np.mean(values)) for tag, values in shifts.items()}
        ok = all(abs(mean) <= MAX_SHIFT_HOURS for mean in means.values())
        count = len(shifts["EU"]) + len(shifts["US"])
        print(f"{pair:>8} {means['EU']:+10.3f} {means['US']:+10.3f} {count:>12}  "
              f"{'EET/EEST on EU dates' if ok else 'MISMATCH'}")
        if not ok:
            failed.append(pair)
    if failed:
        print(f"\nFAIL: {failed} no longer match {SESSION_TZ}. A +1h US shift means the clock "
              "follows US DST (TZ_EET_US_DST); a -1h EU shift means a fixed offset. Fix "
              "SESSION_TZ and the generated EA params before trading this.")
        return 1
    print("\nPASS: the European morning holds its clock hour across both transitions, so the "
          "data clock moves with Europe and not with the US.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
