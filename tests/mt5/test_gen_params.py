"""mt5/tools/gen_params.py -- the MQL5 constant headers stay a pure function of the
Python source of truth (strategies/orb_intraday/config.py::ORB_BASE, bot/risk.py,
bot/account_guard.py)."""
from __future__ import annotations

import re

import pytest

from mt5.tools import gen_params
from strategies.orb_intraday.config import ORB_BASE


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
    assert values["S021_CLOCK_UTC_OFFSET_HOURS"] == "(-5)"    # fixed EST, no DST
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


def test_core_header_mirrors_bot_constants():
    from bot import account_guard, risk

    values = _defines(gen_params.render_core())
    assert float(values["ALGO_MIN_STOP_POINTS"]) == risk.MIN_STOP_POINTS
    assert float(values["ALGO_PCT"]) == account_guard.PCT
    assert values["ALGO_REASON_DAILY"] == f'"{account_guard.REASON_DAILY}"'
    assert values["ALGO_REASON_MAX"] == f'"{account_guard.REASON_MAX}"'
