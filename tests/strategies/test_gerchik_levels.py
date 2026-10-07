"""Gate 0 for S031 (strategies/gerchik_levels).

1. Unit checks of the course arithmetic (ATR paranormal exclusion, stop/luft).
2. Pivot/level causality on synthetic D1 data.
3. Truncation test on real data: trades that finished before a cut date must be
   identical whether or not the future after the cut exists (max|delta| = 0).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from strategies.gerchik_levels.config import ALL_MODELS, BASE_S031, STOP_ATR_S031
from strategies.gerchik_levels.engine import simulate, trades_to_frame
from strategies.gerchik_levels.levels import gerchik_atr, levels_by_day, stop_size

ROOT = Path(__file__).resolve().parents[2]
HISTDATA = ROOT / "data" / "histdata"


def _d1(ranges, start=100.0):
    idx = pd.date_range("2024-01-01", periods=len(ranges), freq="D")
    lo = np.full(len(ranges), start)
    return pd.DataFrame({"open": lo, "high": lo + np.array(ranges), "low": lo, "close": lo + 0.5}, index=idx)


def test_atr_excludes_paranormal_bars():
    # ten normal 1.0 ranges, then a 5.0 spike and a 0.1 dead day
    d1 = _d1([1.0] * 10 + [5.0, 0.1, 1.0])
    atr = gerchik_atr(d1, BASE_S031)
    assert np.isnan(atr[:5]).all()
    assert atr[12] == pytest.approx(1.0)       # spike and dead day ignored
    assert atr[11] == pytest.approx(1.0)


def test_atr_uses_only_past_days():
    d1 = _d1([1.0] * 12)
    a1 = gerchik_atr(d1, BASE_S031)
    d1.iloc[-1, d1.columns.get_loc("high")] = 1000.0   # today's bar must not matter
    a2 = gerchik_atr(d1, BASE_S031)
    assert a1[-1] == a2[-1]


def test_stop_and_luft_course_example():
    # lesson example: level 7380, stop 0.2% = 14.76, luft = 20% of stop
    stop = stop_size(7380.0, atr=np.nan, cfg=BASE_S031)
    assert stop == pytest.approx(14.76)
    assert BASE_S031.luft_frac * stop == pytest.approx(2.952)
    assert stop_size(100.0, atr=60.0, cfg=STOP_ATR_S031) == pytest.approx(10.0)


def test_pivot_visible_only_after_confirmation():
    k = BASE_S031.pivot_k
    highs = [100, 101, 102, 110, 103, 101, 100, 99, 98, 97] + [100] * 20
    n = len(highs)
    d1 = pd.DataFrame({"open": 99.0, "high": np.array(highs, float), "low": np.array(highs, float) - 2.0,
                       "close": np.array(highs, float) - 1.0},
                      index=pd.date_range("2024-01-01", periods=n, freq="D"))
    atr = np.full(n, 2.0)
    lv = levels_by_day(d1, atr, BASE_S031)
    piv = 3                                   # the 110 high
    for j in range(n):
        has = any(L.price == 110 and L.kind == "H" for L in lv[j])
        assert has == (j >= piv + k + 1), j


def _load(sym, start, end):
    from utils.histdata import load_histdata_m1_utc
    m1 = load_histdata_m1_utc(sym, HISTDATA)
    return m1.loc[start:end]


@pytest.mark.skipif(not HISTDATA.exists(), reason="histdata not available")
@pytest.mark.parametrize("model", ALL_MODELS)
def test_truncation_no_lookahead(model):
    m1 = _load("EURUSD", "2023-01-01", "2023-12-31")
    cfg = BASE_S031.with_(model=model)
    full = trades_to_frame(simulate("EURUSD", m1, cfg))
    for cut in ("2023-05-17 13:07", "2023-09-04 09:00", "2023-11-22 16:41"):
        cut_ts = pd.Timestamp(cut)
        part = trades_to_frame(simulate("EURUSD", m1.loc[:cut_ts], cfg))
        a = full[full["exit_time"] < cut_ts - pd.Timedelta(days=1)].reset_index(drop=True)
        b = part[part["exit_time"] < cut_ts - pd.Timedelta(days=1)].reset_index(drop=True)
        assert len(a) == len(b), (model, cut)
        if len(a):
            num = ["entry", "sl", "tp", "exit", "r_gross", "r_net"]
            assert np.abs(a[num].to_numpy() - b[num].to_numpy()).max() == 0.0
            assert (a["entry_time"] == b["entry_time"]).all()
