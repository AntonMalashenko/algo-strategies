"""Daily bars, Gerchik ATR and causal D1 levels for S031.

Causality contract: everything returned for trading day index j is computed
from D1 bars with index < j only (the current day is never used). A swing
pivot at bar i becomes visible on day j only if its confirmation bar i+k
closed before day j started (i + k <= j - 1).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import D1_BOUNDARY_UTC_HOUR, STOP_MODE_ATR, STOP_MODE_PCT, GerchikConfig

MIN_M1_BARS_PER_DAY = 120          # drop holiday/weekend stubs from the D1 series
ROUND_STEPS_REL = (0.0025, 0.005, 0.01)  # not used for filtering; feature only


def trading_day_index(idx_utc: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Trading date of each UTC timestamp, FX convention: the day starts at 22:00 UTC."""
    shift = pd.Timedelta(hours=24 - D1_BOUNDARY_UTC_HOUR)
    return (idx_utc + shift).normalize()


def build_daily(m1: pd.DataFrame) -> pd.DataFrame:
    td = trading_day_index(m1.index)
    g = m1.groupby(td)
    d1 = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(),
        "low": g["low"].min(), "close": g["close"].last(), "n": g["close"].size(),
    })
    d1 = d1[d1["n"] >= MIN_M1_BARS_PER_DAY].drop(columns="n")
    d1.index.name = "day"
    return d1


def gerchik_atr(d1: pd.DataFrame, cfg: GerchikConfig) -> np.ndarray:
    """ATR known at the START of each day: mean range of the last `atr_days`
    previous non-paranormal D1 bars. Paranormal = range >= high*base or
    <= low*base, base = median of the last `atr_base_window` ranges."""
    rng = (d1["high"] - d1["low"]).to_numpy()
    out = np.full(len(rng), np.nan)
    for j in range(len(rng)):
        past = rng[max(0, j - 4 * cfg.atr_base_window):j]
        if len(past) < cfg.atr_days:
            continue
        base = np.median(past[-cfg.atr_base_window:])
        ok = past[(past < cfg.atr_paranormal_high * base) & (past > cfg.atr_paranormal_low * base)]
        if len(ok) < cfg.atr_days:
            continue
        out[j] = ok[-cfg.atr_days:].mean()
    return out


def stop_size(price: float, atr: float, cfg: GerchikConfig) -> float:
    if cfg.stop_mode == STOP_MODE_PCT:
        return price * cfg.stop_pct
    if cfg.stop_mode == STOP_MODE_ATR:
        return atr * cfg.stop_atr_fraction
    raise ValueError(cfg.stop_mode)


@dataclass(frozen=True)
class Level:
    price: float
    kind: str          # "H" (pivot high) or "L" (pivot low)
    pivot_day: int     # D1 index of the pivot bar
    touches: int       # later D1 bars whose high/low came within tolerance
    crosses: int       # D1 closes that crossed the level since the pivot
    mirror: bool       # crossed an odd number of times -> role reversed
    round_number: bool


def _pivots(d1: pd.DataFrame, atr: np.ndarray, cfg: GerchikConfig) -> list[tuple[int, str, float]]:
    h, lo = d1["high"].to_numpy(), d1["low"].to_numpy()
    k = cfg.pivot_k
    out = []
    for i in range(k, len(d1) - k):
        a = atr[i]
        if np.isnan(a):
            continue
        if h[i] >= h[i - k:i].max() and h[i] > h[i + 1:i + k + 1].max():
            if lo[i + 1:i + k + 1].min() <= h[i] - cfg.pivot_min_move_atr * a:
                out.append((i, "H", h[i]))
        if lo[i] <= lo[i - k:i].min() and lo[i] < lo[i + 1:i + k + 1].min():
            if h[i + 1:i + k + 1].max() >= lo[i] + cfg.pivot_min_move_atr * a:
                out.append((i, "L", lo[i]))
    return out


def _is_round(price: float, stop: float) -> bool:
    for step in ROUND_STEPS_REL:
        unit = 10 ** np.floor(np.log10(price * step))
        if abs(price / unit - round(price / unit)) * unit <= stop * 0.1:
            return True
    return False


def levels_by_day(d1: pd.DataFrame, atr: np.ndarray, cfg: GerchikConfig) -> list[list[Level]]:
    """For each D1 index j, the active level set known at the start of day j."""
    piv = _pivots(d1, atr, cfg)
    h, lo, c = d1["high"].to_numpy(), d1["low"].to_numpy(), d1["close"].to_numpy()
    k = cfg.pivot_k
    result: list[list[Level]] = []
    for j in range(len(d1)):
        a = atr[j]                      # ATR known at the start of day j (uses days < j)
        cand = [p for p in piv if p[0] + k <= j - 1 and p[0] >= j - cfg.level_lookback_days]
        if np.isnan(a) or not cand:
            result.append([])
            continue
        kept: list[Level] = []
        for i, kind, price in sorted(cand, key=lambda p: -p[0]):     # most recent first
            stp = stop_size(price, a, cfg)
            if any(abs(price - L.price) <= cfg.level_merge_frac * stp for L in kept):
                continue
            closes = c[i + 1:j]
            side0 = -1 if kind == "H" else 1
            sides = np.sign(closes - price)
            sides = sides[sides != 0]
            seq = np.concatenate([[side0], sides]) if len(sides) else np.array([side0])
            crosses = int((np.diff(seq) != 0).sum())
            if crosses > cfg.max_level_crosses:
                continue
            tol = cfg.touch_tol_frac * stp
            hh, ll = h[i + k + 1:j], lo[i + k + 1:j]
            touches = int(((np.abs(hh - price) <= tol) | (np.abs(ll - price) <= tol)).sum())
            kept.append(Level(price=float(price), kind=kind, pivot_day=i, touches=touches,
                              crosses=crosses, mirror=bool(crosses % 2 == 1),
                              round_number=_is_round(price, stp)))
        result.append(kept)
    return result
