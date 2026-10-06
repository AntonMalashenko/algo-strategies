"""Shared synthetic data for the mt5 tooling tests (no histdata dependency)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

SYNTHETIC_START = "2025-09-15"     # spans the EU (Oct 26) and US (Nov 2) DST ends
SYNTHETIC_WEEKDAYS = 45
SYNTHETIC_FIRST_MINUTE = "04:00"   # America/New_York clock, like histdata
SYNTHETIC_LAST_MINUTE = "19:59"
SYNTHETIC_START_PRICE = 20_000.0
SYNTHETIC_STEP_SIGMA = 6.0
SYNTHETIC_SEED = 20261003


def make_ny_m1(weekdays: int = SYNTHETIC_WEEKDAYS, seed: int = SYNTHETIC_SEED) -> pd.DataFrame:
    """Random-walk M1 bars on the NY exchange clock, histdata-shaped (open/high/low/close)."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(SYNTHETIC_START, periods=weekdays)
    stamps = []
    for day in days:
        stamps.append(pd.date_range(f"{day.date()} {SYNTHETIC_FIRST_MINUTE}",
                                    f"{day.date()} {SYNTHETIC_LAST_MINUTE}", freq="min"))
    index = stamps[0].append(stamps[1:])
    closes = SYNTHETIC_START_PRICE + np.cumsum(rng.normal(0.0, SYNTHETIC_STEP_SIGMA, len(index)))
    opens = np.concatenate([[SYNTHETIC_START_PRICE], closes[:-1]])
    wiggle = np.abs(rng.normal(0.0, SYNTHETIC_STEP_SIGMA / 2, len(index)))
    frame = pd.DataFrame({
        "open": opens.round(2),
        "high": (np.maximum(opens, closes) + wiggle).round(2),
        "low": (np.minimum(opens, closes) - wiggle).round(2),
        "close": closes.round(2),
    }, index=index)
    frame.index.name = "dt"
    return frame


@pytest.fixture
def ny_m1() -> pd.DataFrame:
    return make_ny_m1()
