"""Frozen config and presets for S004 -- H4 FVG bounce in the Asia session (FX).

The engine is `strategies/fvg_mtf.py` (shared with S016); this module is S004's
config home: the frozen champion parameters and the named presets built off them
with `.with_()`, per the strategy-modifiers discipline. `S004_BASE` is the frozen
champion as validated (passport S004: mode="base", stop="zone", RR 3.0, Asia
00:00-06:59 server time, the 7 core FX pairs, 0.9-pip round-trip cost) -- never
edit it in place; a variant is a new preset.

Presets
-------
S004_BASE      -- the frozen champion. All three prop fields below are off, so
                  `engine_kwargs()` reproduces the pre-ALGODEV-62 call exactly
                  (regression: backtest/run_s004_intraday.py `regression`
                  reproduces reports/s004_metalabel_dataset.csv trade for trade).
S004_INTRADAY  -- ALGODEV-62: the same frozen champion plus three prop rules,
                  fixed BEFORE looking at results (no sweep, nothing tuned), so a
                  FundingPips 2-Step $10k account can run it as the second leg
                  next to S021 (Nasdaq ORB):
                    1. intraday_cutoff   -- nothing is held past 22:45 server
                       (bar closes 23:00, before rollover): no overnights, no
                       weekends, so no gap risk and no swap.
                    2. cost_inclusive_sizing -- size on (stop + spread), so a
                       full stop is exactly -1R. This removes the tiny-stop
                       R-explosions (worst observed -10R on a 0.1-pip stop, see
                       backtest/s004_metalabel_data.py) that would otherwise
                       break a hard daily loss limit.
                    3. max_trades_per_day -- at most 2 entries per server day
                       across the WHOLE 7-pair portfolio. With rule 2 this caps
                       the planned worst day at exactly -2R, which at 1% risk per
                       trade leaves S021's 2% inside the firm's -5% daily limit.
                  Rules 1-2 are engine flags; rule 3 is portfolio-level and is
                  applied by backtest/run_s004_intraday.py, which is the only
                  place that sees all 7 pairs at once.
                  Amendment 2026-10-08 (maintainer's call, after the entry-shift
                  sensitivity below): the entry limit sits ENTRY_SHIFT_PIPS
                  toward the bounce, not exactly on the zone's edge.
S004_INTRADAY_NOSHIFT -- S004_INTRADAY exactly as first validated (limit parked on the
                  zone's near edge, entry_shift_pips=0). Kept so the numbers in
                  the 2026-10-06/07 research notes stay reproducible.
S004_INTRADAY_CAP1 -- modifier, DEFAULT OFF: the same -2% daily budget spent on
                  the first signal of the day only (1 x 2.00%). Better per
                  trade, worse per challenge -- the verdict and its numbers sit
                  on the preset itself.

The two position-sizing dials
-----------------------------
`max_trades_per_day` and `risk_pct` are deliberately kept separate and both are
EA inputs (mt5/tools/gen_params.py exports them as the MT5 defaults), because
they are what the account's survival is actually made of: with rule 2 the
planned worst day is exactly `max_trades_per_day * risk_pct` percent, and the
firm kills the account at -5% in a day. `DAILY_RISK_BUDGET_PCT` is the share of
that limit S004 may spend (the rest belongs to S021's Nasdaq leg), and the
product is checked against it here rather than in the EA, so an impossible pair
cannot even be exported.

backtest/run_s004_risk_grid.py measures the trade-off across the grid. The edge
of the budget is a cliff, not a slope: at cap 2 the expected number of blown
challenges in 3 years goes 3.0 (0.50%) -> 7.1 (1.00%) -> 32.8 (1.50%), because
1.50% is where `cap * risk + S021` first crosses -5% in a single day. The
default pair stays 2 x 1.00% (maintainer's call, 2026-10-07): it is the
validated preset, and it sits one step inside that cliff.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time

# ejtrader/histdata FX files store prices in MT points (point = pip/10), so one
# FX pip is always 10 raw price units regardless of symbol.
PIP_RAW = 10.0
SPREAD_PIPS = 0.9               # real measured round-trip cost, used throughout S004 validation
CORE_PAIRS = ("GBPJPY", "EURUSD", "USDCHF", "GBPUSD", "EURJPY", "USDJPY", "AUDUSD")
ASIA_HOURS = tuple(range(0, 7))  # 00:00-06:59 session clock, per passport S004 SS3

# The clock the M15 bar index -- and therefore every session hour above -- is
# stamped in: EET/EEST, switching on the EUROPEAN DST dates (UTC+2 winter,
# UTC+3 summer). This is NOT an assumption: mt5/tools/s004_clock_probe.py
# measures it from the data itself and shows the European morning sits at the
# same clock hour on both sides of the EU transition (so the clock moves with
# Europe) and on both sides of the US transition (so it does not move with the
# US). It holds for both data vintages: ejtrader 2012-2022 is the broker's own
# EET server time, and scripts/convert_histdata.py converts the 2022+ histdata
# files from fixed EST into Europe/Bucharest on purpose. ALGODEV-61 cost a live
# hour of drift per summer month by assuming a fixed offset -- a fixed offset is
# a bug here, never a shortcut.
SESSION_TZ = "Europe/Bucharest"

# S004-intraday prop rules (ALGODEV-62) -- see the module docstring.
INTRADAY_CUTOFF = time(22, 45)  # last M15 bar of the session day; it closes at 23:00
MAX_TRADES_PER_DAY = 2          # portfolio-wide, all 7 pairs together

# Entry limit offset, in pips, from the zone's near edge TOWARD the bounce.
#
# Why: the backtest fills on the first TOUCH of the edge. A real limit queue does
# not fill every touch, and the touches it misses are the ones that reverse at
# once -- the winners -- so touch-fills are optimistic in exactly the wrong
# direction. For a buy limit there may be a second, mechanical gap: MT5 bars are
# normally built from BID prices while a buy fills at the ASK, about the 0.9-pip
# spread above them. That is a hypothesis about this data feed, not measured.
#
# TESTED 2026-10-08 on S004_INTRADAY, 7 pairs, RR 3 (scratch scripts, then
# strategies/fvg_mtf.py::entry_shift_pips), 2022-03+ untouched window:
#   limit moved toward the bounce  0 / 0.2 / 0.5 / 1.0 / 2.0 pip
#     -> +0.164 / +0.168 / +0.176 / +0.151 / +0.148 R per trade, 5/5 years in plus
#        throughout: trades that only get NEAR the edge are as good as the rest,
#        so moving the limit costs almost nothing.
#   fill only if price trades THROUGH the edge by 0 / 0.2 / 0.5 / 1.0 / 2.0 pip
#     (the harsh queue model, fill price still the edge)
#     -> +0.164 / +0.149 / +0.128 / +0.065 / -0.013 R: the edge halves at 1 pip
#        and is gone at 2. That is what a limit parked EXACTLY on the edge risks.
# 1.0 pip was chosen by that argument -- it is about the spread -- and NOT as the
# peak of the table: the 0.5-pip bump is inside the noise, and picking it would
# be fitting. It costs 0.013 R per trade against the unshifted limit.
ENTRY_SHIFT_PIPS = 1.0
# Largest shift the EA accepts as an input: the top of the tested range. Beyond
# it the stop-to-entry distance grows enough to change the strategy, untested.
MAX_ENTRY_SHIFT_PIPS = 2.0

# Live-trading identity, consumed by the MT5 EA through mt5/tools/gen_params.py
# (the S021 equivalents live in bot/orb_config.py; S004 has no bot module).
MAGIC = "S004"                  # position-label prefix
RISK_PCT = 1.0                  # % of equity per trade (ALGODEV-62: 1% x 2 trades = -2% worst day)
# The share of the firm's -5% daily limit S004 may plan to lose; the remaining
# 3% is S021's 2% Nasdaq leg plus headroom for slippage on both. Any
# (max_trades_per_day, risk_pct) pair whose product exceeds this is refused.
DAILY_RISK_BUDGET_PCT = 2.0


@dataclass(frozen=True)
class S004Config:
    # --- frozen champion parameters (passport S004) ---
    mode: str = "base"              # entry mode, see fvg_mtf.run_backtest
    stop: str = "zone"              # stop behind the far edge of the H4 FVG zone
    rr: float = 3.0                 # take-profit distance in multiples of risk
    pip: float = PIP_RAW            # raw price units per pip
    spread_pips: float = SPREAD_PIPS
    entry_hours: tuple[int, ...] = ASIA_HOURS   # server-time hours an entry may be taken in
    pairs: tuple[str, ...] = CORE_PAIRS

    # --- S004-intraday prop rules (all off in S004_BASE) ---
    intraday_cutoff: time | None = None         # engine flag, rule 1
    cost_inclusive_sizing: bool = False         # engine flag, rule 2
    max_trades_per_day: int | None = None       # portfolio-level, rule 3

    # --- entry limit offset (off in S004_BASE; amendment 2026-10-08, see ENTRY_SHIFT_PIPS) ---
    entry_shift_pips: float = 0.0               # engine flag; EA input default

    # --- live sizing (ignored by the backtest engine, exported to the EA) ---
    risk_pct: float = RISK_PCT                  # % of equity per trade

    def __post_init__(self) -> None:
        if not 0.0 <= self.entry_shift_pips <= MAX_ENTRY_SHIFT_PIPS:
            raise ValueError(
                f"entry_shift_pips={self.entry_shift_pips} is outside the tested range "
                f"0..{MAX_ENTRY_SHIFT_PIPS} pips (see ENTRY_SHIFT_PIPS)")
        # Uncapped (S004_BASE) there is no planned worst day to check: that
        # preset is backtest-only and gen_params.py refuses to export it.
        if self.max_trades_per_day is None:
            return
        if self.worst_planned_day_pct() > DAILY_RISK_BUDGET_PCT:
            raise ValueError(
                f"max_trades_per_day={self.max_trades_per_day} x risk_pct={self.risk_pct}% plans "
                f"a {self.worst_planned_day_pct():.2f}% worst day, over S004's "
                f"{DAILY_RISK_BUDGET_PCT:.2f}% share of the firm's -5% daily limit. Lower one of "
                "the two dials, or raise DAILY_RISK_BUDGET_PCT only together with S021's leg.")

    def worst_planned_day_pct(self) -> float:
        """The worst day the prop rules allow, in % of equity.

        Exact, not an estimate: rule 2 (cost_inclusive_sizing) makes a full stop
        cost exactly -1R, so `max_trades_per_day` stops cost exactly that many
        times `risk_pct`. Uncapped (`S004_BASE`) there is no such bound.
        """
        if self.max_trades_per_day is None:
            return float("inf")
        return self.max_trades_per_day * self.risk_pct

    def engine_kwargs(self) -> dict:
        """Keyword arguments for strategies.fvg_mtf.run_backtest."""
        return dict(mode=self.mode, stop=self.stop, rr=self.rr, pip=self.pip,
                    spread_pips=self.spread_pips,
                    intraday_cutoff=self.intraday_cutoff,
                    cost_inclusive_sizing=self.cost_inclusive_sizing,
                    entry_shift_pips=self.entry_shift_pips)

    def with_(self, **overrides) -> "S004Config":
        return replace(self, **overrides)


S004_BASE = S004Config()        # frozen champion -- never edit in place

S004_INTRADAY_NOSHIFT = S004_BASE.with_(
    intraday_cutoff=INTRADAY_CUTOFF,
    cost_inclusive_sizing=True,
    max_trades_per_day=MAX_TRADES_PER_DAY,
)

S004_INTRADAY = S004_INTRADAY_NOSHIFT.with_(entry_shift_pips=ENTRY_SHIFT_PIPS)

# Modifier: spend the whole daily budget on the FIRST signal of the day instead
# of splitting it over the first two. Same planned worst day (-2%), different
# shape: 1 x 2.00% rather than 2 x 1.00%.
#
# TESTED 2026-10-06 (backtest/run_s004_daily_cap.py) and NOT PROMOTED. The edge
# is real and monotone -- the first signal of the day is the best one: cap 1
# beats cap 2 on R per trade in every year since 2019, out of sample +0.259 vs
# +0.164 R/trade, it survives a 1.15x stop stress, and it roughly doubles the
# prop emulator's median outcome. The price is diversification: one trade a day
# has no second trade to average with, so it busts the challenge about twice as
# often (13.7 vs 6.9 blown attempts per 3 years in the same emulator).
#
# Default-off because the comparison is a re-cap of one finished trade list, not
# a walk-forward: nothing here was re-fitted out of sample, so the gate in
# strategy-lifecycle is not met. Promote it only after a walk-forward, and only
# if the higher bust rate is acceptable for the account it would run on.
# Built off S004_INTRADAY_NOSHIFT: every number above was measured with the limit on the edge.
S004_INTRADAY_CAP1 = S004_INTRADAY_NOSHIFT.with_(max_trades_per_day=1, risk_pct=2.0)
