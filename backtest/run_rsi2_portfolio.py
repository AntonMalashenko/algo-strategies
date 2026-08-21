"""S011/RSI(2) multi-asset portfolio backtest -- Gate 0/1/3 for the "one
account, all assets" question (see `strategies/rsi2_portfolio.py` for the
engine and sizing rationale).

Run from the algo repo root:
    python backtest/run_rsi2_portfolio.py

Requires the S011 multi-asset universe under data/raw/ (equities/FX/gold via
scripts/fetch_yahoo_universe.py, crypto via scripts/fetch_crypto_bybit.py)
and S009's crypto_funding data (scripts/fetch_funding_bybit.py) for the
Gate 3 correlation check.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from strategies.rsi2 import BASELINE_RSI2, rsi2_signal                              # noqa: E402
from strategies.rsi2_portfolio import PortfolioConfig, simulate_compounding_portfolio  # noqa: E402
from strategies.funding_carry import (                                               # noqa: E402
    FundingCarryConfig, load_panels as load_s009_panels, run_backtest as run_s009, MS_PER_DAY,
)

RAW = REPO / "data" / "raw"
S009_DATA = REPO / "data" / "raw" / "crypto_funding"

PORTFOLIO_START = pd.Timestamp("2021-01-01")
IS_CUTOFF = pd.Timestamp("2018-12-31")     # walk-forward split for non-crypto assets
WF_SHARPE_THRESHOLD = 0.15                 # min IS-only standalone Sharpe to select an asset
CRYPTO_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT")

EQUITY_INDEX_FILES = {
    "DOW": ("DOW", "DOW.csv"), "NASDAQ": ("NASDAQ", "NASDAQ.csv"),
    "SP500idx": ("SP500", "SP500.csv"), "RUSSELL": ("RUSSELL", "RUSSELL.csv"),
    "DAX": ("DAX", "DAX.csv"), "CAC40": ("CAC40", "CAC40.csv"),
    "FTSE100": ("FTSE100", "FTSE100.csv"), "ESTOXX50": ("ESTOXX50", "ESTOXX50.csv"),
}
FX_PAIRS = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD",
            "AUDJPY", "EURGBP", "EURJPY", "EURCHF", "GBPJPY")

# Walk-forward-selected universe, frozen 2026-08-18 (see claude/experiments-log.md,
# S011 E4/E5): assets kept ONLY on pre-2019 (or, for crypto, first-half-of-life)
# standalone Sharpe >= WF_SHARPE_THRESHOLD. This is the Gate-1-honest champion --
# NOT the larger "profitable in 2021-2026" list from the same research session,
# which was selected on the same window it was tested on (flagged there as
# data-snooping, not used as the frozen candidate here).
WALK_FORWARD_UNIVERSE = (
    "CAC40", "DOW", "ESTOXX50", "ETHUSDT", "EURGBP", "EURUSD", "FTSE100",
    "NASDAQ", "RUSSELL", "SOLUSDT", "SP500idx", "SPY", "XAUUSD",
)


def _load_std(path: Path, date_col: str = "date") -> pd.DataFrame:
    df = pd.read_csv(path)
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.set_index(date_col).sort_index()
    return df[["open", "high", "low", "close"]].dropna()


def _load_crypto(sym: str) -> pd.DataFrame:
    df = pd.read_csv(RAW / "crypto" / sym / "d1.csv")
    df["date"] = pd.to_datetime(df["datetime"]).dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close"]].dropna()


def load_universe() -> dict[str, pd.DataFrame]:
    universe = {"SPY": _load_std(RAW / "SPY" / "SPYd1.csv")}
    for name, (folder, fname) in EQUITY_INDEX_FILES.items():
        universe[name] = _load_std(RAW / folder / fname)
    universe["XAUUSD"] = _load_std(RAW / "XAUUSD" / "XAUUSDd1.csv")
    for fx in FX_PAIRS:
        universe[fx] = _load_std(RAW / fx / f"{fx}d1.csv")
    for sym in CRYPTO_SYMBOLS:
        universe[sym] = _load_crypto(sym)
    return universe


def build_positions_and_returns(universe: dict[str, pd.DataFrame]):
    positions, rets = {}, {}
    for name, df in universe.items():
        if len(df) < 260:
            continue
        pos = rsi2_signal(df, BASELINE_RSI2)
        positions[name] = pos.shift(1).fillna(0.0)
        rets[name] = df["close"].pct_change()
    return positions, rets


def report(equity: pd.Series, label: str, start_capital: float) -> None:
    years = (equity.index[-1] - equity.index[0]).days / 365.25
    ret = equity.pct_change().dropna()
    cagr = (equity.iloc[-1] / start_capital) ** (1 / years) - 1
    ppy_eff = len(ret) / years   # actual observed periods/year on this calendar -- NOT a
                                 # hardcoded 252, since a crypto-inclusive union calendar
                                 # runs closer to 365 obs/year (see experiments-log.md 2026-08-18)
    ann_vol = ret.std() * np.sqrt(ppy_eff)
    sharpe = (ret.mean() * ppy_eff) / ann_vol if ann_vol > 1e-12 else float("nan")
    dd = equity / equity.cummax() - 1.0
    maxdd = dd.min()
    calmar = cagr / abs(maxdd) if maxdd != 0 else float("nan")
    print(f"\n=== {label} ===")
    print(f"{equity.index[0].date()}..{equity.index[-1].date()} ({years:.2f}y)  "
          f"Final ${equity.iloc[-1]:,.0f} (x{equity.iloc[-1]/start_capital:.2f})")
    print(f"CAGR {cagr*100:.2f}%  Sharpe {sharpe:.2f}  MaxDD {maxdd*100:.2f}%  Calmar {calmar:.2f}")


def gate3_correlation(equity: pd.Series, label: str) -> None:
    ret = equity.pct_change().dropna()
    base = FundingCarryConfig()
    close9, funding9 = load_s009_panels(S009_DATA, base.universe)
    champ9 = base.with_(taker_fee_per_side=0.00055, signal_lookback_days=7,
                         top_n=2, bottom_n=2, vol_target_annual=0.20)
    out9, _ = run_s009(close9, funding9, champ9)
    s009_ret = out9["net_ret"].copy()
    s009_ret.index = pd.to_datetime(s009_ret.index * MS_PER_DAY, unit="ms", utc=True).tz_localize(None)
    both = pd.DataFrame({label: ret, "s009_funding_carry": s009_ret}).dropna()
    corr = both[label].corr(both["s009_funding_carry"])
    print(f"\nGate 3: corr({label}, S009 funding-carry champion) = {corr:+.3f}  "
          f"(n={len(both)} overlapping days)")
    print("Gate 3 caveat: S004/S007 daily equity not available in this environment this "
          "session (no saved trade logs / separate repo) -- not correlated here, open item.")


def main() -> None:
    universe = load_universe()
    positions, rets = build_positions_and_returns(universe)
    cfg = PortfolioConfig()

    positions_2021 = {a: s[s.index >= PORTFOLIO_START] for a, s in positions.items()}
    rets_2021 = {a: s[s.index >= PORTFOLIO_START] for a, s in rets.items()}

    wf_positions = {a: positions_2021[a] for a in WALK_FORWARD_UNIVERSE if a in positions_2021}
    wf_rets = {a: rets_2021[a] for a in WALK_FORWARD_UNIVERSE if a in rets_2021}
    equity_wf = simulate_compounding_portfolio(wf_positions, wf_rets, cfg)
    report(equity_wf, f"WALK-FORWARD universe ({len(wf_positions)} assets), "
                       f"{PORTFOLIO_START.date()}+, cap {cfg.cap_pct:.0%} compounding", cfg.start_capital)
    gate3_correlation(equity_wf, "rsi2_wf_universe")


if __name__ == "__main__":
    main()
