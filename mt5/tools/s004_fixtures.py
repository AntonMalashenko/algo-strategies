"""Fixtures for Scripts/AlgoTrading/S004_SelfTest.mq5, produced by the Python
source of truth (ALGODEV-62 phase C/D).

    python -m mt5.tools.s004_fixtures [--start 2024-01-01 --end 2025-12-31]
                                      [--symbols EURUSD,GBPJPY] [--out DIR]

Writes into DIR (default mt5/MQL5/Files/AlgoTrading/fixtures, git-ignored;
mt5/tools/install_mac.sh copies it to the terminal's Common/Files):

  s004_m15.csv     symbol,time_server,open,high,low,close -- the same M15 bars
                   backtest/s004_metalabel_data.py feeds the engine. No clock
                   conversion happens here: these files are ALREADY stamped on
                   the broker's EET/EEST clock (strategies/s004_config.py::
                   SESSION_TZ, measured by mt5/tools/s004_clock_probe.py).
  s004_trades.csv  every trade strategies/fvg_mtf.py::run_backtest produces on
                   those bars under the S004_INTRADAY preset -- the expectation
                   the MQL5 engine port must reproduce one for one.
  s004_meta.csv    key,value (pip, cost_price, rr, symbols, window)

The trade list is deliberately NOT filtered to the Asia window. The backtest
filters entry hours only afterwards (backtest/run_s004_intraday.py), so the
engine also opens positions outside it; those "shadow" trades consume their
zone and hold the one-position-per-symbol lock, and an EA that skipped them
would drift away from the backtest. The self-test therefore asserts the full,
unfiltered list -- shadow trades included.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest.s004_metalabel_data import load_combined
from strategies.fvg_mtf import run_backtest
from strategies.s004_config import PIP_RAW, SPREAD_PIPS, S004_INTRADAY

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "mt5" / "MQL5" / "Files" / "AlgoTrading" / "fixtures"
# Two pairs, two years: ~70k bars and a few hundred trades, which covers both
# DST transitions in each year, every exit reason and both directions.
DEFAULT_SYMBOLS = ("EURUSD", "GBPJPY")
DEFAULT_START = "2024-01-01"
DEFAULT_END = "2025-12-31"
MT5_TIME_FORMAT = "%Y.%m.%d %H:%M"
PRICE_DECIMALS = 8          # what the CSV carries; expectations use the same values
TRADE_COLUMNS = ["symbol", "time_in", "time_out", "dir", "entry", "sl", "tp",
                 "exit", "r", "exit_reason", "hour"]


def window_bars(symbol: str, start: str, end: str) -> pd.DataFrame:
    m15 = load_combined(symbol)
    bars = m15.loc[pd.Timestamp(start):pd.Timestamp(end) + pd.Timedelta(days=1)]
    if bars.empty:
        raise SystemExit(f"no {symbol} bars between {start} and {end}")
    return bars.round(PRICE_DECIMALS)


def write_fixtures(out_dir: Path, symbols: tuple[str, ...], start: str, end: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    bar_frames, trade_frames = [], []
    for symbol in symbols:
        bars = window_bars(symbol, start, end)
        bar_frames.append(pd.DataFrame({
            "symbol": symbol,
            "time_server": bars.index.strftime(MT5_TIME_FORMAT),
            "open": bars["open"], "high": bars["high"],
            "low": bars["low"], "close": bars["close"],
        }))
        trades = run_backtest(bars, **S004_INTRADAY.engine_kwargs())
        trades["symbol"] = symbol
        trade_frames.append(trades)

    all_bars = pd.concat(bar_frames, ignore_index=True)
    all_bars.to_csv(out_dir / "s004_m15.csv", index=False, lineterminator="\n",
                    float_format="%.10f")
    trades = pd.concat(trade_frames, ignore_index=True)
    for column in ("time_in", "time_out"):
        trades[column] = trades[column].dt.strftime(MT5_TIME_FORMAT)
    trades[TRADE_COLUMNS].to_csv(out_dir / "s004_trades.csv", index=False,
                                 lineterminator="\n", float_format="%.10f")
    pd.DataFrame({
        "key": ["pip", "cost_price", "rr", "symbols", "start", "end"],
        "value": [PIP_RAW, SPREAD_PIPS * PIP_RAW, S004_INTRADAY.rr,
                  " ".join(symbols), start, end],
    }).to_csv(out_dir / "s004_meta.csv", index=False, header=False, lineterminator="\n")
    return dict(bars=len(all_bars), trades=len(trades),
                exit_reasons=trades["exit_reason"].value_counts().to_dict())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    symbols = tuple(s.strip() for s in args.symbols.split(",") if s.strip())
    stats = write_fixtures(args.out, symbols, args.start, args.end)
    print(f"fixtures -> {args.out}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
