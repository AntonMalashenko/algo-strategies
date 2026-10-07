"""mt5/tools/s004_parity.py on synthetic bars: the broker export round trip, and the
comparison passing on engine-consistent EA output while catching a wrong price, a
missing trade, an invented one and a mis-applied daily cap."""
from __future__ import annotations

import pandas as pd
import pytest

from mt5.tools import clock, s004_parity
from strategies.s004_config import PIP_RAW, SPREAD_PIPS, S004_INTRADAY

from .conftest import make_fx_m1

RULE = clock.RULE_EET_US_DST        # a broker clock that is NOT the session clock
SYMBOLS = ("AUDUSD", "EURUSD")      # two pairs, so the portfolio cap has to pick
COST = PIP_RAW * SPREAD_PIPS
SCALES = {symbol: (PIP_RAW, COST) for symbol in SYMBOLS}


def _to_server(frame: pd.DataFrame) -> pd.DataFrame:
    """Session-clock bars -> the broker's own clock, the inverse of the tool's hop."""
    utc = clock.index_local_to_utc(frame.index, clock.RULE_EET_EU_DST)
    server = frame.copy()
    server.index = clock.index_utc_to_local(utc, RULE)
    return server


def _export_csv(path, session_frame: pd.DataFrame) -> None:
    """Write bars the way Scripts/AlgoTrading/ExportM1.mq5 does."""
    server = _to_server(session_frame)
    pd.DataFrame({
        "time_server": server.index.strftime(s004_parity.SERVER_TIME_FORMAT),
        "open": server["open"], "high": server["high"],
        "low": server["low"], "close": server["close"],
        "tick_volume": 1, "spread": 0,
    }).to_csv(path, index=False)


def _ea_trades_from_engine(engine: pd.DataFrame) -> pd.DataFrame:
    """What a correct EA would write (Runtime.mqh::FlushTradeRow columns)."""
    rows = []
    for _index, trade in engine.iterrows():
        taken = trade["expected_status"] == s004_parity.STATUS_TAKEN
        rows.append(dict(
            symbol=trade["symbol"],
            time_in=trade["time_in"].strftime(s004_parity.SERVER_TIME_FORMAT),
            time_out=trade["time_out"].strftime(s004_parity.SERVER_TIME_FORMAT),
            dir=int(trade["dir"]), entry=trade["entry"], sl=trade["sl"], tp=trade["tp"],
            exit=trade["exit"], r=round(float(trade["r"]), 6),
            exit_reason=trade["exit_reason"], hour=int(trade["hour"]),
            status=s004_parity.STATUS_TAKEN if taken else trade["expected_status"],
            fill=trade["entry"] if taken else 0.0, lots=0.1 if taken else 0.0,
            ticket=1 if taken else 0, pip=PIP_RAW, cost=COST))
    return pd.DataFrame(rows, columns=["symbol", "time_in", "time_out", "dir", "entry", "sl",
                                       "tp", "exit", "r", "exit_reason", "hour", "status",
                                       "fill", "lots", "ticket", "pip", "cost"])


@pytest.fixture(scope="module")
def session_bars() -> dict:
    # Two correlated-but-different walks, so the two pairs sometimes signal on
    # the same bar and the symbol-name tie-break actually matters.
    return {symbol: make_fx_m1(seed=20261007 + offset)
            for offset, symbol in enumerate(SYMBOLS)}


@pytest.fixture(scope="module")
def m15(session_bars, tmp_path_factory) -> dict:
    """The bars as the tool sees them: exported on the server clock, read back."""
    directory = tmp_path_factory.mktemp("exports")
    out = {}
    for symbol, frame in session_bars.items():
        path = directory / f"{symbol}_M1_Broker-Demo.csv"
        _export_csv(path, frame)
        out[symbol] = s004_parity.load_broker_bars(path, RULE)
    return out


@pytest.fixture(scope="module")
def engine(m15) -> pd.DataFrame:
    return s004_parity.engine_trades(m15, S004_INTRADAY, SCALES)


@pytest.fixture
def ea(engine) -> pd.DataFrame:
    return s004_parity.load_ea_trades_frame(_ea_trades_from_engine(engine))


def _categories(engine: pd.DataFrame, ea: pd.DataFrame) -> dict:
    report = s004_parity.compare(engine, ea, since=None, until=None)
    return report["category"].value_counts().to_dict()


def test_symbol_of_reads_the_export_name(tmp_path):
    assert s004_parity.symbol_of(tmp_path / "GBPJPY_M1_FundingPips-Server.csv") == "GBPJPY"
    with pytest.raises(ValueError):
        s004_parity.symbol_of(tmp_path / "GBPJPY.csv")


def test_server_clock_round_trip_lands_on_the_session_clock(session_bars, m15):
    """The export is written on the broker clock and read back on the session one."""
    for symbol, frame in session_bars.items():
        expected = frame.resample(s004_parity.M15).agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        pd.testing.assert_frame_equal(m15[symbol][expected.columns], expected, check_freq=False)


def test_the_synthetic_data_exercises_every_decision(engine):
    """A test that only ever saw virtual trades would prove nothing."""
    statuses = set(engine["expected_status"])
    assert s004_parity.STATUS_TAKEN in statuses
    assert s004_parity.STATUS_VIRTUAL_HOUR in statuses
    assert s004_parity.STATUS_VIRTUAL_CAP in statuses


def test_an_engine_consistent_ea_is_full_parity(engine, ea):
    assert _categories(engine, ea) == {s004_parity.CATEGORY_MATCH: len(engine)}


def test_shadow_counts_as_taken(engine, ea):
    """InpTradeEnabled=false still spends the cap, so `shadow` is a match."""
    ea.loc[ea["status"] == s004_parity.STATUS_TAKEN, "status"] = s004_parity.STATUS_SHADOW
    assert _categories(engine, ea) == {s004_parity.CATEGORY_MATCH: len(engine)}


def test_a_wrong_price_fails(engine, ea):
    ea.loc[0, "sl"] = float(ea.loc[0, "sl"]) + 1.0
    categories = _categories(engine, ea)
    assert categories[s004_parity.CATEGORY_FAIL_FIELDS] == 1


def test_a_wrong_r_fails(engine, ea):
    ea.loc[0, "r"] = float(ea.loc[0, "r"]) + 0.01
    assert _categories(engine, ea)[s004_parity.CATEGORY_FAIL_FIELDS] == 1


def test_a_trade_the_ea_never_reported_fails(engine, ea):
    # Not the first row: the window starts at the EA's first entry, so dropping
    # that one only moves the window (see test_the_warmup_window_is_not_compared).
    assert _categories(engine, ea.drop(index=5).reset_index(drop=True)) \
        [s004_parity.CATEGORY_FAIL_MISSING] == 1


def test_a_trade_the_engine_never_took_fails(engine, ea):
    invented = ea.loc[[1]].copy()
    invented["time_in"] = invented["time_in"] + pd.Timedelta(minutes=15)
    extended = pd.concat([ea, invented], ignore_index=True)
    assert _categories(engine, extended)[s004_parity.CATEGORY_FAIL_EXTRA] == 1


def test_a_cap_the_ea_got_wrong_fails(engine, ea):
    """The one error a shadow run exists to catch: a trade taken past the cap."""
    over_cap = ea.index[ea["status"] == s004_parity.STATUS_VIRTUAL_CAP][0]
    ea.loc[over_cap, "status"] = s004_parity.STATUS_TAKEN
    assert _categories(engine, ea)[s004_parity.CATEGORY_FAIL_STATUS] == 1


def test_an_out_of_session_trade_the_ea_took_fails(engine, ea):
    out_of_hours = ea.index[ea["status"] == s004_parity.STATUS_VIRTUAL_HOUR][0]
    ea.loc[out_of_hours, "status"] = s004_parity.STATUS_TAKEN
    assert _categories(engine, ea)[s004_parity.CATEGORY_FAIL_STATUS] == 1


def test_live_only_outcomes_are_classified_not_failed(engine, ea):
    taken = ea.index[ea["status"] == s004_parity.STATUS_TAKEN]
    ea.loc[taken[0], "status"] = s004_parity.STATUS_MISSED_FILL
    ea.loc[taken[1], "status"] = s004_parity.STATUS_VIRTUAL_HALT
    categories = _categories(engine, ea)
    assert categories[s004_parity.CATEGORY_MISSED_FILL] == 1
    assert categories[s004_parity.CATEGORY_HALTED] == 1
    assert not set(categories) & set(s004_parity.FAIL_CATEGORIES)


def test_a_missed_fill_outside_the_session_still_fails(engine, ea):
    """`missed_fill` only excuses a trade the backtest would have taken."""
    out_of_hours = ea.index[ea["status"] == s004_parity.STATUS_VIRTUAL_HOUR][0]
    ea.loc[out_of_hours, "status"] = s004_parity.STATUS_MISSED_FILL
    assert _categories(engine, ea)[s004_parity.CATEGORY_FAIL_STATUS] == 1


def test_the_warmup_window_is_not_compared(engine, ea):
    """The EA replays its warmup silently; trades older than its first row are history."""
    late = ea[ea["time_in"] > ea["time_in"].iloc[5]].reset_index(drop=True)
    categories = _categories(engine, late)
    assert not set(categories) & set(s004_parity.FAIL_CATEGORIES)
    assert categories[s004_parity.CATEGORY_MATCH] == len(late)


def test_main_end_to_end(tmp_path, session_bars, engine):
    for symbol, frame in session_bars.items():
        _export_csv(tmp_path / f"{symbol}_M1_Broker-Demo.csv", frame)
    ea_path = tmp_path / "S004-mt5-acct1_trades.csv"
    _ea_trades_from_engine(engine).to_csv(ea_path, index=False)
    report = tmp_path / "report.csv"
    code = s004_parity.main(["--bars", str(tmp_path), "--ea-trades", str(ea_path),
                             "--rule", RULE, "--out", str(report)])
    assert code == 0
    assert len(pd.read_csv(report)) == len(engine)
