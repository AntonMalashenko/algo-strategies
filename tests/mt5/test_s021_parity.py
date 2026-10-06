"""mt5/tools/s021_parity.py and s021_fixtures.py on synthetic bars: the server-time
round trip is lossless, the parity check passes on engine-consistent EA output and
fails on a level or entry mismatch."""
from __future__ import annotations

import math

import pandas as pd
import pytest

from mt5.tools import clock, s021_fixtures, s021_parity
from strategies.orb_intraday.config import ORB_BASE

from .conftest import make_ny_m1

RULE = clock.RULE_EET_US_DST


def _export_csv(path, server_frame: pd.DataFrame) -> None:
    """Write bars the way Scripts/AlgoTrading/ExportM1.mq5 does."""
    out = pd.DataFrame({
        "time_server": server_frame.index.strftime(s021_parity.SERVER_TIME_FORMAT),
        "open": server_frame["open"], "high": server_frame["high"],
        "low": server_frame["low"], "close": server_frame["close"],
        "tick_volume": 1, "spread": 0,
    })
    out.to_csv(path, index=False)


def _ea_days_from_python(m1: pd.DataFrame) -> pd.DataFrame:
    """What a correct EA would write (Runtime.mqh::FlushDayRow columns)."""
    levels = s021_parity.python_levels(m1, ORB_BASE)
    trades = s021_parity.engine_trades(m1, ORB_BASE)
    rows = []
    for day, level in levels.iterrows():
        if pd.isna(level["adr"]):
            continue
        trade = trades.loc[day] if (not trades.empty and day in trades.index) else None
        entry_utc = ""
        if trade is not None:
            entry_utc = clock.local_to_utc(
                clock.RULE_EST_US_DST,
                pd.Timestamp(trade["entry_time"]).to_pydatetime()).isoformat()
        rows.append(dict(
            day=day.date().isoformat(), status="traded" if trade is not None else "no_fill",
            open=level["open"], adr=level["adr"], upper=level["upper"], lower=level["lower"],
            stop_distance=level["stop_distance"], sessions=0,
            direction=trade["direction"] if trade is not None else "",
            entry_time_utc=entry_utc, entry_price=0.0, lots=0.0, exit_reason="",
            exit_time_utc="", exit_price=0.0, profit=0.0))
    return pd.DataFrame(rows)


@pytest.fixture
def broker_csv(tmp_path, ny_m1):
    path = tmp_path / "US100_M1_test.csv"
    _export_csv(path, s021_fixtures.to_server_frame(ny_m1, RULE))
    return path


def test_server_time_round_trip_is_lossless(broker_csv, ny_m1):
    loaded = s021_parity.load_broker_bars(broker_csv, RULE)
    pd.testing.assert_frame_equal(loaded, ny_m1, check_freq=False)


def test_server_frame_is_a_constant_shift_from_the_ny_clock(ny_m1):
    # Both clocks switch on the US DST dates (NY = UTC-5/-4, an EET_US_DST
    # server = UTC+2/+3), so the gap between them is 7h year-round. That is a
    # property of this broker rule, not of the strategy clock -- the DST move
    # itself is asserted against UTC below.
    server = s021_fixtures.to_server_frame(ny_m1, RULE)
    shift = (server.index - ny_m1.index).to_series().dt.total_seconds() / 3600
    assert set(shift.round().unique()) == {7.0}


def test_ny_clock_really_moves_with_us_dst(ny_m1):
    utc = clock.index_local_to_utc(pd.DatetimeIndex(ny_m1.index), clock.RULE_EST_US_DST)
    shift = (utc - ny_m1.index).to_series().dt.total_seconds() / 3600
    assert set(shift.round().unique()) == {4.0, 5.0}   # EDT -> UTC+4, EST -> UTC+5


def test_parity_passes_on_engine_consistent_ea_output(broker_csv):
    m1 = s021_parity.load_broker_bars(broker_csv, RULE)
    ea_days = _ea_days_from_python(m1)
    assert len(ea_days) > 10
    report = s021_parity.compare(m1, ea_days, ORB_BASE)
    assert not report["category"].isin(s021_parity.FAIL_CATEGORIES).any(), report


def test_parity_flags_a_level_mismatch(broker_csv):
    m1 = s021_parity.load_broker_bars(broker_csv, RULE)
    ea_days = _ea_days_from_python(m1)
    ea_days.loc[ea_days.index[3], "adr"] *= 1.001
    report = s021_parity.compare(m1, ea_days, ORB_BASE)
    assert (report["category"] == s021_parity.CATEGORY_LEVEL_MISMATCH).sum() == 1


def test_parity_flags_an_unexplained_direction_mismatch(broker_csv):
    m1 = s021_parity.load_broker_bars(broker_csv, RULE)
    ea_days = _ea_days_from_python(m1)
    traded = ea_days.index[ea_days["status"] == "traded"]
    assert len(traded) > 0
    row = traded[0]
    ea_days.loc[row, "direction"] = "short" if ea_days.loc[row, "direction"] == "long" else "long"
    report = s021_parity.compare(m1, ea_days, ORB_BASE)
    assert (report["category"] == s021_parity.CATEGORY_ENTRY_MISMATCH).sum() == 1


def test_expected_levels_need_adr_window_prior_sessions():
    m1 = make_ny_m1()
    levels = s021_fixtures.expected_levels(m1, ORB_BASE)
    with_adr = levels.index[levels["adr"].notna()]
    assert levels["valid_session"].all()
    assert with_adr[0] == ORB_BASE.adr_window              # 15th day is the first with ADR14
    first = levels.loc[with_adr[0]]
    assert math.isclose(first["upper"] - first["open"], ORB_BASE.k_range * first["adr"])


def test_write_fixtures_end_to_end(tmp_path):
    histdata = tmp_path / "histdata"
    histdata.mkdir()
    m1 = make_ny_m1()
    raw = pd.DataFrame({
        "dt": m1.index.strftime("%Y%m%d %H%M%S"), "open": m1["open"], "high": m1["high"],
        "low": m1["low"], "close": m1["close"], "vol": 0})
    raw.to_csv(histdata / "DAT_ASCII_NSXUSD_M1_2025.csv", sep=";", header=False, index=False)
    out = tmp_path / "fixtures"
    stats = s021_fixtures.write_fixtures(out, "2025-09-15", "2025-11-30", RULE, histdata)
    assert {p.name for p in out.iterdir()} == {"s021_m1.csv", "s021_meta.csv",
                                                "s021_levels.csv", "clock_cases.csv"}
    assert stats["days_with_levels"] > 0
    meta = pd.read_csv(out / "s021_meta.csv", header=None, index_col=0)[1].to_dict()
    assert meta["server_tz_rule"] == RULE
