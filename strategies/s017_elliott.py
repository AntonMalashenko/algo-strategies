"""S017 -- Elliott Wave rule-based strategy (ZigZag + EMA200 filter) on index CFDs.

VERDICT (2026-08-31, ALGODEV-27): TESTED and REJECTED at Gate 1 -- archived.
Gate 0 passed (non-repainting proven, max|delta| = 0 over 8 truncation points
on both TFs). Gate 1 failed on US500 M15-stitched data 2005..2025-06 (OOS tail
2025-07..2026-06 reserved and never opened): anchored walk-forward (grid:
zz_atr_mult x ema_period x wave3_fib_min x entry_mode, spread 0.4 pt) stitched
to +3.5R over 273 trades / 2 of 11 positive years on M30, and -22.3R / 3 of 11
positive years on H1. Best IS configs (e.g. zz=1.5 fib3=1.618 wave4, +83R on
M30) still had 7 positive vs 14 negative years -- a lonely peak, not a plateau.
Cross-check: NAS100 WF +16.6R / 5 of 11 years (weak, different best params),
DAX WF -11.7R / 6 of 11 years. Per-instrument "best" parameters disagree ->
classic overfit signature. The formalized wave rules carry no exploitable edge
here; landing-page numbers of the source product were not reproduced.

Independent re-implementation of the *idea class* behind ElliottWaveBot
(ctrader.com/products/669); marketing numbers on that landing page are NOT a
reference. The logic is a formalized heuristic, not "true" Elliott analysis:

1. Non-repainting ZigZag swing detector on CLOSED bars only. A pivot is a bar
   extreme (high for tops, low for bottoms); it becomes *confirmed* at the
   later bar whose close reverses from the extreme by >= ``zz_atr_mult`` x
   ATR(``atr_period``). Every pivot carries both its own bar index and the
   index of the bar that confirmed it, and all downstream pattern logic only
   ever sees pivots with ``confirm_idx <= now`` -- this is what Gate 0
   (no-look-ahead / no-repaint) asserts.
2. Impulse 1-2-3-4-5 markup over the last confirmed swings (long case, lows
   L0 L2 L4 / highs H1 H3 H5):
     - wave 2 does not break the start of wave 1 (L2 > L0);
     - wave 3 is not the shortest of waves 1/3/5;
     - wave 4 does not enter wave-1 territory (L4 > H1), no diagonal
       exceptions in this first iteration;
     - optional Fibonacci minimum for wave 3 vs wave 1 (``wave3_fib_min``).
3. A-B-C correction after the impulse: B does not exceed the impulse end
   (H7 <= H5), C extends beyond A (L8 < L6), C/A length ratio within
   [``c_of_a_min``, ``c_of_a_max``] (~1.0-1.618 heuristic), and the whole
   correction holds above the impulse origin (L8 > L0).
4. Entry modes (compared against each other, not stacked):
     - ``entry_mode="abc"``  -- enter on confirmation of C (start of a new
       impulse), 9-pivot pattern;
     - ``entry_mode="wave4"``-- enter on confirmation of wave 4 (betting on
       wave 5), 6-pivot pattern (wave-3 rule degrades to "3 not shorter
       than 1" because wave 5 is unknown yet).
5. EMA(``ema_period``) trend filter: longs only above, shorts only below.
6. Risk: SL behind the structural extreme (pivot that triggered the entry)
   minus ``sl_buffer_atr`` x ATR; TP = ``tp_fib`` x impulse height projected
   from the entry pivot. R-based accounting, full ``spread_pts`` charged per
   round trip. One position at a time; on a same-bar SL+TP touch the SL wins
   (conservative).

Shorts are the exact mirror. Everything tunable lives in the frozen
``S017Config`` dataclass; the engine itself has no magic numbers.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

# Pivot kinds (ZigZag alternates between them).
PIVOT_HIGH = "H"
PIVOT_LOW = "L"

# Number of alternating pivots each pattern needs, ending with the entry pivot.
ABC_PATTERN_PIVOTS = 9    # L0 H1 L2 H3 L4 H5 A B C (long case)
WAVE4_PATTERN_PIVOTS = 6  # L0 H1 L2 H3 L4 + preceding pivot context

LONG, SHORT = 1, -1


@dataclass(frozen=True)
class S017Config:
    """All S017 tunables. The engine is a pure state machine reading this."""

    # --- ZigZag swing detector ---
    zz_atr_mult: float = 3.0      # reversal threshold, in ATR multiples
    atr_period: int = 14          # ATR window (Wilder), bars of the trading TF

    # --- trend filter ---
    ema_period: int = 200         # EMA on trading-TF closes; long above, short below

    # --- impulse / correction rules ---
    wave3_fib_min: float = 0.0    # wave3 >= this x wave1 (0.0 disables the check)
    c_of_a_min: float = 1.0       # C/A length ratio lower bound
    c_of_a_max: float = 1.618     # C/A length ratio upper bound

    # --- entries / exits ---
    entry_mode: str = "abc"       # "abc" (end of correction) | "wave4" (bet on wave 5)
    tp_fib: float = 1.0           # TP = entry + tp_fib x impulse height (mirror for shorts)
    sl_buffer_atr: float = 0.0    # extra SL distance in ATR multiples beyond the pivot

    # --- costs ---
    spread_pts: float = 0.4       # round-trip spread, instrument points

    def with_(self, **kw) -> "S017Config":
        return dataclasses.replace(self, **kw)


# Frozen reference config for S017 experiments (NOT a validated baseline yet).
BASE_S017 = S017Config()


@dataclass(frozen=True)
class Pivot:
    idx: int          # bar index of the extreme itself
    price: float      # bar high (PIVOT_HIGH) or low (PIVOT_LOW)
    kind: str         # PIVOT_HIGH | PIVOT_LOW
    confirm_idx: int  # bar index where the pivot became known (>= idx)


def atr_series(df: pd.DataFrame, period: int) -> np.ndarray:
    """Wilder ATR on closed bars; value at i uses bars <= i only."""
    h, lo, c = df["high"].values, df["low"].values, df["close"].values
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - lo, np.maximum(np.abs(h - prev_c), np.abs(lo - prev_c)))
    return pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().values


def zigzag(df: pd.DataFrame, cfg: S017Config) -> List[Pivot]:
    """Non-repainting ZigZag: pivots confirmed by close reversal >= k x ATR.

    The candidate extreme is tracked on bar highs/lows; confirmation uses the
    ATR value of the *confirming* bar (known at confirmation time). A pivot's
    ``confirm_idx`` is the exact bar at which any online consumer would first
    learn about it -- truncating the future must never change pivots already
    confirmed (asserted by Gate 0).
    """
    highs, lows, closes = df["high"].values, df["low"].values, df["close"].values
    atr = atr_series(df, cfg.atr_period)
    n = len(df)
    pivots: List[Pivot] = []

    # Bootstrap: direction unknown; track both extremes until first reversal.
    max_i, min_i = 0, 0
    direction = 0  # +1 tracking a top candidate, -1 tracking a bottom candidate
    for i in range(1, n):
        thr = cfg.zz_atr_mult * atr[i]
        if direction == 0:
            if highs[i] >= highs[max_i]:
                max_i = i
            if lows[i] <= lows[min_i]:
                min_i = i
            if closes[i] <= highs[max_i] - thr and max_i >= min_i:
                pivots.append(Pivot(max_i, highs[max_i], PIVOT_HIGH, i))
                direction = -1
                min_i = int(np.argmin(lows[max_i:i + 1]) + max_i)
            elif closes[i] >= lows[min_i] + thr and min_i >= max_i:
                pivots.append(Pivot(min_i, lows[min_i], PIVOT_LOW, i))
                direction = 1
                max_i = int(np.argmax(highs[pivots[-1].idx:i + 1]) + pivots[-1].idx)
        elif direction == 1:
            if highs[i] >= highs[max_i]:
                max_i = i
            if closes[i] <= highs[max_i] - thr:
                pivots.append(Pivot(max_i, highs[max_i], PIVOT_HIGH, i))
                direction = -1
                min_i = int(np.argmin(lows[max_i:i + 1]) + max_i)
        else:  # direction == -1
            if lows[i] <= lows[min_i]:
                min_i = i
            if closes[i] >= lows[min_i] + thr:
                pivots.append(Pivot(min_i, lows[min_i], PIVOT_LOW, i))
                direction = 1
                max_i = int(np.argmax(highs[min_i:i + 1]) + min_i)
    return pivots


def _impulse_ok(p: List[Pivot], side: int, cfg: S017Config,
                need_wave5: bool) -> bool:
    """Check 1-2-3-4(-5) rules on 5 or 6 alternating pivots.

    ``p`` is [P0, P1, P2, P3, P4(, P5)] with P0 the impulse origin. For longs
    P0/P2/P4 are lows, P1/P3/P5 highs; shorts are the mirror (side=-1 flips
    the sign so the same inequalities apply).
    """
    v = [side * x.price for x in p]
    w1 = v[1] - v[0]
    w3 = v[3] - v[2]
    if w1 <= 0 or w3 <= 0:
        return False
    if v[2] <= v[0]:                 # wave 2 breaks wave-1 origin
        return False
    if v[4] <= v[1]:                 # wave 4 enters wave-1 territory
        return False
    if cfg.wave3_fib_min > 0 and w3 < cfg.wave3_fib_min * w1:
        return False
    if need_wave5:
        w5 = v[5] - v[4]
        if w5 <= 0:
            return False
        if w3 < w1 and w3 < w5:      # wave 3 the shortest of 1/3/5
            return False
    else:
        if w3 < w1:                  # degraded rule: 3 not shorter than 1
            return False
    return True


def _abc_ok(impulse_end: Pivot, a: Pivot, b: Pivot, c: Pivot,
            origin: Pivot, side: int, cfg: S017Config) -> bool:
    """A-B-C correction rules after a ``side`` impulse (long: A/C lows, B high)."""
    e, av, bv, cv, o = (side * impulse_end.price, side * a.price,
                        side * b.price, side * c.price, side * origin.price)
    len_a = e - av
    len_c = bv - cv
    if len_a <= 0 or len_c <= 0:
        return False
    if bv > e:                       # B exceeds the impulse end
        return False
    if cv >= av:                     # C does not extend beyond A
        return False
    if cv <= o:                      # correction swallows the whole impulse
        return False
    ratio = len_c / len_a
    return cfg.c_of_a_min <= ratio <= cfg.c_of_a_max


@dataclass
class Signal:
    side: int         # LONG | SHORT
    signal_i: int     # bar whose close confirmed the pattern
    sl: float
    tp: float
    struct_pivot: Pivot


def _pattern_signal(pivots: List[Pivot], i: int, ema: float, close: float,
                    atr_i: float, cfg: S017Config) -> Optional[Signal]:
    """Evaluate the configured pattern on pivots confirmed exactly at bar i."""
    if not pivots or pivots[-1].confirm_idx != i:
        return None
    last = pivots[-1]
    side = LONG if last.kind == PIVOT_LOW else SHORT
    # EMA200 trend filter.
    if side == LONG and close <= ema:
        return None
    if side == SHORT and close >= ema:
        return None

    if cfg.entry_mode == "abc":
        if len(pivots) < ABC_PATTERN_PIVOTS:
            return None
        p = pivots[-ABC_PATTERN_PIVOTS:]
        if not _impulse_ok(p[0:6], side, cfg, need_wave5=True):
            return None
        if not _abc_ok(p[5], p[6], p[7], p[8], p[0], side, cfg):
            return None
        impulse_height = abs(p[5].price - p[0].price)
        struct = p[8]
    elif cfg.entry_mode == "wave4":
        if len(pivots) < WAVE4_PATTERN_PIVOTS - 1:
            return None
        p = pivots[-(WAVE4_PATTERN_PIVOTS - 1):]   # P0..P4, entry on wave-4 pivot
        if not _impulse_ok(p, side, cfg, need_wave5=False):
            return None
        impulse_height = abs(p[3].price - p[0].price)  # waves 1-3 height
        struct = p[4]
    else:
        raise ValueError(f"unknown entry_mode: {cfg.entry_mode!r}")

    sl = struct.price - side * cfg.sl_buffer_atr * atr_i
    tp = struct.price + side * cfg.tp_fib * impulse_height
    return Signal(side=side, signal_i=i, sl=sl, tp=tp, struct_pivot=struct)


def run_backtest(df: pd.DataFrame, cfg: S017Config,
                 end_i: Optional[int] = None) -> pd.DataFrame:
    """Event-driven backtest on closed bars of the trading TF.

    Entry at next bar open after the signal bar; SL/TP checked intrabar with
    SL priority on a same-bar double touch. Returns one row per closed trade
    with R accounting net of ``spread_pts``.

    ``end_i`` (exclusive) lets Gate 0 re-run the engine on a truncated bar
    range without re-slicing the DataFrame (indices stay comparable).
    """
    n = len(df) if end_i is None else end_i
    opens, highs, lows, closes = (df["open"].values, df["high"].values,
                                  df["low"].values, df["close"].values)
    ema = df["close"].ewm(span=cfg.ema_period, adjust=False).mean().values
    atr = atr_series(df, cfg.atr_period)

    # Incremental zigzag would be ideal; recomputing per bar is O(n^2). Instead
    # compute once on the full slice [0, n) -- pivots' confirm_idx gives the
    # exact online availability, which Gate 0 independently verifies.
    pivots = zigzag(df.iloc[:n], cfg)
    by_confirm: dict[int, List[Pivot]] = {}
    for k, pv in enumerate(pivots):
        by_confirm[pv.confirm_idx] = pivots[: k + 1]

    trades = []
    pos = None          # dict(side, entry, sl, tp, entry_i)
    pending: Optional[Signal] = None

    for i in range(1, n):
        # 1) open pending position at this bar's open
        if pending is not None and pos is None:
            entry = opens[i]
            risk = pending.side * (entry - pending.sl)
            if risk > 0:
                pos = dict(side=pending.side, entry=entry, sl=pending.sl,
                           tp=pending.tp, entry_i=i, risk=risk,
                           signal_i=pending.signal_i)
            pending = None

        # 2) manage open position on this bar
        if pos is not None:
            s = pos["side"]
            hit_sl = lows[i] <= pos["sl"] if s == LONG else highs[i] >= pos["sl"]
            hit_tp = highs[i] >= pos["tp"] if s == LONG else lows[i] <= pos["tp"]
            exit_price = None
            reason = None
            if hit_sl:                       # SL priority on double touch
                exit_price, reason = pos["sl"], "sl"
            elif hit_tp:
                exit_price, reason = pos["tp"], "tp"
            if exit_price is not None:
                gross_r = s * (exit_price - pos["entry"]) / pos["risk"]
                net_r = gross_r - cfg.spread_pts / pos["risk"]
                trades.append(dict(
                    entry_time=df.index[pos["entry_i"]], exit_time=df.index[i],
                    side=s, entry=pos["entry"], exit=exit_price, sl=pos["sl"],
                    tp=pos["tp"], risk_pts=pos["risk"], reason=reason,
                    signal_i=pos["signal_i"], entry_i=pos["entry_i"], exit_i=i,
                    gross_r=gross_r, r=net_r))
                pos = None

        # 3) evaluate new signal on this closed bar (only when flat)
        if pos is None and pending is None and i in by_confirm:
            sig = _pattern_signal(by_confirm[i], i, ema[i], closes[i],
                                  atr[i], cfg)
            if sig is not None:
                # sanity: entry will be near close; require SL on the right side
                if sig.side * (closes[i] - sig.sl) > 0 and i + 1 < n:
                    pending = sig

    return pd.DataFrame(trades)
