import numpy as np
import pandas as pd

from utils import metrics


def _equity():
    idx = pd.date_range("2023-01-01", periods=252, freq="B")
    return pd.Series(np.linspace(10000, 12000, len(idx)), index=idx)


def test_cagr_positive():
    assert metrics.cagr(_equity()) > 0


def test_maxdd_non_positive():
    assert metrics.max_drawdown(_equity()) <= 0


def test_sharpe_finite_on_flat_growth():
    eq = _equity()
    r = eq.pct_change().dropna()
    assert np.isfinite(metrics.sharpe(r))
