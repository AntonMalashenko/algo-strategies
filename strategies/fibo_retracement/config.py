"""S020 -- Fibonacci retracement on multiple timeframes. Config.

STATUS (2026-09-18): baseline variant (a) "classic_swing" implementation for
the first backtest, per strategy-passport-S020.md section 4.1. Variant (b)
"mtf_confluence" is formalized in the passport but not implemented in this
module yet (no confluence.py) -- ALGODEV-41 scoped the research task to
formalization only; this module is the first code pass, at Anton's request
in a follow-up session (2026-09-18).

All tunables live here; the engine (engine.py) is a pure state machine that
reads this config, per code-architecture (no magic numbers in the engine).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass


@dataclass(frozen=True)
class FiboRetracementConfig:
    """Tunables for the classic-swing variant (S020 passport section 4.1)."""

    variant: str = "classic_swing"     # only variant implemented so far

    # --- timeframes ---
    higher_tf: str = "4h"              # impulse detection + trend filter TF
    entry_tf: str = "1h"               # entry TF (bar-by-bar zone touch)

    # --- non-repainting swing detector (reused pattern: strategies/s017_elliott.py) ---
    swing_atr_mult: float = 3.0        # ZigZag reversal threshold, ATR multiples
    atr_period: int = 14               # ATR window (Wilder), bars of higher_tf

    # --- trend filter ---
    trend_filter_mode: str = "ema200"  # "ema200" | "structure" | "none"
    ema_period: int = 200              # EMA on higher_tf closes -- see engine.py
                                        # docstring for why this diverges from the
                                        # passport's D1 suggestion in this first cut

    # --- retracement zone (passport 4.1: 38.2-61.8% is the base range) ---
    entry_level_min: float = 0.382     # near edge -- hit first retracing off the extreme
    entry_level_max: float = 0.618     # far edge -- deeper retracement (zone extent)
    stop_level: float = 0.786          # invalidation level beyond the far edge

    # --- take-profit: Fibonacci extension projected from the entry ---
    tp_mode: str = "projection_100"    # "projection_100" (fib=1.0) | "projection_1272" (fib=1.272)
                                        # | "rr_multiple" (see tp_rr below, Anton 2026-09-18)
    tp_rr: float = 1.0
    # Only read when tp_mode="rr_multiple": TP = entry +/- tp_rr * risk_distance
    # (risk_distance = |entry - stop|, i.e. the SAME distance the position's R
    # is measured against), instead of projecting off the impulse height. This
    # is a fixed-RR take-profit, independent of the Fibonacci projection --
    # Anton asked to check RR 1:1 and RR 1:1.5 (tp_rr=1.0 / 1.5) against the
    # existing ~1:2.48 implied by projection_100 (0.404*height risk / 1.0*height
    # reward = ~2.475, see strategy-passport-S020.md 4.1). Base preset's stop
    # (78.6%) and entry (38.2%) are UNCHANGED by this -- only the TP distance,
    # so RR is compared on equal footing (same win/loss definition, same
    # entries, same costs).

    # --- costs ---
    spread_pts: float = 0.9            # round-trip spread, instrument points; overridden
                                        # per-symbol from backtest/run_fvg.py::SPEC in the
                                        # runner (see run_s020_baseline.py docstring re: bps)

    # --- optional entry filter: FVG confluence (Anton, 2026-09-18) ---
    require_fvg_confluence: bool = False
    # Off by default -- base untouched. When True, a zone touch only arms a
    # pending entry if an active fair-value gap on entry_tf (matching
    # direction -- see fvg.py) overlaps the retracement zone
    # [entry_level_min, entry_level_max] at the moment of the touch.
    # TESTED 2026-09-18 (E2, FVG_CONFLUENCE_S020): blended net avgR barely
    # moves vs base (-0.1475R -> -0.1381R) but hides a large FX/index split
    # (FX/metal -0.11R -> -0.02R, index CFD -0.20R -> -0.30R) -- see
    # strategy-passport-S020.md section 9a. Refined further by the three
    # flags below (E3, same session, Anton's "не пустые FVG + свип").

    # --- FVG confluence refinement (Anton, 2026-09-18, "не брать пустые
    # фвг" + "пробивает свинг или индюсмент"): only meaningful when
    # require_fvg_confluence=True. Both default to the E2 behaviour
    # (unmitigated filter off, no sweep requirement) so FVG_CONFLUENCE_S020
    # stays byte-for-byte reproducible; new presets below turn these on. ---
    fvg_require_unmitigated: bool = False
    # Off by default (= E2 behaviour: the most recent gap counts even if
    # price has already fully closed back through it -- an "empty" gap).
    # When True, a gap that has been fully mitigated since forming no
    # longer counts as confluence (fvg.py's *_um arrays).
    fvg_sweep_mode: str | None = None
    # None (default, = E2 behaviour) | "swing" | "inducement" | "either".
    # When set, a gap only counts if it formed off a genuine liquidity
    # sweep of a prior swing ("swing") and/or a smaller-degree local
    # fractal ("inducement") level on entry_tf -- see sweep.py. "either"
    # accepts a sweep of either kind. An FVG with no such anchor ("в
    # воздухе") does not count -- the touch is skipped as if there were no
    # FVG at all, same as when require_fvg_confluence rejects a touch.
    fvg_sweep_swing_atr_mult: float = 1.5
    # ATR multiple for the entry_tf ZigZag used by fvg_sweep_mode="swing"/
    # "either" -- deliberately smaller than swing_atr_mult (3.0, used for
    # the H4 impulse pivots in zones.py): entry_tf structure is a smaller
    # degree by construction. Only read when fvg_sweep_mode needs "swing".
    fvg_sweep_inducement_fractal_bars: int = 2
    # Each-side bar count for the local fractal detector (sweep.py::
    # fractal_pivots) used by fvg_sweep_mode="inducement"/"either". Only
    # read when fvg_sweep_mode needs "inducement".
    fvg_sweep_lookback_bars: int = 3
    # How many entry_tf bars, ending at (and including) the gap's own
    # formation bar, are checked for the level-breaking wick. Only read
    # when fvg_sweep_mode is not None.
    fvg_sweep_max_age_bars: int = 50
    # Anton's "зеркальный уровень не супер старый": the swept pivot's
    # confirm_idx must be within this many entry_tf bars of the gap's
    # formation, or it's "too old" and doesn't qualify. Pragmatic default
    # (~2 days on H1, ~12.5h on M15), not itself swept as a free parameter
    # in E3 -- a natural follow-up axis if a sweep-mode preset shows
    # promise. Only read when fvg_sweep_mode is not None.

    # --- entry-session filter (Anton, 2026-09-18, "усиление": test FVG
    # entries restricted to specific session windows) ---
    session_windows: tuple[tuple[int, int], ...] | None = None
    # Each (start, end) is a half-open LOCAL-hour range [start, end) during
    # which a fill is permitted. Hour is read straight off the entry_tf
    # timestamp -- same convention strategies/fvg_mtf.py::session_of already
    # uses for this exact data pipeline (documented there as "broker/server
    # time, typically GMT+2/+3"); treated here as Anton's Kyiv-hour request
    # without adding a separate tz-conversion layer. None (default) = no
    # restriction, base/all prior presets untouched.
    session_skip_window: tuple[int, int] | None = None
    # A half-open [start, end) hour range in which a fill is ALWAYS skipped,
    # even inside an allowed session_windows entry -- models Anton's
    # session-boundary buffer: "если вход получается на 13-15, пропускаем и
    # ищем модель после 14". Only read when session_windows is not None.

    def with_(self, **kw) -> "FiboRetracementConfig":
        return dataclasses.replace(self, **kw)

    @property
    def tp_fib(self) -> float:
        return {"projection_100": 1.0, "projection_1272": 1.272}[self.tp_mode]


BASE_S020 = FiboRetracementConfig()

# FVG-confluence modifier preset -- Anton's "usilenie" idea (2026-09-18):
# require a fair-value gap sitting inside the retracement zone as extra
# confluence before taking the bounce. Base preset (BASE_S020) is untouched.
# E2 result: net avgR -0.1381R blended (vs -0.1475R base) -- see config field
# comment above and strategy-passport-S020.md 9a. Kept exactly as backtested,
# byte-for-byte reproducible; refinements below are new presets, not edits
# to this one.
FVG_CONFLUENCE_S020 = BASE_S020.with_(require_fvg_confluence=True)

# E3 refinements (Anton, same session, 2026-09-18): "не брать пустые фвг" +
# "пробивает не только свинг, но может быть просто пробитый индюсмент --
# тоже считается ... надо проверить статистику по свингу и индюсменту и
# вместе." Isolates the unmitigated-only fix, then layers each sweep
# requirement on top of it, so E3's presets are directly comparable to each
# other and to E2 above.
FVG_UNMITIGATED_S020 = FVG_CONFLUENCE_S020.with_(fvg_require_unmitigated=True)
FVG_SWEEP_SWING_S020 = FVG_UNMITIGATED_S020.with_(fvg_sweep_mode="swing")
FVG_SWEEP_INDUCEMENT_S020 = FVG_UNMITIGATED_S020.with_(fvg_sweep_mode="inducement")
FVG_SWEEP_EITHER_S020 = FVG_UNMITIGATED_S020.with_(fvg_sweep_mode="either")

# RR presets (Anton, 2026-09-18): "проверь РР 1-1 и 1-1.5" -- fixed-RR take
# profit instead of the 100%-projection default (~1:2.48 implied RR), tested
# both on the plain baseline and on the best modifier found so far
# (FVG_SWEEP_SWING_S020, E3) so the comparison is apples-to-apples on top of
# each. Entries/stops/costs unchanged -- only tp_mode/tp_rr differ from their
# respective parents.
RR1_BASE_S020 = BASE_S020.with_(tp_mode="rr_multiple", tp_rr=1.0)
RR15_BASE_S020 = BASE_S020.with_(tp_mode="rr_multiple", tp_rr=1.5)
RR1_SWEEP_SWING_S020 = FVG_SWEEP_SWING_S020.with_(tp_mode="rr_multiple", tp_rr=1.0)
RR15_SWEEP_SWING_S020 = FVG_SWEEP_SWING_S020.with_(tp_mode="rr_multiple", tp_rr=1.5)

# Session-window presets (Anton, 2026-09-18): "тест фвг с 10 до 13 по Киеву
# или с 14 до 18" -- two candidate entry-session windows, layered on top of
# the current best config (RR1_SWEEP_SWING_S020, E4) so the comparison is
# apples-to-apples against the reigning champion, not a fresh axis tested
# alone. session_skip_window=(13, 15) models the session-boundary buffer
# from the same request ("вход на 13-15 -- пропускаем").
SESSION_10_13_S020 = RR1_SWEEP_SWING_S020.with_(
    session_windows=((10, 13),), session_skip_window=(13, 15))
SESSION_14_18_S020 = RR1_SWEEP_SWING_S020.with_(
    session_windows=((14, 18),), session_skip_window=(13, 15))

# No-trend-filter preset (Anton, 2026-09-18): "попробуй убрать фильтр тренда,
# что по цифрам будет" -- layered on top of the current champion
# (RR1_SWEEP_SWING_S020) so the comparison is against the reigning best, not
# a fresh baseline. trend_filter_mode="none" already existed as a documented
# alternative in the config (see the field's own comment) but had never
# actually been backtested until this preset.
NO_TREND_SWEEP_SWING_S020 = RR1_SWEEP_SWING_S020.with_(trend_filter_mode="none")

# E7 -- finer entry_tf presets (Anton, 2026-09-19): originally asked for
# "1H-5M" (higher_tf=1h, entry_tf=5m). There is no native 5-minute data for
# the full 19-instrument universe/history -- data/raw/<SYM>m15.csv is M15,
# ~9-10 years per instrument; a "5m" resample off M15 would just be M15
# repeated across 3 empty sub-bars, not real 5-minute price action. Real
# M1 data exists only for a handful of FX pairs over ~2022-2026 (histdata/),
# no index CFDs. After AskUserQuestion flagged this, Anton chose to redo the
# idea as 4H-15M instead: entry_tf="15min" IS the data's native resolution
# (resample_ohlc(m15, "15min") is a verified no-op identity on the M15
# source -- 230,400/230,400 rows, max|diff|=0 on EURUSD), so this gets a
# genuinely finer, real entry cursor on the SAME full-history universe,
# unlike the abandoned 5M idea. higher_tf stays "4h" (unchanged).
#
# Layered on top of the current champion structural filter
# (FVG_SWEEP_SWING_S020, E3) for an apples-to-apples read, same as every
# prior axis this session (RR in E4, sessions in E5, trend in E6). NOT
# re-tuned for the new bar granularity: fvg_sweep_swing_atr_mult=1.5 and
# atr_period=14 now run the sweep-precondition's ZigZag/ATR over entry_tf
# 15-minute bars instead of 1-hour bars (a ~4x finer window on the same
# multiplier), and fvg_sweep_max_age_bars=50 is now ~12.5h of "recency"
# instead of ~2 days. This is a deliberate first-pass, as-is test of "same
# rule, finer time cursor" -- if it looks promising, re-tuning those three
# fields for the new granularity is the natural follow-up, not done here.
ENTRY15M_SWEEP_SWING_S020 = FVG_SWEEP_SWING_S020.with_(entry_tf="15min")

# RR sweep on top of the 15m-entry structural preset (Anton, 2026-09-19,
# "пробуй 1к1 1к1.5 и 1к2") -- same tp_rr values already tested at 1H entry
# in E4 (1.0, 1.5), plus a new 1:2 value not tested before at any entry_tf.
# Entries/stops/costs unchanged from ENTRY15M_SWEEP_SWING_S020 -- only
# tp_mode/tp_rr differ, so RR is compared on equal footing.
ENTRY15M_RR1_SWEEP_SWING_S020 = ENTRY15M_SWEEP_SWING_S020.with_(
    tp_mode="rr_multiple", tp_rr=1.0)
ENTRY15M_RR15_SWEEP_SWING_S020 = ENTRY15M_SWEEP_SWING_S020.with_(
    tp_mode="rr_multiple", tp_rr=1.5)
ENTRY15M_RR2_SWEEP_SWING_S020 = ENTRY15M_SWEEP_SWING_S020.with_(
    tp_mode="rr_multiple", tp_rr=2.0)

# E8 -- trend filter by price structure instead of EMA200 (Anton, 2026-09-19,
# "а если тренд по 4Н не по ема а по структуре?"). trend_filter_mode=
# "structure" reuses the SAME higher_tf ZigZag pivots already computed for
# the impulse/zone (swing.py) -- no new detector, no new tunable beyond the
# mode string itself. Long needs higher-high + higher-low on the last two
# same-kind pivot pairs; short the mirror (engine.py::_structure_trend_ok).
# Layered on top of the current champion (RR1_SWEEP_SWING_S020, E4) for an
# apples-to-apples read against ema200, same as every prior axis.
STRUCTURE_TREND_SWEEP_SWING_S020 = RR1_SWEEP_SWING_S020.with_(trend_filter_mode="structure")

# E9 -- entry depth: 50% / golden pocket instead of 38.2% (Anton, 2026-09-19,
# discretionary-modifier brainstorm, item #1 -- "смена глубины входа на
# 50%/золотой карман"). No engine change: entry_level_min/entry_level_max
# were already config fields (near/far edges of the retracement zone, see
# zones.py::build_zone) -- this only moves them. Touch still triggers on
# `near` only (engine.py step 3), so this literally changes "how deep the
# pullback must go before we even consider an entry" -- same idea a
# discretionary trader expresses as "I don't buy the shallow 38.2%, I want
# 50% or the golden pocket". stop_level (78.6%) and tp_rr (1.0) unchanged.
# Two sibling variants, both layered on the champion (RR1_SWEEP_SWING_S020,
# E4) for an apples-to-apples read, same pattern as E3's swing/inducement/
# either siblings.
ENTRY50_SWEEP_SWING_S020 = RR1_SWEEP_SWING_S020.with_(entry_level_min=0.5)
GOLDEN_POCKET_SWEEP_SWING_S020 = RR1_SWEEP_SWING_S020.with_(
    entry_level_min=0.618, entry_level_max=0.65)

