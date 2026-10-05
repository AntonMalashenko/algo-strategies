"""Fixtures for Scripts/AlgoTrading/S021_SelfTest.mq5, produced by the Python
source of truth.

    python -m mt5.tools.s021_fixtures [--start 2025-09-01 --end 2025-12-31]
                                      [--rule EET_US_DST] [--out DIR]

Writes into DIR (default mt5/MQL5/Files/AlgoTrading/fixtures, git-ignored;
mt5/tools/install_mac.sh copies it to the terminal's Common/Files):

  s021_m1.csv       histdata NSXUSD M1 bars re-stamped in SERVER time under
                    --rule, "YYYY.MM.DD HH:MM" -- what an MT5 broker would show
  s021_meta.csv     key,value (server_tz_rule)
  s021_levels.csv   per strategy-clock day: was it a valid session in the
                    backtest engine, and O / ADR14 / U / L / stop exactly as
                    bot/orb_signals.py::_levels_for_today would compute them
                    from the SAME bars (prior sessions strictly before the day)
  clock_cases.csv   rule offsets from real zoneinfo zones, hour by hour
                    around every DST transition 2019-2027 + a regular grid

The default window covers the EU (Oct 26) and US (Nov 2) 2025 DST ends and
the Thanksgiving half day, i.e. the cases a timezone or session-validity bug
would break first.
"""
from __future__ import annotations

import argparse
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from mt5.tools import clock

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "mt5" / "MQL5" / "Files" / "AlgoTrading" / "fixtures"
DEFAULT_HISTDATA = REPO_ROOT / "data" / "histdata"
DEFAULT_START = "2025-09-01"
DEFAULT_END = "2025-12-31"
MT5_TIME_FORMAT = "%Y.%m.%d %H:%M"
MT5_DATE_FORMAT = "%Y.%m.%d"
PRICE_DECIMALS = 8

# zoneinfo zones each rule must equal (tests/mt5/test_clock.py asserts the same)
RULE_REFERENCE_ZONES = {
    clock.RULE_EET_US_DST: ("America/New_York", timedelta(hours=7)),
    clock.RULE_EET_EU_DST: ("Europe/Athens", timedelta(0)),
    clock.RULE_CET_EU_DST: ("Europe/Prague", timedelta(0)),
    clock.RULE_UTC: ("UTC", timedelta(0)),
}
CLOCK_CASE_YEARS = range(2019, 2028)
CLOCK_TRANSITION_WINDOW_HOURS = 3
CLOCK_GRID_STEP_HOURS = 61          # coprime with 24: walks through every hour of day


def reference_offset_seconds(rule: str, utc_naive: datetime) -> int:
    zone_name, shift = RULE_REFERENCE_ZONES[rule]
    aware = utc_naive.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(zone_name))
    return int((aware.utcoffset() + shift).total_seconds())


def clock_cases() -> pd.DataFrame:
    rows = []
    for rule, (zone_name, _shift) in RULE_REFERENCE_ZONES.items():
        instants: set[datetime] = set()
        start = datetime(min(CLOCK_CASE_YEARS), 1, 1)
        stop = datetime(max(CLOCK_CASE_YEARS) + 1, 1, 1)
        probe = start
        while probe < stop:
            instants.add(probe)
            probe += timedelta(hours=CLOCK_GRID_STEP_HOURS)
        # every transition of the reference zone, +- a few hours
        previous = reference_offset_seconds(rule, start)
        hour = start
        while hour < stop:
            current = reference_offset_seconds(rule, hour)
            if current != previous:
                for delta in range(-CLOCK_TRANSITION_WINDOW_HOURS, CLOCK_TRANSITION_WINDOW_HOURS + 1):
                    instants.add(hour + timedelta(hours=delta))
                previous = current
            hour += timedelta(hours=1)
        for instant in sorted(instants):
            rows.append(dict(rule=rule, utc=instant.strftime(MT5_TIME_FORMAT),
                             offset_seconds=reference_offset_seconds(rule, instant)))
    return pd.DataFrame(rows)


def to_server_frame(m1_fixed_est: pd.DataFrame, rule: str) -> pd.DataFrame:
    """histdata bars (naive fixed-EST index) -> naive SERVER-time index."""
    from strategies.orb_intraday import engine

    est_offset_hours = int(ZoneInfo(engine.HISTDATA_FIXED_OFFSET)
                           .utcoffset(datetime(2020, 1, 1)).total_seconds() // 3600)
    utc_index = m1_fixed_est.index - pd.Timedelta(hours=est_offset_hours)
    server = m1_fixed_est.copy()
    server.index = clock.index_utc_to_local(utc_index, rule)
    return server


def expected_levels(m1_fixed_est: pd.DataFrame, config) -> pd.DataFrame:
    """Per strategy-clock day that has a session-open bar: engine validity and
    the live-style levels (bot/orb_signals.py::_levels_for_today semantics,
    computed with the engine's own functions on M1 bars)."""
    from strategies.orb_intraday.engine import compute_adr14, compute_daily_sessions

    daily = compute_daily_sessions(m1_fixed_est, config)
    dates = m1_fixed_est.index.normalize()
    rows = []
    for day in dates.unique():
        open_ts = pd.Timestamp.combine(day.date(), config.session_open)
        if open_ts not in m1_fixed_est.index:
            continue
        open_price = float(m1_fixed_est.loc[open_ts, "open"])
        prior = daily.loc[daily.index < day]
        today_row = pd.DataFrame({"session_open": [open_price], "session_high": [math.nan],
                                  "session_low": [math.nan], "session_range": [math.nan],
                                  "n_bars": [0]}, index=[day])
        adr = compute_adr14(pd.concat([prior, today_row]), config).iloc[-1] if len(prior) else math.nan
        has_adr = not pd.isna(adr)
        rows.append(dict(
            day=day.strftime(MT5_DATE_FORMAT),
            valid_session=int(day in daily.index),
            open=open_price,
            adr=float(adr) if has_adr else math.nan,
            upper=open_price + config.k_range * adr if has_adr else math.nan,
            lower=open_price - config.k_range * adr if has_adr else math.nan,
            stop_distance=config.stop_adr_mult * adr if has_adr else math.nan,
        ))
    return pd.DataFrame(rows)


def write_fixtures(out_dir: Path, start: str, end: str, rule: str,
                   histdata_dir: Path = DEFAULT_HISTDATA) -> dict:
    from strategies.orb_intraday.config import ORB_BASE
    from strategies.orb_intraday.engine import load_nsxusd_m1

    out_dir.mkdir(parents=True, exist_ok=True)
    m1 = load_nsxusd_m1(histdata_dir)
    window = m1.loc[pd.Timestamp(start):pd.Timestamp(end) + pd.Timedelta(days=1)]
    if window.empty:
        raise SystemExit(f"no histdata bars between {start} and {end}")

    server = to_server_frame(window, rule)
    m1_out = pd.DataFrame({
        "time_server": server.index.strftime(MT5_TIME_FORMAT),
        "open": server["open"].round(PRICE_DECIMALS),
        "high": server["high"].round(PRICE_DECIMALS),
        "low": server["low"].round(PRICE_DECIMALS),
        "close": server["close"].round(PRICE_DECIMALS),
    })
    m1_out.to_csv(out_dir / "s021_m1.csv", index=False, lineterminator="\n")
    pd.DataFrame({"key": ["server_tz_rule", "source_start", "source_end"],
                  "value": [rule, start, end]}).to_csv(
        out_dir / "s021_meta.csv", index=False, header=False, lineterminator="\n")

    # prices were rounded for the CSV: compute expectations from the SAME values
    rounded = window.round(PRICE_DECIMALS)
    levels = expected_levels(rounded, ORB_BASE)
    levels.to_csv(out_dir / "s021_levels.csv", index=False, lineterminator="\n",
                  float_format="%.10f", na_rep="nan")
    cases = clock_cases()
    cases.to_csv(out_dir / "clock_cases.csv", index=False, lineterminator="\n")
    return dict(bars=len(m1_out), days=len(levels),
                days_with_levels=int(levels["adr"].notna().sum()), clock_cases=len(cases))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--rule", default=clock.RULE_EET_US_DST, choices=clock.RULES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--histdata", type=Path, default=DEFAULT_HISTDATA)
    args = parser.parse_args(argv)
    stats = write_fixtures(args.out, args.start, args.end, args.rule, args.histdata)
    print(f"fixtures -> {args.out}: {stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
