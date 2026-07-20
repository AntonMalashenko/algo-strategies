"""FX carry strategy: rank G10 currencies by short-term interest rate, go
long the top-N against USD and short the bottom-N, equal-weight, monthly
rebalance.

The academic "carry trade" payoff has two legs, not one: the currency's
price move against USD, AND the interest-rate differential accrued while
holding the position (that differential is what a swap/rollover pays or
charges in real trading). A backtest that only measures spot price return
of high-rate-vs-low-rate currencies is testing momentum, not carry -- so
`build_carry_portfolio` adds the accrued rate differential explicitly.

Look-ahead discipline (same convention as S004): the currency ranking for
month t is read from information dated AT t (that month's rate observation,
closed and known). The resulting long/short basket is held over month t+1,
and t+1's own rate differential (also fixed as of t, per standard
uncovered-interest-parity accounting) is what accrues as carry. Nothing
from t+1 is used to pick t+1's basket.

Cost model (opt-in, off by default -- zero cost args reproduce the E2
baseline exactly, same "flag off = old behavior" convention as S004):
  spread_bps    -- round-trip spread cost in bps of notional, charged on
                   TURNOVER (a currency entering/exiting/flipping a leg),
                   not on months where a currency simply stays put. Mirrors
                   run_donchian.py's `turnover * (cost_bps / 1e4)` model.
  swap_haircut  -- fraction of the theoretical rate differential a retail
                   broker's swap markup eats (0.0 = full "honest" FRED
                   rate as in E2; e.g. 0.3 = broker keeps 30% of the carry).
"""
from __future__ import annotations

import pandas as pd

NON_USD_CURRENCIES = ["AUD", "CAD", "CHF", "EUR", "GBP", "JPY", "NOK", "NZD", "SEK"]

# currency -> (pair name in data/raw/spot_g10/, is_base_quote)
# is_base_quote True  -> pair is quoted CCY/USD (EUR, GBP, AUD, NZD): price up = CCY stronger.
# is_base_quote False -> pair is quoted USD/CCY (JPY, CHF, CAD, NOK, SEK): price up = CCY weaker.
PAIR_MAP = {
    "EUR": ("EURUSD", True),
    "GBP": ("GBPUSD", True),
    "AUD": ("AUDUSD", True),
    "NZD": ("NZDUSD", True),
    "JPY": ("USDJPY", False),
    "CHF": ("USDCHF", False),
    "CAD": ("USDCAD", False),
    "NOK": ("USDNOK", False),
    "SEK": ("USDSEK", False),
}


def monthly_ccy_returns(spot_close: dict[str, pd.Series]) -> pd.DataFrame:
    """Turn daily spot closes into each currency's month-end-to-month-end
    price return, expressed uniformly as "being long CCY funded in USD"
    (positive = CCY appreciated against USD), regardless of quote
    convention. Uses the last available close each month -- no forward
    fill across a missing month-end, since that would fabricate a price.
    """
    out = {}
    for ccy, (pair, is_base) in PAIR_MAP.items():
        s = spot_close[pair].sort_index()
        month_end = s.resample("ME").last()
        if is_base:
            ret = month_end.pct_change()
        else:
            ret = (1.0 / month_end).pct_change()  # USD/CCY quote: invert first.
        out[ccy] = ret
    return pd.DataFrame(out)


def build_carry_portfolio(rates: pd.DataFrame, ccy_returns: pd.DataFrame,
                           n_long: int = 3, n_short: int = 3,
                           spread_bps: float = 0.0,
                           swap_haircut: float = 0.0) -> pd.DataFrame:
    """Construct the long-top/short-bottom carry basket and its monthly return.

    Parameters
    ----------
    rates : DataFrame indexed by month-end date, columns include 'USD' plus
        the entries of NON_USD_CURRENCIES, values = short-term interest
        rate in percent per annum (as produced by fetch_rates.py).
    ccy_returns : DataFrame indexed by month-end date, columns =
        NON_USD_CURRENCIES, values = that month's price return of CCY
        against USD (see monthly_ccy_returns).
    n_long, n_short : basket size per leg (equal-weight within each leg).
    spread_bps, swap_haircut : see module docstring. Both default to 0,
        which reproduces the frictionless E2 baseline exactly.

    Returns
    -------
    DataFrame indexed by the month the return is REALIZED IN (t+1), columns:
        signal_date      -- the month-end whose rates picked this basket (t)
        long_ccys, short_ccys -- comma-joined currency codes
        gross_return      -- price + full carry, before any costs
        carry_haircut_cost -- portion of gross_return lost to swap_haircut
        turnover_cost      -- spread cost from entering/exiting/flipping legs
        portfolio_return    -- gross_return - carry_haircut_cost - turnover_cost
    A month is skipped (not fabricated) if fewer than n_long+n_short
    currencies have a rate, or if any selected currency is missing its
    price return for the holding month.
    """
    non_usd = [c for c in NON_USD_CURRENCIES if c in rates.columns]
    dates = rates.index
    rows = []
    prev_weights = pd.Series(0.0, index=non_usd)

    for i in range(len(dates) - 1):
        t, t1 = dates[i], dates[i + 1]
        month_rates = rates.loc[t, non_usd].dropna()
        if len(month_rates) < n_long + n_short:
            continue
        usd_rate_t = rates.loc[t, "USD"]
        if pd.isna(usd_rate_t):
            continue

        ranked = month_rates.sort_values(ascending=False)
        longs = list(ranked.index[:n_long])
        shorts = list(ranked.index[-n_short:])

        if t1 not in ccy_returns.index:
            continue
        realized = ccy_returns.loc[t1]
        selected = longs + shorts
        if realized[selected].isna().any():
            continue

        rate_diff = month_rates - usd_rate_t         # percentage points, annualized
        full_carry = rate_diff / 12.0 / 100.0          # -> monthly fraction, "honest" rate
        kept_carry = full_carry * (1.0 - swap_haircut)  # what the broker actually pays/charges

        gross_total = realized[selected] + full_carry[selected]
        net_total = realized[selected] + kept_carry[selected]

        gross_ret = gross_total[longs].mean() - gross_total[shorts].mean()
        net_of_haircut = net_total[longs].mean() - net_total[shorts].mean()
        haircut_cost = gross_ret - net_of_haircut

        weights = pd.Series(0.0, index=non_usd)
        weights[longs] = 1.0 / n_long
        weights[shorts] = -1.0 / n_short
        turnover = (weights - prev_weights).abs().sum()
        turnover_cost = turnover * (spread_bps / 1e4)
        prev_weights = weights

        rows.append({
            "date": t1,
            "signal_date": t,
            "long_ccys": ",".join(longs),
            "short_ccys": ",".join(shorts),
            "gross_return": gross_ret,
            "carry_haircut_cost": haircut_cost,
            "turnover_cost": turnover_cost,
            "portfolio_return": net_of_haircut - turnover_cost,
        })
    return pd.DataFrame(rows).set_index("date")
