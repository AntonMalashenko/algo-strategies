"""S009 — crypto funding-carry: cross-sectional, market-neutral engine.

Idea: perpetual funding is a periodic payment between longs and shorts. We rank a
basket of perps by trailing funding, SHORT the highest-funding names (collect
funding + fade crowded longs) and LONG the lowest/most-negative (collect funding
as a long), equal-weight and dollar-neutral. Return of a leg = the perp's price
move PLUS accrued funding — the funding term is the carry (cf. the rate term in
S005 FX carry). No spot needed; the cross-section hedges market beta.

Design (parametric; baseline = FundingCarryConfig()):
  - Daily rebalance at 00:00 UTC.
  - Signal at day d = trailing mean daily funding through day d-1 (STRICTLY past —
    no look-ahead). Positions held during day d; return uses day-d price move and
    funding accrued during day d.
  - Weights: long book +0.5, short book -0.5 (gross 1.0, dollar-neutral), scaled
    by gross_leverage.
  - Costs: turnover x taker_fee_per_side (0 in the E2 baseline).

Accounting convention (derivation): holding weight w in a coin with day price
return p and accrued funding f (longs pay f when f>0) earns  w*p - w*f = w*(p - f).
So a short (w<0) in a high-funding coin (f>0) earns -w*f > 0. Hence
    port_ret_d = Σ_sym w[sym] * (price_ret[sym,d] - funding_day[sym,d]).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class FundingCarryConfig:
    universe: tuple = DEFAULT_UNIVERSE
    top_n: int = 3                    # SHORT the N highest-funding perps
    bottom_n: int = 3                 # LONG the N lowest-funding perps
    signal_lookback_days: int = 1     # trailing window for the funding signal
    min_universe: int = 6             # need at least this many valid coins to trade
    gross_leverage: float = 1.0       # scales the +0.5/-0.5 books
    taker_fee_per_side: float = 0.0   # E2 baseline = 0; E3 sets ~0.00055

    # Optional vol-targeting modifier (default OFF → reproduces baseline byte-for-byte).
    # When >0, each day's weights are scaled so the strategy's trailing realised vol
    # matches the target, capped at vol_scale_cap. Uses only past returns (no look-ahead).
    vol_target_annual: float = 0.0    # 0 = off; e.g. 0.15 targets ~15% annualised vol
    vol_lookback_days: int = 30
    vol_scale_cap: float = 3.0        # max leverage multiple

    reserved_oos_start: str = "2025-07-20"

    def with_(self, **changes) -> "FundingCarryConfig":
        return dataclasses.replace(self, **changes)


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
# Engine
# --------------------------------------------------------------------------

def compute_weights(signal: pd.DataFrame, valid: pd.DataFrame, cfg: FundingCarryConfig) -> pd.DataFrame:
    """Per day: short top_n by signal, long bottom_n, dollar-neutral weights."""
    w = pd.DataFrame(0.0, index=signal.index, columns=signal.columns)
    long_w = 0.5 * cfg.gross_leverage / cfg.bottom_n
    short_w = 0.5 * cfg.gross_leverage / cfg.top_n
    sig = signal.where(valid)
    for d in signal.index:
        row = sig.loc[d].dropna()
        if len(row) < cfg.min_universe:
            continue
        ordered = row.sort_values()
        longs = ordered.index[:cfg.bottom_n]      # lowest funding → long
        shorts = ordered.index[-cfg.top_n:]       # highest funding → short
        w.loc[d, longs] = long_w
        w.loc[d, shorts] = -short_w
    return w


def run_backtest(close: pd.DataFrame, funding_day: pd.DataFrame, cfg: FundingCarryConfig):
    """Return a DataFrame indexed by day with columns: gross_ret, net_ret, turnover, n_pos."""
    price_ret = close.pct_change()                         # p_d = C[d]/C[d-1]-1, held day d
    # Signal: trailing mean daily funding through d-1 (STRICTLY past → shift(1)).
    signal = funding_day.rolling(cfg.signal_lookback_days, min_periods=1).mean().shift(1)
    valid = price_ret.notna() & funding_day.notna() & signal.notna()

    w = compute_weights(signal, valid, cfg)

    # Optional vol-targeting: scale weights by trailing realised vol (past-only).
    if cfg.vol_target_annual > 0:
        base_gross = (w * (price_ret - funding_day)).where(valid, 0.0).sum(axis=1)
        target_daily = cfg.vol_target_annual / np.sqrt(365)
        realized = base_gross.rolling(cfg.vol_lookback_days, min_periods=cfg.vol_lookback_days).std(ddof=0).shift(1)
        k = (target_daily / realized).clip(upper=cfg.vol_scale_cap).fillna(0.0)
        w = w.mul(k, axis=0)

    # leg return = w * (price_ret - funding_day); NaNs (untraded) contribute 0
    contrib = w * (price_ret - funding_day)
    gross = contrib.where(valid, 0.0).sum(axis=1)
    turnover = w.diff().abs().sum(axis=1).fillna(w.abs().sum(axis=1))
    cost = turnover * cfg.taker_fee_per_side
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
