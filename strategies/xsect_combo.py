"""S009+S012 combo book — funding-carry + cross-sectional momentum on one
account (ALGODEV-13).

Decision record (2026-08-30, Confluence S012 page): the validated way to run
S012 is NOT standalone (Gate 1 walk-forward FAIL) but as a diversifier inside
one combined book with S009. Weight grid on the honest 2022..2026 track put
the Sharpe plateau at 30-40% momentum with max drawdown at its minimum;
Anton fixed the working weight at 30% S012 / 70% S009. A portfolio-level
vol target (same 20%/yr risk control the S009 deploy already used) lifted
the combo Sharpe from ~1.33 to ~1.51 at every target level tested, so the
combo keeps vol targeting at the PORTFOLIO level and the sleeves run vol-off.

Mechanics: both sleeves produce their daily dollar-neutral weight books from
the SAME (close, funding) panels; the combo book is the weight-level blend

    w_combo = momentum_weight * w_S012 + (1 - momentum_weight) * w_S009

and only then costed — so opposite legs net out BEFORE turnover x fee, which
is exactly what one real account rebalancing to the blended book pays (a
return-level blend would double-count crossing trades).

No look-ahead: sleeves inherit it from their engines; the portfolio vol
scale uses shift(1) trailing vol inside xsect_base.portfolio_returns.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from strategies.funding_carry import FundingCarryConfig
from strategies.funding_carry import run_backtest as run_carry
from strategies.xsect_base import (  # noqa: F401  (re-exported public API)
    DEFAULT_UNIVERSE,
    MS_PER_DAY,
    load_panels,
    metrics,
    portfolio_returns,
)
from strategies.xsect_momentum import XSectMomentumConfig
from strategies.xsect_momentum import run_backtest as run_momentum

# Decision constants (2026-08-30, see module docstring). Referenced by the
# default config below AND by bot/s009_paper.py's DEPLOY so the deployed
# numbers cannot silently drift from the recorded decision.
MOMENTUM_WEIGHT = 0.30          # S012 share of the blended book (S009 gets the rest)
COMBO_VOL_TARGET_ANNUAL = 0.20  # portfolio-level vol target, same 20%/yr as the old S009 deploy


@dataclass(frozen=True)
class XSectComboConfig:
    """Frozen combo config: two vol-off sleeves + portfolio-level risk knobs.

    Sleeve configs keep their own signal parameters (champion S009 lb7 2/2,
    deployed S012 lb14 3/3) but MUST share the universe and stay vol-off —
    validated in __post_init__ so a mis-wired deploy fails loudly at import.
    """
    carry: FundingCarryConfig = field(default_factory=FundingCarryConfig)
    momentum: XSectMomentumConfig = field(default_factory=XSectMomentumConfig)
    momentum_weight: float = MOMENTUM_WEIGHT

    # Portfolio-level vol targeting (sleeves run vol-off, see module docstring).
    vol_target_annual: float = COMBO_VOL_TARGET_ANNUAL
    vol_lookback_days: int = 30
    vol_scale_cap: float = 3.0

    # One fee for the single blended book (turnover is costed after netting).
    taker_fee_per_side: float = 0.0

    def __post_init__(self):
        if tuple(self.carry.universe) != tuple(self.momentum.universe):
            raise ValueError("combo sleeves must share one universe")
        if self.carry.vol_target_annual or self.momentum.vol_target_annual:
            raise ValueError("sleeves must be vol-off; the combo vol-targets at portfolio level")
        if not 0.0 <= self.momentum_weight <= 1.0:
            raise ValueError("momentum_weight must be in [0, 1]")

    @property
    def universe(self) -> tuple:
        return self.carry.universe

    def with_(self, **changes) -> "XSectComboConfig":
        return dataclasses.replace(self, **changes)


def blend_weights(w_carry: pd.DataFrame, w_momentum: pd.DataFrame, momentum_weight: float) -> pd.DataFrame:
    """Weight-level blend of the two sleeve books (net first, cost later)."""
    return (momentum_weight * w_momentum + (1.0 - momentum_weight) * w_carry).fillna(0.0)


def run_backtest(close: pd.DataFrame, funding_day: pd.DataFrame, cfg: XSectComboConfig):
    """Return (out, w): same shape as the sleeve engines — out indexed by day
    with gross_ret / net_ret / turnover / n_pos, w = final (vol-scaled) book."""
    _, w_carry = run_carry(close, funding_day, cfg.carry)
    _, w_mom = run_momentum(close, funding_day, cfg.momentum)
    w = blend_weights(w_carry, w_mom, cfg.momentum_weight)

    price_ret = close.pct_change()
    valid = price_ret.notna() & funding_day.notna()
    return portfolio_returns(
        w, price_ret, funding_day, valid,
        taker_fee_per_side=cfg.taker_fee_per_side,
        vol_target_annual=cfg.vol_target_annual,
        vol_lookback_days=cfg.vol_lookback_days,
        vol_scale_cap=cfg.vol_scale_cap,
    )


# --------------------------------------------------------------------------
# Forward (live) book — what to hold for the day ahead
# --------------------------------------------------------------------------

def _forward_rank_book(sig_row: pd.Series, last_close: pd.Series, *,
                       top_n: int, bottom_n: int, min_universe: int,
                       gross_leverage: float, long_top: bool) -> dict:
    """One sleeve's unscaled forward book from its latest signal row — the
    live-path mirror of xsect_base.rank_weights for a single (next) day."""
    row = sig_row[sig_row.notna() & last_close.notna()].dropna()
    if len(row) < min_universe:
        return {}
    ordered = row.sort_values()
    long_n = top_n if long_top else bottom_n
    short_n = bottom_n if long_top else top_n
    lw = 0.5 * gross_leverage / long_n
    sw = 0.5 * gross_leverage / short_n
    if long_top:
        longs, shorts = ordered.index[-top_n:], ordered.index[:bottom_n]
    else:
        longs, shorts = ordered.index[:bottom_n], ordered.index[-top_n:]
    book = {s: lw for s in longs}
    book.update({s: -sw for s in shorts})
    return book


def forward_target_book(close: pd.DataFrame, funding_day: pd.DataFrame, cfg: XSectComboConfig) -> dict:
    """Blended book to hold for the day AHEAD, using data through the last
    closed day only. Same contract as bot/s009_paper.py::forward_target_book:
    selection from the latest signal rows, portfolio vol scale from the
    trailing realised vol of the vol-off combo track."""
    last_close = close.iloc[-1]

    carry_sig = funding_day.rolling(cfg.carry.signal_lookback_days, min_periods=1).mean().iloc[-1]
    book_carry = _forward_rank_book(
        carry_sig, last_close,
        top_n=cfg.carry.top_n, bottom_n=cfg.carry.bottom_n,
        min_universe=cfg.carry.min_universe, gross_leverage=cfg.carry.gross_leverage,
        long_top=False)

    m = cfg.momentum
    # momentum_signal() shifts by 1 to be strictly past for the NEXT day's
    # holding period — but here `close` already ends at the last closed day,
    # so the forward signal is the unshifted window ending at that day.
    mom_sig = (close.shift(m.skip_days) / close.shift(m.skip_days + m.lookback_days) - 1.0).iloc[-1]
    book_mom = _forward_rank_book(
        mom_sig, last_close,
        top_n=m.top_n, bottom_n=m.bottom_n,
        min_universe=m.min_universe, gross_leverage=m.gross_leverage,
        long_top=True)

    if not book_carry and not book_mom:
        return {}
    syms = set(book_carry) | set(book_mom)
    mw = cfg.momentum_weight
    book = {s: mw * book_mom.get(s, 0.0) + (1.0 - mw) * book_carry.get(s, 0.0) for s in syms}

    k = 1.0
    if cfg.vol_target_annual > 0:
        base = run_backtest(close, funding_day, cfg.with_(vol_target_annual=0.0))[0]["gross_ret"]
        realized = base.rolling(cfg.vol_lookback_days, min_periods=cfg.vol_lookback_days).std(ddof=0).iloc[-1]
        if realized and realized > 0:
            k = min(cfg.vol_scale_cap, (cfg.vol_target_annual / np.sqrt(365)) / float(realized))
    return {s: float(round(v * k, 4)) for s, v in book.items() if abs(v) > 1e-9}


# Live-signature self-check used by tests and bot --simulate: the forward book
# must equal the weights the BACKTEST would assign to the day after the panel
# ends (same guarantee bot/s009_paper.py's simulate() asserts for pure S009).


def _backtest_book_for_next_day(close: pd.DataFrame, funding_day: pd.DataFrame,
                                cfg: XSectComboConfig) -> dict:
    """Reference implementation for tests: extend the panel by one phantom day
    (NaN prices/funding contribute nothing to selection, which only looks at
    strictly-past data) and read the blended UNSCALED backtest book there."""
    next_day = int(close.index[-1]) + 1
    close2 = close.reindex(list(close.index) + [next_day])
    fund2 = funding_day.reindex(close2.index)
    # keep last close visible for validity, like the live path does
    close2.loc[next_day] = close.iloc[-1]
    fund2.loc[next_day] = 0.0
    _, w_carry = run_carry(close2, fund2, cfg.carry)
    _, w_mom = run_momentum(close2, fund2, cfg.momentum)
    w = blend_weights(w_carry, w_mom, cfg.momentum_weight).loc[next_day]
    return {s: float(round(v, 4)) for s, v in w.items() if abs(v) > 1e-9}
