"""Gate 0 coverage for strategies/gold_session_momentum.py (S024).

These tests protect the four places where a Donchian-breakout momentum engine
most easily goes wrong silently, i.e. still produces plausible-looking trades
while the backtest is quietly invalid:

* look-ahead anywhere in the pipeline (indicators or the bar-by-bar trade
  loop) -- proved the standard Gate 0 way: truncate the input and assert that
  everything already emitted before the cutoff is unchanged;
* the Donchian channel including the breakout bar itself (or using a wrong
  window length) -- the channel must be the PRIOR 20 bars only;
* the EMA(200) trend filter not actually gating direction;
* the ATR-expansion filter (documented interpretation: ATR(14) above its own
  trailing 20-bar mean) not actually gating entries.

All data here is synthetic and hand-built (no dependency on data/histdata),
so the tests are fast and deterministic. The base series is a flat sine
oscillation with a period of 10 bars: every 20-bar Donchian window then
contains two full cycles, so the oscillation alone can NEVER close outside
the channel -- every breakout in these tests is one placed on purpose.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.gold_session_momentum import (GOLD_MOMENTUM_BASE, add_indicators,
                                              simulate, trades_to_frame)

BARS_PER_DAY = 96          # M15
START = "2024-01-02"       # a Tuesday; synthetic data ignores weekends anyway
OSC_PERIOD = 10            # bars per sine cycle (must divide the 20-bar Donchian window)
WICK = 0.2                 # high/low distance from close on ordinary bars


def _bars(n_days: int, level_jumps: dict[int, float] | None = None,
          amplitude: float = 1.0, amp_changes: dict[int, float] | None = None) -> pd.DataFrame:
    """Sine-oscillation M15 series around a piecewise-constant level.

    level_jumps maps bar position -> level change applied from that bar on;
    amp_changes maps bar position -> new oscillation amplitude from that bar on.
    Open = previous close (continuous tape)."""
    n = n_days * BARS_PER_DAY
    idx = pd.date_range(START, periods=n, freq="15min")
    level = np.full(n, 100.0)
    for pos, delta in (level_jumps or {}).items():
        level[pos:] += delta
    amp = np.full(n, amplitude)
    for pos, a in (amp_changes or {}).items():
        amp[pos:] = a
    close = level + amp * np.sin(2 * np.pi * np.arange(n) / OSC_PERIOD)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + WICK
    low = np.minimum(open_, close) - WICK
    return pd.DataFrame(dict(open=open_, high=high, low=low, close=close), index=idx)


def _pos(day: int, hh: int, mm: int = 0) -> int:
    """Bar position of day `day` (0-based) at hh:mm UTC."""
    return day * BARS_PER_DAY + hh * 4 + mm // 15


def _gate0_series() -> pd.DataFrame:
    """Six synthetic days with several deliberate in-session breakouts in both
    directions (days 0-1 are EMA200 warm-up)."""
    jumps = {
        _pos(2, 10): +5.0,    # day 2 10:00: up-breakout, above EMA -> long
        _pos(3, 9): -12.0,    # day 3 09:00: down-breakout, below EMA -> short
        _pos(4, 13): -6.0,    # day 4 13:00: further down-breakout -> short
        _pos(5, 8): +25.0,    # day 5 08:00: big up-breakout back above EMA -> long
    }
    return _bars(6, jumps)


def _random_walk_series(n_days: int = 20, seed: int = 20260923) -> pd.DataFrame:
    """Seeded random walk with volatility clustering, for a broader Gate 0 check
    (many more trades than the hand-built series, in both directions)."""
    rng = np.random.default_rng(seed)
    n = n_days * BARS_PER_DAY
    vol = 0.5 + 0.4 * np.abs(np.sin(np.arange(n) / 37.0))
    close = 2000.0 + np.cumsum(rng.normal(0.0, 1.0, n) * vol)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0.0, 0.5, n)) * vol
    low = np.minimum(open_, close) - np.abs(rng.normal(0.0, 0.5, n)) * vol
    idx = pd.date_range(START, periods=n, freq="15min")
    return pd.DataFrame(dict(open=open_, high=high, low=low, close=close), index=idx)


# --------------------------------------------------------------------------- Gate 0


def _assert_prefix_identical(full: pd.DataFrame, cutoff: pd.Timestamp) -> int:
    """Trades entered before a DAY-BOUNDARY cutoff must be byte-identical when
    the engine only sees data up to the cutoff (all trades are force-closed the
    same day, so they are fully resolved before a midnight cutoff)."""
    ft = trades_to_frame(simulate(full))
    tt = trades_to_frame(simulate(full.loc[full.index < cutoff]))
    ft = ft[ft["entry_time"] < cutoff].reset_index(drop=True)
    tt = tt[tt["entry_time"] < cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(ft, tt, check_exact=True)
    return len(ft)


def test_gate0_no_look_ahead_truncation_hand_built_series():
    """Standard Gate 0 proof: a trade emitted before the cutoff must not change
    when every bar after the cutoff is removed. A failure means some indicator
    or the trade loop reads future bars."""
    full = _gate0_series()
    all_trades = trades_to_frame(simulate(full))
    assert len(all_trades) == 4, "series was built to produce exactly 4 trades"
    assert list(all_trades["direction"]) == ["long", "short", "short", "long"]
    for day in (3, 4, 5):
        cutoff = full.index[_pos(day, 0)]
        n_before = _assert_prefix_identical(full, cutoff)
        assert n_before == day - 2   # one trade per day from day 2 onward


def test_gate0_no_look_ahead_truncation_random_walk():
    """Same Gate 0 truncation proof on a seeded random walk -- many trades, both
    directions, all three exit reasons, at several cutoffs."""
    full = _random_walk_series()
    all_trades = trades_to_frame(simulate(full))
    assert len(all_trades) >= 10
    assert set(all_trades["direction"]) == {"long", "short"}
    for day in (4, 7, 10, 13, 16, 19):
        _assert_prefix_identical(full, full.index[_pos(day, 0)])


def test_gate0_entry_decisions_unchanged_on_mid_session_cutoff():
    """With a cutoff INSIDE a session, a trade entered before it may be exited
    differently (the truncated data forces an earlier time exit) -- but its
    ENTRY decision (time, direction, price, frozen SL/TP) must be identical:
    the entry never depends on bars after the signal bar."""
    full = _random_walk_series()
    entry_cols = ["day", "direction", "signal_time", "entry_time", "entry_price",
                  "stop_price", "tp_price", "channel_level", "ema_at_entry", "atr_at_entry"]
    ft = trades_to_frame(simulate(full))
    checked = 0
    for day in range(3, 20):
        cutoff = full.index[_pos(day, 12, 30)]
        tt = trades_to_frame(simulate(full.loc[full.index < cutoff]))
        a = ft[ft["signal_time"] < cutoff - pd.Timedelta(minutes=15)][entry_cols]
        b = tt[tt["signal_time"] < cutoff - pd.Timedelta(minutes=15)][entry_cols]
        pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True),
                                      check_exact=True)
        checked += len(a)
    assert checked > 0


def test_indicators_are_causal_under_truncation():
    """Every indicator column at bar i must be identical whether or not bars
    after i exist -- guards against e.g. a centered rolling window or a
    backward-filled warm-up sneaking into add_indicators."""
    full = _random_walk_series()
    cutoff = 700
    a = add_indicators(full)
    b = add_indicators(full.iloc[:cutoff])
    cols = ["donchian_high", "donchian_low", "ema", "atr", "atr_mean", "atr_expanding",
            "long_signal", "short_signal"]
    pd.testing.assert_frame_equal(a.iloc[:cutoff][cols], b[cols], check_exact=True)


# --------------------------------------------------------------------------- Donchian


def test_donchian_channel_is_the_prior_20_bars_only():
    """donchian_high[i] must equal max(High[i-20:i]) and donchian_low[i] must
    equal min(Low[i-20:i]) exactly -- prior 20 bars, current bar excluded, no
    off-by-one window (19 or 21 bars)."""
    df = _random_walk_series(n_days=2)
    ind = add_indicators(df)
    h, lo = df["high"].to_numpy(), df["low"].to_numpy()
    for i in range(len(df)):
        if i < 20:
            assert np.isnan(ind["donchian_high"].iat[i])
            continue
        assert ind["donchian_high"].iat[i] == h[i - 20:i].max()
        assert ind["donchian_low"].iat[i] == lo[i - 20:i].min()


def test_breakout_fires_on_causal_channel_where_inclusive_channel_would_not():
    """The breakout bar closes above the prior-20-bar high but below its own
    high. The causal channel (current bar excluded) must fire a long on
    exactly that bar; a channel that included the current bar would sit at
    High[i] >= Close[i] and could never fire -- the bug this guards against."""
    brk = _pos(2, 10)
    df = _bars(3, {brk: +5.0})
    ind = add_indicators(df)
    close_i, high_i = df["close"].iat[brk], df["high"].iat[brk]
    prior_max = df["high"].iloc[brk - 20:brk].max()
    inclusive_max = df["high"].iloc[brk - 19:brk + 1].max()

    assert close_i > prior_max                  # breaks the causal channel
    assert close_i <= inclusive_max == high_i   # would NOT break an inclusive one
    assert ind["donchian_high"].iat[brk] == prior_max
    assert bool(ind["long_signal"].iat[brk])

    trades = simulate(df)
    assert len(trades) == 1
    t = trades[0]
    assert t.direction == "long"
    assert t.signal_time == df.index[brk]
    assert t.entry_price == close_i
    assert t.channel_level == prior_max


# --------------------------------------------------------------------------- EMA200 filter


def _counter_trend_series(up_jump: float) -> tuple[pd.DataFrame, int]:
    """Level 100 for two days (EMA ~100), then an overnight drop to 90 at day 2
    00:00 (outside the session, so not an entry), then an up-breakout of
    `up_jump` at day 2 10:00."""
    brk = _pos(2, 10)
    return _bars(3, {_pos(2, 0): -10.0, brk: up_jump}), brk


def test_ema200_filter_blocks_a_long_breakout_below_the_ema():
    """A genuine Donchian up-breakout with expanding ATR, but still below the
    EMA200 (bearish regime), must not be traded -- and must NOT be flipped
    into a short either. Guards against the trend filter being dropped or
    applied to the wrong side."""
    df, brk = _counter_trend_series(up_jump=4.0)
    ind = add_indicators(df)
    assert bool(ind["breakout_long"].iat[brk])      # real channel breakout
    assert bool(ind["atr_expanding"].iat[brk])      # volatility filter passes
    assert df["close"].iat[brk] < ind["ema"].iat[brk]  # but it is below EMA200
    assert not bool(ind["long_signal"].iat[brk])
    assert simulate(df) == []


def test_ema200_filter_control_same_breakout_above_the_ema_is_taken():
    """Control for the test above: the identical construction with a jump big
    enough to close above the EMA200 IS traded long -- proves the block above
    is due to the EMA filter, not some other condition."""
    df, brk = _counter_trend_series(up_jump=20.0)
    ind = add_indicators(df)
    assert df["close"].iat[brk] > ind["ema"].iat[brk]
    trades = simulate(df)
    assert len(trades) == 1
    assert trades[0].direction == "long" and trades[0].signal_time == df.index[brk]


# --------------------------------------------------------------------------- ATR expansion filter


def _contraction_series(breakout_close_offset: float, breakout_low_offset: float
                        ) -> tuple[pd.DataFrame, int]:
    """Amplitude-3 oscillation for two days (high ATR), contraction to
    amplitude 0.2 from day 2 00:00 (ATR decaying), then at day 2 08:00 a
    breakout bar closing `breakout_close_offset` above the level 100 with its
    low at 100 + `breakout_low_offset`."""
    df = _bars(3, amplitude=3.0, amp_changes={_pos(2, 0): 0.2})
    brk = _pos(2, 8)
    c = 100.0 + breakout_close_offset
    df.iloc[brk, df.columns.get_loc("close")] = c
    df.iloc[brk, df.columns.get_loc("high")] = c + WICK
    df.iloc[brk, df.columns.get_loc("low")] = 100.0 + breakout_low_offset
    return df, brk


def test_atr_expansion_filter_blocks_a_breakout_in_contracting_volatility():
    """A small Donchian up-breakout, above the EMA200, while ATR(14) is still
    BELOW its own trailing 20-bar mean (volatility contracting), must not be
    traded. Guards against the documented ATR-expansion filter being a no-op."""
    df, brk = _contraction_series(breakout_close_offset=0.8, breakout_low_offset=0.0)
    ind = add_indicators(df)
    assert bool(ind["breakout_long"].iat[brk])            # real channel breakout
    assert df["close"].iat[brk] > ind["ema"].iat[brk]     # trend filter passes
    assert ind["atr"].iat[brk] < ind["atr_mean"].iat[brk]  # but ATR is not expanding
    assert not bool(ind["long_signal"].iat[brk])
    assert simulate(df) == []


def test_atr_expansion_filter_control_wide_range_breakout_is_taken():
    """Control: the same contraction setup, but the breakout bar has a wide
    range that lifts ATR(14) above its trailing mean -- this one IS traded,
    so the block above is attributable to the expansion filter alone."""
    df, brk = _contraction_series(breakout_close_offset=8.0, breakout_low_offset=-0.5)
    ind = add_indicators(df)
    assert ind["atr"].iat[brk] > ind["atr_mean"].iat[brk]
    trades = simulate(df)
    assert len(trades) == 1
    assert trades[0].direction == "long" and trades[0].signal_time == df.index[brk]


# --------------------------------------------------------------------------- session / exits


def test_out_of_session_breakout_is_not_an_entry_and_one_trade_per_day():
    """A breakout at 05:00 UTC (outside 07:00-17:00) is ignored; of two
    in-session breakouts on the same day only the first is traded."""
    df = _bars(3, {_pos(2, 5): +5.0, _pos(2, 9): +5.0, _pos(2, 14): +5.0})
    trades = simulate(df)
    assert len(trades) == 1
    assert trades[0].signal_time == df.index[_pos(2, 9)]


def test_frozen_sl_tp_and_same_day_time_exit():
    """SL/TP are 1.5x / 2.5x ATR(14) at the signal bar, frozen; with the price
    going flat after the breakout neither is hit, and the trade is
    force-closed at the 16:45 bar's close (17:00), reason "time"."""
    brk = _pos(2, 10)
    df = _bars(3, {brk: +5.0}, amplitude=0.3)
    ind = add_indicators(df)
    trades = simulate(df)
    assert len(trades) == 1
    t = trades[0]
    atr = ind["atr"].iat[brk]
    assert t.atr_at_entry == atr
    assert t.stop_dist == GOLD_MOMENTUM_BASE.stop_atr_mult * atr
    assert np.isclose(t.stop_price, t.entry_price - 1.5 * atr)
    assert np.isclose(t.tp_price, t.entry_price + 2.5 * atr)
    assert t.exit_reason == "time"
    assert t.exit_time == pd.Timestamp("2024-01-04 17:00")
    assert t.exit_price == df["close"].iat[_pos(2, 16, 45)]


def test_same_bar_sl_and_tp_resolves_as_stop():
    """If one later bar spans both SL and TP, the conservative ordering books
    the stop, never the target."""
    brk = _pos(2, 10)
    df = _bars(3, {brk: +5.0}, amplitude=0.3)
    nxt = brk + 1
    df.iloc[nxt, df.columns.get_loc("high")] = 150.0
    df.iloc[nxt, df.columns.get_loc("low")] = 50.0
    trades = simulate(df)
    assert trades[0].exit_reason == "stop"
    assert trades[0].exit_time == df.index[nxt]
