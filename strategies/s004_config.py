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

    # --- live sizing (ignored by the backtest engine, exported to the EA) ---
    risk_pct: float = RISK_PCT                  # % of equity per trade

    def __post_init__(self) -> None:
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
                    cost_inclusive_sizing=self.cost_inclusive_sizing)

    def with_(self, **overrides) -> "S004Config":
        return replace(self, **overrides)


S004_BASE = S004Config()        # frozen champion -- never edit in place

S004_INTRADAY = S004_BASE.with_(
    intraday_cutoff=INTRADAY_CUTOFF,
    cost_inclusive_sizing=True,
    max_trades_per_day=MAX_TRADES_PER_DAY,
)
