"""Shared cross-sectional layer for daily crypto-perp strategies (S009, S012).

Extracted from strategies/funding_carry.py (ALGODEV-13) so the pro-trend
momentum sibling (S012) reuses one implementation instead of a copy:

  - load_panels()   — (close, funding_day) daily panels from
                      data/raw/crypto_funding/<SYM>/{d1,funding}.csv
  - rank_weights()  — per-day dollar-neutral long/short books from a signal rank
  - portfolio_returns() — the shared accounting  port_ret_d = Σ w*(p - f)
                      plus optional trailing-vol targeting and turnover costs
  - metrics()       — daily-return summary stats (Sharpe/Sortino/MaxDD/corr_BTC)

Accounting convention (derivation, same as S009): holding weight w in a coin
with day price return p and accrued funding f (longs pay f when f>0) earns
w*p - w*f = w*(p - f). Every perp position — carry or momentum — pays/receives
funding, so the funding term stays in the momentum book too.

No look-ahead by construction: callers must build `signal` from data strictly
through day d-1 (shift before ranking); rank_weights/portfolio_returns never
touch future rows themselves (vol targeting uses shift(1) internally).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

MS_PER_DAY = 86_400_000

DEFAULT_UNIVERSE = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
    "AVAXUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT", "ATOMUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "FILUSDT", "INJUSDT", "SUIUSDT", "UNIUSDT",
    "AAVEUSDT", "ETCUSDT", "BCHUSDT",
)


# --------------------------------------------------------------------------
# Data loading → aligned daily panels
# --------------------------------------------------------------------------

def load_panels(data_root: Path, universe) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (close, funding_day) daily panels indexed by UTC date int (days
    since epoch), columns = symbols. `funding_day[d]` sums the funding stamps that
    fall on day d; `close[d]` is the daily close of the candle opened on day d."""
    closes = {}
    fund = {}
    for sym in universe:
        d1p = data_root / sym / "d1.csv"
        fp = data_root / sym / "funding.csv"
        if not d1p.exists() or not fp.exists():
            continue
        d1 = pd.read_csv(d1p)
        d1["day"] = (d1["ts"].astype("int64") // MS_PER_DAY)
        closes[sym] = d1.groupby("day")["close"].last()
        f = pd.read_csv(fp)
        f["day"] = (f["ts"].astype("int64") // MS_PER_DAY)
        fund[sym] = f.groupby("day")["funding_rate"].sum()
    close = pd.DataFrame(closes).sort_index()
    funding_day = pd.DataFrame(fund).reindex(close.index).sort_index()
    return close, funding_day


# --------------------------------------------------------------------------
# Rank-based dollar-neutral weights
# --------------------------------------------------------------------------

def rank_weights(
    signal: pd.DataFrame,
    valid: pd.DataFrame,
    *,
    top_n: int,
    bottom_n: int,
    min_universe: int,
    gross_leverage: float,
    long_top: bool,
) -> pd.DataFrame:
    """Per day: rank by signal, put +0.5/-0.5 books (scaled by gross_leverage)
    on the extremes. long_top=False longs the LOWEST-signal names (S009
    funding-carry: long lowest funding, short highest); long_top=True longs the
    HIGHEST-signal names (S012 momentum: long winners, short losers)."""
    w = pd.DataFrame(0.0, index=signal.index, columns=signal.columns)
    long_n = top_n if long_top else bottom_n
    short_n = bottom_n if long_top else top_n
    long_w = 0.5 * gross_leverage / long_n
    short_w = 0.5 * gross_leverage / short_n
    sig = signal.where(valid)
    for d in signal.index:
        row = sig.loc[d].dropna()
        if len(row) < min_universe:
            continue
        ordered = row.sort_values()
        if long_top:
            longs = ordered.index[-top_n:]
            shorts = ordered.index[:bottom_n]
        else:
            longs = ordered.index[:bottom_n]
            shorts = ordered.index[-top_n:]
        w.loc[d, longs] = long_w
        w.loc[d, shorts] = -short_w
    return w


# --------------------------------------------------------------------------
# Portfolio accounting (returns, optional vol targeting, costs)
# --------------------------------------------------------------------------

def portfolio_returns(
    w: pd.DataFrame,
    price_ret: pd.DataFrame,
    funding_day: pd.DataFrame,
    valid: pd.DataFrame,
    *,
    taker_fee_per_side: float,
    vol_target_annual: float = 0.0,
    vol_lookback_days: int = 30,
    vol_scale_cap: float = 3.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Shared accounting: leg return = w*(price_ret - funding_day); cost =
    turnover x taker fee per side. Optional trailing-vol targeting (past-only,
    shift(1)) scales the weights. Returns (out, w_scaled)."""
    if vol_target_annual > 0:
        base_gross = (w * (price_ret - funding_day)).where(valid, 0.0).sum(axis=1)
        target_daily = vol_target_annual / np.sqrt(365)
        realized = base_gross.rolling(vol_lookback_days, min_periods=vol_lookback_days).std(ddof=0).shift(1)
        k = (target_daily / realized).clip(upper=vol_scale_cap).fillna(0.0)
        w = w.mul(k, axis=0)

    contrib = w * (price_ret - funding_day)
    gross = contrib.where(valid, 0.0).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    cost = turnover * taker_fee_per_side
    out = pd.DataFrame({
        "gross_ret": gross,
        "net_ret": gross - cost,
        "turnover": turnover,
        "n_pos": (w != 0).sum(axis=1),
    })
    return out, w


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def metrics(ret: pd.Series, close: pd.DataFrame | None = None) -> dict:
    r = ret[ret.index >= ret[ret != 0].index.min()] if (ret != 0).any() else ret
    n = len(r)
    ann = np.sqrt(365)
    mean, sd = r.mean(), r.std(ddof=0)
    eq = (1 + r).cumprod()
    peak = eq.cummax()
    dd = (eq / peak - 1).min()
    downside = r[r < 0].std(ddof=0)
    m = {
        "days": n,
        "CAGR": eq.iloc[-1] ** (365 / n) - 1 if n and eq.iloc[-1] > 0 else float("nan"),
        "Sharpe": (mean / sd * ann) if sd > 0 else float("nan"),
        "Sortino": (mean / downside * ann) if downside and downside > 0 else float("nan"),
        "MaxDD": dd,
        "total_ret": eq.iloc[-1] - 1 if n else float("nan"),
        "hit_day": (r > 0).mean(),
    }
    if close is not None and "BTCUSDT" in close.columns:
        btc = close["BTCUSDT"].pct_change().reindex(r.index)
        m["corr_BTC"] = r.corr(btc)
    return m
