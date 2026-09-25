"""bot/orb_signals.py::_levels_for_today() vs. the independently-verified
backtest engine -- the "Gate 0 for the bot" check.

strategies/orb_intraday/engine.py's compute_daily_sessions/compute_adr14
were written for a full historical dataset, where "today" already has a
complete session's worth of bars. Live, "today" is always a PARTIAL day, so
_levels_for_today() reconstructs the same ADR14/O/U/L from history strictly
before today plus a synthetic "today" anchor row (see its own docstring for
why that's equivalent, not an approximation). This test proves that
equivalence empirically on the real NSXUSD dataset: for a sample of valid
trading days, truncating the feed to progressively more of that day's
session must never change the computed O/U/L/ADR14/stop distance from what
the full-dataset backtest computed for that same day.

Since 2026-09-22 the live bot reconstructs ADR14's HISTORY from M15 bars
(cTrader caps a trendbars response at ~14000 bars, which on M1 is fewer
than the 14 sessions ADR14 needs -- see bot/ctrader_orb.py's module
docstring) and only reads "today" from M1. There is no real M15 histdata
file, so the M15 series here is resampled from the same M1 data -- a
faithful stand-in for cTrader's own M15 trendbars, because session-range
aggregation (max of highs / min of lows over whole in-session bars) is
granularity-invariant, which is the premise of the fix and is itself
asserted below rather than assumed. The ONE thing that is not
granularity-invariant is the day-validity rule (M1 bar count, exact 09:30
M1 bar), so the Gate 0 comparison is split in two:

  - against the real M1 backtest, on every sampled day whose ADR14 lookback
    window has the same set of valid sessions under both rules (the vast
    majority -- see test_m15_sessions_match_m1_sessions_on_full_dataset);
  - against the same engine run on the M15 series, on EVERY sampled day,
    which isolates the live truncation/anchor/look-ahead logic itself from
    the granularity question.

The full-dataset M15 series is handed to _levels_for_today at EVERY
truncation point, including bars after the truncation time: the function's
own "strictly before today" filter is what must keep the future out, and
passing it future bars is a stronger test of that guard than truncating
them away here.
"""
from __future__ import annotations

import math
from datetime import time
from pathlib import Path

import pandas as pd
import pytest

from bot.orb_signals import _levels_for_today, _m15_min_session_bars, _valid_prior_sessions
from strategies.orb_intraday.config import ORB_BASE
from strategies.orb_intraday.engine import (
    compute_adr14, compute_daily_sessions, load_nsxusd_m1,
)

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "histdata"
requires_histdata = pytest.mark.skipif(
    not list(DATA_DIR.glob("DAT_ASCII_NSXUSD_M1_*.csv")),
    reason="NSXUSD histdata files not present in this environment")

M15_FREQ = "15min"
N_RECENT_SAMPLE_DAYS = 40      # most recent valid days (the original Gate 0 sample)
N_SPREAD_SAMPLE_DAYS = 20      # evenly spread over the whole dataset, all years


def _resample_m15(m1: pd.DataFrame) -> pd.DataFrame:
    """M1 -> M15 the way cTrader builds its own M15 trendbars: left-closed,
    left-labelled 15-minute bins on the same (fixed-EST) clock. 09:30 is 38
    whole bins after midnight, so the default midnight origin already puts a
    bin boundary exactly on the session open (asserted in
    test_m15_sessions_match_m1_sessions_on_full_dataset, not assumed). Empty
    bins (weekends, data gaps) are dropped -- the broker sends no bar there
    either."""
    m15 = m1.resample(M15_FREQ).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"})
    return m15.dropna(how="all")


@pytest.fixture(scope="module")
def backtest_reference():
    m1 = load_nsxusd_m1(DATA_DIR)
    daily = compute_daily_sessions(m1, ORB_BASE)
    adr14 = compute_adr14(daily, ORB_BASE)
    m15 = _resample_m15(m1)
    m15_cfg = ORB_BASE.with_(min_session_bars=_m15_min_session_bars(ORB_BASE))
    daily_m15 = compute_daily_sessions(m15, m15_cfg)
    adr14_m15 = compute_adr14(daily_m15, ORB_BASE)
    return dict(m1=m1, daily=daily, adr14=adr14, m15=m15,
                daily_m15=daily_m15, adr14_m15=adr14_m15)


def _sample_days(daily, adr14):
    valid = daily.index[~adr14.isna()]
    step = max(1, len(valid) // N_SPREAD_SAMPLE_DAYS)
    spread = valid[::step][:N_SPREAD_SAMPLE_DAYS]
    return spread.union(valid[-N_RECENT_SAMPLE_DAYS:])


@pytest.fixture(scope="module")
def live_levels(backtest_reference):
    """_levels_for_today() at three truncation points (open, midday, entry
    cutoff) of every sampled day, computed once and shared by the two Gate 0
    comparisons below. Key: (day, cut_ts) -> levels dict or None."""
    ref = backtest_reference
    m1, m15, cfg = ref["m1"], ref["m15"], ORB_BASE
    out = {}
    for day in _sample_days(ref["daily"], ref["adr14"]):
        day_open_ts = pd.Timestamp.combine(day.date(), cfg.session_open)
        day_mid_ts = pd.Timestamp.combine(day.date(), time(12, 0))
        day_cutoff_ts = pd.Timestamp.combine(day.date(), cfg.entry_cutoff)
        for cut_ts in (day_open_ts, day_mid_ts, day_cutoff_ts):
            truncated = m1.loc[m1.index <= cut_ts]
            if truncated.empty or truncated.index[-1] < day_open_ts:
                continue
            out[(day, cut_ts)] = _levels_for_today(truncated, m15, cfg)
    return out


def _assert_levels(levels, day, cut_ts, O_expected, adr_expected, cfg):
    assert levels is not None, f"{day.date()} @ {cut_ts.time()}: expected levels, got None"
    assert levels["O"] == pytest.approx(O_expected, abs=1e-9)
    assert levels["adr"] == pytest.approx(adr_expected, abs=1e-9)
    assert levels["U"] == pytest.approx(O_expected + cfg.k_range * adr_expected, abs=1e-9)
    assert levels["L"] == pytest.approx(O_expected - cfg.k_range * adr_expected, abs=1e-9)
    assert levels["stop_dist"] == pytest.approx(cfg.stop_adr_mult * adr_expected, abs=1e-9)


def _same_lookback_sessions(day, daily, daily_m15, cfg) -> bool:
    """True if `day`'s ADR14 lookback (the last adr_window valid sessions
    strictly before it) is the same list of dates under the M1 and the M15
    day-validity rule -- then the M15 ADR14 must equal the M1 backtest's
    exactly, since each session's range is granularity-invariant."""
    w1 = daily.index[daily.index < day][-cfg.adr_window:]
    w15 = daily_m15.index[daily_m15.index < day][-cfg.adr_window:]
    return w1.equals(w15)


@requires_histdata
def test_live_levels_match_m1_backtest_at_multiple_truncation_points(
        backtest_reference, live_levels):
    ref, cfg = backtest_reference, ORB_BASE
    daily, adr14, daily_m15 = ref["daily"], ref["adr14"], ref["daily_m15"]
    checked, skipped_days = 0, set()
    for (day, cut_ts), levels in live_levels.items():
        if not _same_lookback_sessions(day, daily, daily_m15, cfg):
            skipped_days.add(day)
            continue
        _assert_levels(levels, day, cut_ts, daily.loc[day, "session_open"],
                       adr14.loc[day], cfg)
        checked += 1
    n_days = len({d for d, _ in live_levels})
    assert checked > 0, "no truncation points were actually exercised"
    # The lookback-mismatch days are the (rare) ones whose window contains a
    # day the two validity rules disagree on; if that ever stops being rare,
    # this comparison has silently lost its power and must be looked at.
    assert len(skipped_days) <= n_days // 2, (
        f"{len(skipped_days)}/{n_days} sampled days skipped for an M1/M15 "
        f"validity mismatch in their ADR14 lookback: {sorted(skipped_days)}")


@requires_histdata
def test_live_levels_match_m15_engine_at_multiple_truncation_points(
        backtest_reference, live_levels):
    ref, cfg = backtest_reference, ORB_BASE
    daily, adr14_m15 = ref["daily"], ref["adr14_m15"]
    checked = 0
    for (day, cut_ts), levels in live_levels.items():
        _assert_levels(levels, day, cut_ts, daily.loc[day, "session_open"],
                       adr14_m15.loc[day], cfg)
        checked += 1
    assert checked > 0, "no truncation points were actually exercised"


@requires_histdata
def test_m15_sessions_match_m1_sessions_on_full_dataset(backtest_reference):
    """The premise of the M1->M15 history fix, checked on every day of the
    real dataset rather than assumed: (1) a resampled M15 bin boundary lands
    exactly on 09:30 for every valid session; (2) each day's session high/
    low/range is IDENTICAL from M15 and M1; (3) the M15 day-validity rule is
    never stricter than the backtest's M1 rule, and disagrees (the other
    way) on well under 1% of days -- round-to-nearest scaling instead of
    ceil admitted ~7.8% extra days on this dataset (see
    bot/orb_signals.py::_m15_min_session_bars)."""
    ref, cfg = backtest_reference, ORB_BASE
    daily, daily_m15, m15 = ref["daily"], ref["daily_m15"], ref["m15"]

    open_stamps = [pd.Timestamp.combine(d.date(), cfg.session_open) for d in daily.index]
    missing = [ts for ts in open_stamps if ts not in m15.index]
    assert not missing, f"no M15 bar at session open on: {missing[:10]}"

    assert daily.index.difference(daily_m15.index).empty, (
        "M15 validity rule rejected a day the M1 backtest accepts")
    common = daily.index.intersection(daily_m15.index)
    for col in ("session_open", "session_high", "session_low", "session_range"):
        diff = (daily.loc[common, col] - daily_m15.loc[common, col]).abs()
        assert (diff == 0).all(), f"{col} differs on {list(diff[diff != 0].index[:10])}"
    only_m15 = daily_m15.index.difference(daily.index)
    assert len(only_m15) < 0.01 * len(daily), (
        f"{len(only_m15)} days valid on M15 only: {list(only_m15[:10])}")


# ---------- synthetic (no histdata needed) ----------

SYNTH_OPEN = 100.0
SYNTH_RANGE = 10.0


def _prior_weekdays(today: pd.Timestamp, n: int) -> list[pd.Timestamp]:
    days, d = [], today - pd.Timedelta(days=1)
    while len(days) < n:
        if d.dayofweek < 5:
            days.append(d)
        d -= pd.Timedelta(days=1)
    return sorted(days)


def _m15_sessions(days, cfg, n_bars=None, rng=SYNTH_RANGE) -> pd.DataFrame:
    """Flat M15 sessions (open=SYNTH_OPEN, session range exactly `rng`) on
    each of `days`, starting at cfg.session_open; `n_bars` truncates each
    session to its first n_bars M15 bars (default: the full session)."""
    frames = []
    for d in days:
        idx = pd.date_range(pd.Timestamp.combine(d.date(), cfg.session_open),
                            pd.Timestamp.combine(d.date(), cfg.session_close), freq=M15_FREQ)
        if n_bars is not None:
            idx = idx[:n_bars]
        frames.append(pd.DataFrame(dict(open=SYNTH_OPEN, high=SYNTH_OPEN + rng / 2,
                                        low=SYNTH_OPEN - rng / 2, close=SYNTH_OPEN), index=idx))
    return pd.concat(frames).sort_index() if frames else pd.DataFrame()


def _m1_today(today: pd.Timestamp, cfg, start=None, periods=30) -> pd.DataFrame:
    start = start or pd.Timestamp.combine(today.date(), cfg.session_open)
    idx = pd.date_range(start, periods=periods, freq="1min")
    return pd.DataFrame(dict(open=SYNTH_OPEN, high=SYNTH_OPEN + 1, low=SYNTH_OPEN - 1,
                             close=SYNTH_OPEN), index=idx)


TODAY = pd.Timestamp("2026-01-28")   # a Wednesday


def test_m15_min_session_bars_matches_hand_computed_value():
    cfg = ORB_BASE
    # 09:30..15:59 inclusive = 390 minutes = 390 M1 bars = 26 M15 bars;
    # 350/390 of the session in M15 bars is 23.33, rounded UP to 24.
    session_minutes = (15 * 60 + 59) - (9 * 60 + 30) + 1
    assert session_minutes == 390
    expected = math.ceil(cfg.min_session_bars * (session_minutes // 15) / session_minutes)
    assert expected == 24
    assert _m15_min_session_bars(cfg) == expected
    # Defining property: the smallest M15 count that every M1-valid day
    # (>= min_session_bars M1 bars, 15 per M15 bar at most) must reach.
    thr = _m15_min_session_bars(cfg)
    assert 15 * (thr - 1) < cfg.min_session_bars <= 15 * thr


def test_m15_min_session_bars_tracks_the_config():
    # Derived from the config, not a hardcoded literal: a different
    # min_session_bars or session length moves it accordingly.
    assert _m15_min_session_bars(ORB_BASE.with_(min_session_bars=390)) == 26
    assert _m15_min_session_bars(ORB_BASE.with_(min_session_bars=345)) == 23
    # 09:30..12:59 = 210 minutes = 14 M15 bars; 189/210 of it -> 12.6 -> 13
    shorter = ORB_BASE.with_(session_close=time(12, 59), min_session_bars=189)
    assert _m15_min_session_bars(shorter) == 13


def test_exactly_adr_window_valid_sessions_is_enough():
    cfg = ORB_BASE
    m15 = _m15_sessions(_prior_weekdays(TODAY, cfg.adr_window), cfg)
    levels = _levels_for_today(_m1_today(TODAY, cfg), m15, cfg)
    assert levels is not None
    assert levels["day"] == TODAY
    assert levels["adr"] == pytest.approx(SYNTH_RANGE)
    assert levels["U"] == pytest.approx(SYNTH_OPEN + cfg.k_range * SYNTH_RANGE)
    assert levels["L"] == pytest.approx(SYNTH_OPEN - cfg.k_range * SYNTH_RANGE)
    assert levels["stop_dist"] == pytest.approx(cfg.stop_adr_mult * SYNTH_RANGE)


def test_one_session_short_of_adr_window_returns_none():
    # The direct regression guard for the original live bug: fewer valid
    # prior sessions than adr_window -> ADR14 NaN -> no levels. (The real
    # ~14000-bar broker cap that caused it can't be reproduced offline.)
    cfg = ORB_BASE
    m15 = _m15_sessions(_prior_weekdays(TODAY, cfg.adr_window - 1), cfg)
    assert len(_valid_prior_sessions(m15, TODAY, cfg)) == cfg.adr_window - 1
    assert _levels_for_today(_m1_today(TODAY, cfg), m15, cfg) is None


def test_session_below_m15_bar_threshold_does_not_count():
    cfg = ORB_BASE
    thr = _m15_min_session_bars(cfg)
    days = _prior_weekdays(TODAY, cfg.adr_window)
    full = _m15_sessions(days[1:], cfg)
    short = pd.concat([_m15_sessions(days[:1], cfg, n_bars=thr - 1), full]).sort_index()
    at_thr = pd.concat([_m15_sessions(days[:1], cfg, n_bars=thr), full]).sort_index()
    assert len(_valid_prior_sessions(short, TODAY, cfg)) == cfg.adr_window - 1
    assert _levels_for_today(_m1_today(TODAY, cfg), short, cfg) is None
    assert len(_valid_prior_sessions(at_thr, TODAY, cfg)) == cfg.adr_window
    assert _levels_for_today(_m1_today(TODAY, cfg), at_thr, cfg) is not None


def test_m15_bars_on_or_after_today_are_ignored():
    # Look-ahead guard: today's own M15 bars and later days' bars (with a
    # wildly different range) must not change ADR14 or count as history.
    cfg = ORB_BASE
    hist = _m15_sessions(_prior_weekdays(TODAY, cfg.adr_window), cfg)
    future_days = [TODAY, TODAY + pd.Timedelta(days=1), TODAY + pd.Timedelta(days=2)]
    polluted = pd.concat([hist, _m15_sessions(future_days, cfg, rng=1000.0)]).sort_index()
    base = _levels_for_today(_m1_today(TODAY, cfg), hist, cfg)
    with_future = _levels_for_today(_m1_today(TODAY, cfg), polluted, cfg)
    assert base is not None and with_future == base
    assert len(_valid_prior_sessions(polluted, TODAY, cfg)) == cfg.adr_window


def test_no_valid_09_30_anchor_returns_none():
    cfg = ORB_BASE
    # A day with bars starting after the session open has no valid anchor,
    # even with ample M15 history.
    m1 = _m1_today(TODAY, cfg, start=TODAY + pd.Timedelta(hours=10), periods=100)
    m15 = _m15_sessions(_prior_weekdays(TODAY, cfg.adr_window + 2), cfg)
    assert _levels_for_today(m1, m15, cfg) is None


def test_insufficient_history_returns_none():
    cfg = ORB_BASE
    # Valid 09:30 anchor today, but no prior sessions at all -> ADR14 unavailable,
    # whether the M15 history is empty or only holds today's own bars.
    m1 = _m1_today(TODAY, cfg)
    assert _levels_for_today(m1, pd.DataFrame(), cfg) is None
    assert _levels_for_today(m1, _m15_sessions([TODAY], cfg), cfg) is None
