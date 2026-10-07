"""S031 -- Gerchik level trading, course-literal formalization.

Source: A. Gerchik, "Trading from A to Z 3.0" (2023), text materials only
(lesson PDFs, lecture notes, student algorithms). Full rule extraction and the
list of contradictions between materials: Claude Project doc
`claude/research-2026-10-02-gerchik-formalization.md`.

Every tunable lives in `GerchikConfig`. Defaults reproduce the course rules as
literally as a machine can read them; where the course is ambiguous or the
materials contradict each other, the default follows the book/most student
algorithms and the alternative is a field (see the research doc, section 5).

Five entry models (`model` field), each backtested as an independent strategy:
  bounce   -- BSU/BPU1/BPU2 limit-player bounce off a D1 level (limit order)
  breakout -- buy/sell stop beyond a D1 level after compression ("podzhatie")
  fb1      -- false breakout by one bar (stop order back inside before bar close)
  fb2      -- false breakout by two bars
  fb3      -- complex false breakout, >= 3 bars held beyond the level
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# --- model identifiers -------------------------------------------------------
MODEL_BOUNCE = "bounce"
MODEL_BREAKOUT = "breakout"
MODEL_FB1 = "fb1"
MODEL_FB2 = "fb2"
MODEL_FB3 = "fb3"
ALL_MODELS = (MODEL_BOUNCE, MODEL_BREAKOUT, MODEL_FB1, MODEL_FB2, MODEL_FB3)

STOP_MODE_PCT = "pct_of_price"   # course "raschetny stop": 0.2% of level price
STOP_MODE_ATR = "atr_fraction"   # course 3.0: stop = 1/5..1/6 of daily ATR

TREND_MODE_GERCHIK = "gerchik"   # with local trend while ATR used < threshold, counter-trend after
TREND_MODE_NONE = "none"

# FX "trading day" boundary: 17:00 New York fixed-EST == 22:00 UTC (histdata clock).
D1_BOUNDARY_UTC_HOUR = 22


@dataclass(frozen=True)
class GerchikConfig:
    model: str = MODEL_FB2

    # --- daily ATR (lesson "Volatility and ATR") ---
    atr_days: int = 5                     # 3..5 previous D1 bars, current day excluded
    atr_paranormal_high: float = 2.0      # bars >= 2 ATR excluded (3.0 lecture notes say 1.5)
    atr_paranormal_low: float = 1.0 / 3.0  # bars <= 1/3 ATR excluded (3.0 notes say 0.5)
    atr_base_window: int = 10             # median of this many D1 ranges = reference for "paranormal"

    # --- stop / luft / target (lesson "Key trade parameters") ---
    stop_mode: str = STOP_MODE_PCT
    stop_pct: float = 0.002               # 0.2% of level price
    stop_atr_fraction: float = 1.0 / 6.0  # used when stop_mode == STOP_MODE_ATR
    luft_frac: float = 0.20               # luft = 20% of stop
    touch_tol_frac: float = 0.10          # "kopeyka v kopeyku" / "1-2 points" tolerance, as fraction of stop
    rr_target: float = 3.0                # take profit >= 3R
    tech_stop_max_ratio: float = 1.30     # technical stop allowed if <= calc stop * this (FB lessons: +30%)
    min_risk_stop_frac: float = 0.5       # NOT from the course: floor on a technical stop shorter than the
                                          # calc stop (course allows any shorter one); avoids R blow-ups on
                                          # near-zero risk, same artefact class as S004/S007 min-risk guards

    # --- D1 levels (lesson "Levels") ---
    level_lookback_days: int = 126        # ~6 months of D1, "all levels -- only daily, last 6 months"
    pivot_k: int = 3                      # swing extremum confirmed by k bars each side
    pivot_min_move_atr: float = 1.0       # "a level must have stopped/started a significant move"
    level_merge_frac: float = 0.5         # pivots closer than this * stop are one level
    max_level_crosses: int = 2            # D1 closes crossing the level more often -> "floating", dropped
    level_scan_atr: float = 3.0           # only levels within this many ATR of the day open are scanned

    # --- trade filters ---
    trend_mode: str = TREND_MODE_GERCHIK
    atr_used_threshold: float = 0.75      # 75% of daily ATR passed -> no with-trend trades
    min_room_stops: float = 4.0           # room to next D1 level in trade direction, in stops
    require_tech_atr: bool = True         # level-to-next-level distance must be >= daily ATR
    min_atr_in_stops: float = 0.0         # instrument filter "ATR >= 5 stops"; 0 = off (kills FX at 0.2% stop)

    # --- model-specific ---
    bounce_cancel_stops: float = 2.0      # unfilled limit cancelled when a bar closes >= 2 stops away
    bounce_skip_compression: bool = True  # no bounce while closes keep approaching the level
    compression_bars: int = 3             # closes strictly approaching the level over this many bars
    breakout_near_stops: float = 1.0      # breakout setup needs last close within this many stops of level
    breakout_small_bar_window: int = 20   # "small bars": mean range of compression bars <= mean of this window
    breakout_cancel_stops: float = 2.0
    fb_max_depth_atr: float = 1.0 / 3.0   # false-breakout depth beyond level <= 1/3 ATR
    fb1_arm_atr: float = 0.25             # fb1 watches the level nearest the bar open if within this * ATR
    fb3_min_bars: int = 3                # bars that must open AND close beyond the level after the breakout bar

    # --- session / risk ---
    skip_first_hour_bounce: bool = True   # "bounce is not traded in the first hour"
    no_entry_last_minutes: int = 60       # no new entries in the last hour of the session
    max_losses_per_day: int = 3           # 3 losers -> stop for the day (per instrument, per model)
    one_position_at_a_time: bool = True

    def with_(self, **kw) -> "GerchikConfig":
        return replace(self, **kw)


# --- presets -----------------------------------------------------------------
BASE_S031 = GerchikConfig()
STOP_ATR_S031 = BASE_S031.with_(stop_mode=STOP_MODE_ATR)
NO_TREND_S031 = BASE_S031.with_(trend_mode=TREND_MODE_NONE)


def preset_for(model: str, base: GerchikConfig = BASE_S031) -> GerchikConfig:
    if model not in ALL_MODELS:
        raise ValueError(f"unknown model {model!r}")
    return base.with_(model=model)


# --- instruments --------------------------------------------------------------
# spread: round-trip cost in native price units (same calibration family as
# backtest/run_fvg.py SPEC and S016/S020 runners; FX crosses widened).
# session: (IANA tz, start "HH:MM", end "HH:MM") local; FX/XAU use a London+NY
# UTC window since the course's exchange-session rules have no FX analogue.
FX_SESSION = ("UTC", "07:00", "20:00")

INSTRUMENTS: dict[str, dict] = {
    "EURUSD": dict(spread=0.00009, session=FX_SESSION),
    "GBPUSD": dict(spread=0.00012, session=FX_SESSION),
    "AUDUSD": dict(spread=0.00010, session=FX_SESSION),
    "USDCAD": dict(spread=0.00014, session=FX_SESSION),
    "USDCHF": dict(spread=0.00014, session=FX_SESSION),
    "USDJPY": dict(spread=0.010, session=FX_SESSION),
    "EURJPY": dict(spread=0.016, session=FX_SESSION),
    "GBPJPY": dict(spread=0.025, session=FX_SESSION),
    "AUDJPY": dict(spread=0.018, session=FX_SESSION),
    "EURGBP": dict(spread=0.00012, session=FX_SESSION),
    "EURCHF": dict(spread=0.00016, session=FX_SESSION),
    "XAUUSD": dict(spread=0.30, session=FX_SESSION),
    "GRXEUR": dict(spread=1.2, session=("Europe/Berlin", "09:00", "17:30")),   # DAX
    "FRXEUR": dict(spread=1.5, session=("Europe/Paris", "09:00", "17:30")),    # CAC40
    "UKXGBP": dict(spread=1.5, session=("Europe/London", "08:00", "16:30")),   # FTSE100
    "NSXUSD": dict(spread=1.0, session=("America/New_York", "09:30", "16:00")),  # NAS100
    "SPXUSD": dict(spread=0.5, session=("America/New_York", "09:30", "16:00")),  # S&P500
}
