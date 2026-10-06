"""S021 parity: the Python backtest engine vs the MQL5 EA, on the broker's own bars.

    python -m mt5.tools.s021_parity --bars US100_M1_<server>.csv \
        --ea-days S021-mt5-acct<login>_days.csv [--rule EET_US_DST] [--out report.csv]

--bars     ExportM1.mq5 output (SERVER time). Converted to the strategy's fixed
           UTC-5 clock with the same rule the EA used, then fed to
           strategies/orb_intraday/engine.py exactly like histdata.
--ea-days  the EA's per-day CSV (Strategy Tester or live), written by
           Strategies/S021_ORB/Runtime.mqh::FlushDayRow.

Checks, per day the EA reports levels for:
  levels     O, ADR14, U, L, stop distance vs the live-style Python levels
             (bot/orb_signals.py::_levels_for_today semantics) -- relative
             tolerance LEVEL_REL_TOLERANCE. Any miss is a FAILURE.
  entry      EA direction / entry minute vs engine.simulate(ORB_BASE). Known,
             explained differences are classified, not failed:
               engine_skip_both_in_bar  both levels inside one M1 bar: the
                                        engine skips the day, ticks decide live
               live_only_short_session  the day ends up < min_session_bars
                                        (half days): the engine never trades
                                        it, live cannot know in advance
               ea_missed_entry          a level was touched before the EA
                                        could place its stops
             anything else is a FAILURE.
Exit code 0 = parity holds, 1 = at least one failure.
"""
from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from mt5.tools import clock

LEVEL_REL_TOLERANCE = 1e-6
SERVER_TIME_FORMAT = "%Y.%m.%d %H:%M"
LEVEL_COLUMNS = (("open", "open"), ("adr", "adr"), ("upper", "upper"), ("lower", "lower"),
                 ("stop_distance", "stop_distance"))
EA_STATUSES_WITH_LEVELS = ("traded", "no_fill", "missed_entry", "skip_risk_cap",
                           "skip_account_guard", "placement_failed", "halted")

CATEGORY_MATCH = "match"
CATEGORY_BOTH_NO_TRADE = "both_no_trade"
CATEGORY_BOTH_IN_BAR = "engine_skip_both_in_bar"
CATEGORY_SHORT_SESSION = "live_only_short_session"
CATEGORY_MISSED = "ea_missed_entry"
CATEGORY_LEVEL_MISMATCH = "FAIL_levels"
CATEGORY_ENTRY_MISMATCH = "FAIL_entry"
FAIL_CATEGORIES = (CATEGORY_LEVEL_MISMATCH, CATEGORY_ENTRY_MISMATCH)


def _session_clock_rule() -> str:
    from strategies.orb_intraday import engine

    if engine.SESSION_TZ != "America/New_York":
        raise ValueError(f"no tz rule mirrors engine.SESSION_TZ {engine.SESSION_TZ!r}")
    return clock.RULE_EST_US_DST


def load_broker_bars(path: Path, rule: str, fixed_hours: int = 0) -> pd.DataFrame:
    """ExportM1 CSV (server time) -> engine-ready frame on the NY exchange clock."""
    raw = pd.read_csv(path)
    server_index = pd.DatetimeIndex(pd.to_datetime(raw["time_server"], format=SERVER_TIME_FORMAT))
    utc_index = clock.index_local_to_utc(server_index, rule, fixed_hours)
    clock_index = clock.index_utc_to_local(utc_index, _session_clock_rule())
    frame = raw[["open", "high", "low", "close"]].astype(float)
    frame.index = clock_index
    frame.index.name = "dt"
    return frame[~frame.index.duplicated()].sort_index()


def python_levels(m1: pd.DataFrame, config) -> pd.DataFrame:
    from mt5.tools.s021_fixtures import expected_levels

    levels = expected_levels(m1, config)
    levels["day"] = pd.to_datetime(levels["day"], format="%Y.%m.%d")
    return levels.set_index("day")


def engine_trades(m1: pd.DataFrame, config) -> pd.DataFrame:
    from strategies.orb_intraday.engine import simulate, trades_to_frame

    trades = trades_to_frame(simulate(m1, config))
    if trades.empty:
        return trades
    trades["day"] = pd.to_datetime(trades["entry_time"]).dt.normalize()
    return trades.set_index("day")


def _both_levels_in_one_bar(m1: pd.DataFrame, day: pd.Timestamp, upper: float, lower: float,
                            config) -> bool:
    start = pd.Timestamp.combine(day.date(), config.session_open)
    stop = pd.Timestamp.combine(day.date(), config.entry_cutoff)
    scan = m1.loc[start:stop]
    for _ts, bar in scan.iterrows():
        hit_upper, hit_lower = bar["high"] >= upper, bar["low"] <= lower
        if hit_upper or hit_lower:
            return hit_upper and hit_lower
    return False


def _near(actual: float, expected: float) -> bool:
    if pd.isna(actual) or pd.isna(expected):
        return pd.isna(actual) and pd.isna(expected)
    return math.isclose(actual, expected, rel_tol=LEVEL_REL_TOLERANCE, abs_tol=1e-9)


def compare(m1: pd.DataFrame, ea_days: pd.DataFrame, config) -> pd.DataFrame:
    levels = python_levels(m1, config)
    trades = engine_trades(m1, config)
    session_rule = _session_clock_rule()
    rows = []
    for _index, ea in ea_days.iterrows():
        day = pd.Timestamp(ea["day"])
        row = dict(day=day.date().isoformat(), ea_status=ea["status"], category=CATEGORY_MATCH,
                   detail="")
        if ea["status"] not in EA_STATUSES_WITH_LEVELS:
            row["category"] = "ea_no_levels"
            rows.append(row)
            continue
        if day not in levels.index:
            row.update(category=CATEGORY_LEVEL_MISMATCH, detail="python has no anchor bar")
            rows.append(row)
            continue
        py = levels.loc[day]
        misses = [f"{ea_col}: ea={ea[ea_col]} py={py[py_col]}"
                  for ea_col, py_col in LEVEL_COLUMNS if not _near(float(ea[ea_col]), float(py[py_col]))]
        if misses:
            row.update(category=CATEGORY_LEVEL_MISMATCH, detail="; ".join(misses))
            rows.append(row)
            continue

        ea_direction = ea["direction"] if isinstance(ea["direction"], str) else ""
        engine_trade = trades.loc[day] if (not trades.empty and day in trades.index) else None
        engine_direction = engine_trade["direction"] if engine_trade is not None else ""
        row.update(ea_direction=ea_direction, engine_direction=engine_direction)
        if ea_direction == engine_direction:
            if ea_direction:
                ea_entry_utc = pd.Timestamp(ea["entry_time_utc"]).to_pydatetime()
                ea_minute = pd.Timestamp(
                    clock.utc_to_local(session_rule, ea_entry_utc)).floor("min")
                engine_minute = pd.Timestamp(engine_trade["entry_time"]).floor("min")
                if ea_minute != engine_minute:
                    row.update(category=CATEGORY_ENTRY_MISMATCH,
                               detail=f"entry minute ea={ea_minute} engine={engine_minute}")
            else:
                row["category"] = CATEGORY_BOTH_NO_TRADE
        elif ea["status"] == "missed_entry":
            row["category"] = CATEGORY_MISSED
        elif not bool(py["valid_session"]):
            row["category"] = CATEGORY_SHORT_SESSION
        elif not engine_direction and _both_levels_in_one_bar(m1, day, py["upper"], py["lower"], config):
            row["category"] = CATEGORY_BOTH_IN_BAR
        else:
            row.update(category=CATEGORY_ENTRY_MISMATCH,
                       detail=f"direction ea={ea_direction!r} engine={engine_direction!r}")
        rows.append(row)
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    from strategies.orb_intraday.config import ORB_BASE

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--ea-days", type=Path, required=True)
    parser.add_argument("--rule", default=clock.RULE_EET_US_DST, choices=clock.RULES)
    parser.add_argument("--fixed-hours", type=int, default=0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    m1 = load_broker_bars(args.bars, args.rule, args.fixed_hours)
    ea_days = pd.read_csv(args.ea_days)
    report = compare(m1, ea_days, ORB_BASE)
    if args.out:
        report.to_csv(args.out, index=False)
    counts = report["category"].value_counts().to_dict() if not report.empty else {}
    print(f"bars={len(m1)} ea_days={len(ea_days)} categories={counts}")
    failures = report[report["category"].isin(FAIL_CATEGORIES)] if not report.empty else report
    for _index, failure in failures.head(20).iterrows():
        print(f"  {failure['day']} {failure['category']}: {failure['detail']}")
    return 1 if len(failures) else 0


if __name__ == "__main__":
    sys.exit(main())
