"""Data-availability check for S026 (WTI Crude Oil, EIA event volatility).

This is deliberately NOT a backtest runner. S026's mechanic (per Confluence page
24838145) needs INTRADAY WTI price data around each EIA Weekly Petroleum Status
Report release (build a pre-event range 15-30 min before the number, trade the
breakout, close 1-2h later or EOD) -- and this script's job is to state plainly
whether that data exists before anyone writes a backtest that would otherwise
silently run on too few events to mean anything.

Usage:
    python -m backtest.run_eia_calendar_check
"""
from __future__ import annotations

import pandas as pd

from strategies.eia_calendar import report_release_datetimes


def check_calendar(start: str, end: str) -> None:
    resolved, unresolved = report_release_datetimes(start, end)
    print(f"EIA release calendar, {start}..{end}: {len(resolved)} resolved events, "
          f"{len(unresolved)} unresolved (year-end boundary weeks not yet in "
          f"KNOWN_HOLIDAY_SHIFTS -- see strategies/eia_calendar.py)")
    if unresolved:
        print("  Unresolved week-ending Fridays (need manual registration before "
              "use, see the module's UnregisteredYearEndShiftError message):")
        for d in unresolved:
            print(f"    {d.date()}")
    per_year = pd.Series(resolved).dt.year.value_counts().sort_index()
    print(f"  Events per year: {dict(per_year)}")


def check_intraday_data_availability() -> None:
    print("\nIntraday WTI (CL=F) data availability via utils.data.load_yf (yfinance):")
    try:
        import yfinance as yf
    except ImportError:
        print("  yfinance not installed here -- cannot check. Install it first "
              "(pip install yfinance) before trusting any conclusion below.")
        return

    for interval in ("15m", "30m", "1h"):
        df = yf.download("CL=F", period="60d", interval=interval,
                         auto_adjust=False, progress=False)
        if df.empty:
            print(f"  {interval}: no data returned")
            continue
        span_days = (df.index.max() - df.index.min()).days
        print(f"  {interval}: {len(df)} bars, {df.index.min().date()}..{df.index.max().date()} "
              f"(~{span_days} calendar days)")

    print(
        "\n  CONCLUSION: Yahoo Finance (yfinance) only serves ~60 calendar days of "
        "intraday history for any interval below daily -- this is a Yahoo platform "
        "limit, not something utils.data.load_yf can work around. Weekly EIA events "
        "at ~1/week means at most ~8-9 usable historical events from this source, "
        "far below the sample the P12 spec itself already worried was thin even at "
        "a full year (~50 events). A real backtest of S026 is NOT possible from "
        "this project's current free data source.\n"
        "\n  Two real paths forward, neither attempted here (both are decisions for "
        "Anton, not something to wire up unprompted):\n"
        "    1. cTrader Open API historical trendbars (bot/ctrader_orb.py /\n"
        "       ctrader_s007.py / ctrader_s011.py already use ProtoOAGetTrendbarsReq\n"
        "       against the live demo account for other CFDs) -- if the broker\n"
        "       offers an oil CFD (e.g. XTIUSD/USOIL) with multi-year minute history,\n"
        "       this is the same pattern S021/S007 already use, just a new symbol.\n"
        "       Touches the live broker connection, so needs an explicit go-ahead.\n"
        "    2. Start a forward-only collector NOW (same 'невозвратность' logic as\n"
        "       the Deribit options-snapshot job in infra-scheduled-jobs-spec.md):\n"
        "       log real intraday prices around each future EIA release as they\n"
        "       happen, accepting that a usable sample is then a year or more away,\n"
        "       not something available today."
    )


def main():
    check_calendar("2015-01-01", "2026-12-31")
    check_intraday_data_availability()


if __name__ == "__main__":
    main()
