"""mt5/tools/gen_params.py -- the MQL5 constant headers stay a pure function of the
Python source of truth (strategies/orb_intraday/config.py::ORB_BASE,
strategies/s004_config.py::S004_INTRADAY, bot/risk.py, bot/account_guard.py)."""
from __future__ import annotations

import re
from datetime import datetime

import pytest

from mt5.tools import clock, gen_params
from strategies import fvg_mtf
from strategies.orb_intraday.config import ORB_BASE
from strategies.s004_config import SESSION_TZ, S004_BASE, S004_INTRADAY


def _defines(text: str) -> dict[str, str]:
    return dict(re.findall(r"^#define (\w+)[ \t]+(\S+)", text, flags=re.MULTILINE))


def test_committed_headers_are_not_stale():
    stale = [header.relative_path for header in gen_params.stale_headers()]
    assert stale == [], f"regenerate with `python -m mt5.tools.gen_params`: {stale}"


def test_s021_header_carries_orb_base_values():
    values = _defines(gen_params.render_s021())
    assert int(values["S021_ADR_WINDOW"]) == ORB_BASE.adr_window
    assert float(values["S021_K_RANGE"]) == ORB_BASE.k_range
    assert float(values["S021_STOP_ADR_MULT"]) == ORB_BASE.stop_adr_mult
    assert int(values["S021_SESSION_OPEN_MINUTE"]) == 9 * 60 + 30
    assert int(values["S021_SESSION_CLOSE_MINUTE"]) == 15 * 60 + 59
    assert int(values["S021_ENTRY_CUTOFF_MINUTE"]) == 14 * 60 + 29
    assert int(values["S021_MIN_SESSION_BARS"]) == ORB_BASE.min_session_bars
    assert float(values["S021_MAX_GAP_DAYS_PER_SESSION"]) == ORB_BASE.max_gap_days_per_session
    assert values["S021_CLOCK_TZ_RULE"] == "TZ_EST_US_DST"    # NY exchange local, DST-aware
    assert values["S021_MAGIC_PREFIX"] == '"S021"'


def test_header_is_deterministic():
    assert gen_params.render_s021() == gen_params.render_s021()


@pytest.mark.parametrize("override", [
    dict(breakeven_at_r=0.5),
    dict(reversal_mode="sar"),
    dict(trail_adr_mult=0.5),
    dict(stop_on_close=True),
    dict(time_stop_minutes=30),
    dict(squeeze_preset_enabled=True),
])
def test_modifier_presets_cannot_be_exported_to_the_ea(override):
    # the EA implements the frozen base only -- exporting a modifier preset
    # would silently trade rules the EA does not implement
    with pytest.raises(ValueError, match="frozen base only"):
        gen_params.render_s021(ORB_BASE.with_(**override))


def test_backtest_only_cost_field_does_not_block_export():
    values = _defines(gen_params.render_s021(ORB_BASE.with_(cost_bps_roundtrip=3.0)))
    assert float(values["S021_K_RANGE"]) == ORB_BASE.k_range


def test_s004_header_carries_the_intraday_preset():
    values = _defines(gen_params.render_s004())
    assert float(values["S004_RR"]) == S004_INTRADAY.rr
    assert int(values["S004_SESSION_FIRST_HOUR"]) == min(S004_INTRADAY.entry_hours)
    assert int(values["S004_SESSION_LAST_HOUR"]) == max(S004_INTRADAY.entry_hours)
    assert int(values["S004_INTRADAY_CUTOFF_MINUTE"]) == 22 * 60 + 45
    assert values["S004_COST_INCLUSIVE_SIZING"] == "true"
    assert int(values["S004_MAX_TRADES_PER_DAY"]) == S004_INTRADAY.max_trades_per_day
    assert int(values["S004_SYMBOL_COUNT"]) == len(S004_INTRADAY.pairs)
    assert values["S004_SYMBOLS"] == '"' + ",".join(S004_INTRADAY.pairs) + '"'
    assert float(values["S004_BUFFER_PIPS"]) == fvg_mtf.BUFFER_PIPS
    assert int(values["S004_WINDOW_BARS"]) == fvg_mtf.WINDOW_BARS


def test_s004_session_clock_is_eet_on_european_dst_dates():
    # measured from the M15 data, not assumed: backtest/run_s004_clock_probe.py.
    # A fixed offset (or the US-dated TZ_EET_US_DST) drifts an hour every summer.
    assert gen_params.SESSION_CLOCK_RULES[SESSION_TZ] == "TZ_EET_EU_DST"
    assert _defines(gen_params.render_s004())["S004_CLOCK_TZ_RULE"] == "TZ_EET_EU_DST"
    assert clock.offset_seconds_at_utc(clock.RULE_EET_EU_DST, datetime(2025, 1, 15, 12)) == 2 * 3600
    assert clock.offset_seconds_at_utc(clock.RULE_EET_EU_DST, datetime(2025, 7, 15, 12)) == 3 * 3600


def test_s004_prop_rules_cannot_be_dropped_from_the_export():
    # the EA IS the prop preset; exporting the frozen base would silently trade
    # overnight, unsized-for-cost and without the portfolio daily cap
    with pytest.raises(ValueError, match="S004_INTRADAY preset only"):
        gen_params.render_s004(S004_BASE)


@pytest.mark.parametrize("override", [
    dict(mode="shift"),
    dict(stop="swing"),
])
def test_s004_entry_modes_the_ea_does_not_implement_are_rejected(override):
    with pytest.raises(ValueError, match="only"):
        gen_params.render_s004(S004_INTRADAY.with_(**override))


def test_s004_non_contiguous_session_is_rejected():
    with pytest.raises(ValueError, match="contiguous"):
        gen_params.render_s004(S004_INTRADAY.with_(entry_hours=(0, 1, 5)))


def test_s004_backtest_only_cost_fields_do_not_block_export():
    values = _defines(gen_params.render_s004(S004_INTRADAY.with_(spread_pips=1.5, pip=1.0)))
    assert float(values["S004_RR"]) == S004_INTRADAY.rr


def test_core_header_mirrors_bot_constants():
    from bot import account_guard, risk


    values = _defines(gen_params.render_core())
    assert float(values["ALGO_MIN_STOP_POINTS"]) == risk.MIN_STOP_POINTS
    assert float(values["ALGO_PCT"]) == account_guard.PCT
    assert values["ALGO_REASON_DAILY"] == f'"{account_guard.REASON_DAILY}"'
    assert values["ALGO_REASON_MAX"] == f'"{account_guard.REASON_MAX}"'
