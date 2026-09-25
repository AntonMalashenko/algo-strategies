"""Frozen strategy config for S021 -- ORB (opening range breakout) / intraday momentum.

Rules are frozen per claude/strategy-passport-S021.md sec 3 (Anton, 2026-09-21). This is
an independent from-scratch implementation, built by a Cowork session with no access to
Anton's original .../Trading/strategies/ORB-intraday-momentum/ code -- see passport sec 0
for why independent verification, not a review of the original, is the point of this
package. Do not tune these values here; a variant is a new preset via .with_(...)
(strategy-modifiers discipline) -- ORB_BASE itself must stay exactly what the passport
freezes.

session_open/session_close/entry_cutoff are clock readings against histdata's native
FIXED UTC-5 (EST) clock, not DST-converted -- see engine.py's module docstring for the
2026-09-21 investigation that confirmed this is the correct, canonical rule: it anchors
the opening range at the true NY 09:30 open in EST months and at true NY 10:30 in EDT
months (a deliberate seasonal-conditional anchor, not a timezone bug).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import time


@dataclass(frozen=True)
class OrbConfig:
    adr_window: int = 14                    # trading days of ADR lookback (past only, causal)
    k_range: float = 0.20                   # opening-range half-width = k_range * ADR14
    stop_adr_mult: float = 0.75             # stop distance = stop_adr_mult * ADR14 from entry
    session_open: time = time(9, 30)        # fixed-EST clock reading (NOT DST-converted -- see module docstring)
    session_close: time = time(15, 59)
    entry_cutoff: time = time(14, 29)       # last minute of the entry scan window
    min_session_bars: int = 350             # day invalid below this many M1 bars in-session
    max_gap_days_per_session: float = 2.5   # skip day if the ADR lookback window is this stretched
    cost_bps_roundtrip: float = 1.0         # round-trip cost, bps of entry price, charged once/trade

    def with_(self, **overrides) -> "OrbConfig":
        return replace(self, **overrides)


ORB_BASE = OrbConfig()  # frozen baseline -- matches passport sec 3 exactly, never edit in place
