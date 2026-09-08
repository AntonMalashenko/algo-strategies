"""Dataset builder for the S004 meta-labeling experiment (P1).

Builds one row per S004 champion-config trade (mode="base", stop="zone",
rr=3.0, Asia session 00:00-06:59 server time, core 7-pair universe) with a
small set of causal features computed strictly from information available
at entry time, plus the realized outcome (r, win).

Not a trading-decision change: reuses strategies.fvg_mtf.run_backtest
unmodified (only additive diagnostic columns were added to it -- zone_top/
zone_bot/zone_avail/ref -- see git diff, no control-flow change). This
script only shapes those outputs into an ML-ready table.

Usage: python3 backtest/s004_metalabel_data.py [--until YYYY-MM-DD]
  --until truncates the input M15 data before running the backtest, used by
  the Gate-0 no-look-ahead check (run_s004_metalabel_gate0.py).
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from strategies.fvg_mtf import run_backtest, resample_h4

ROOT = Path(__file__).resolve().parent.parent
CORE_PAIRS = ["GBPJPY", "EURUSD", "USDCHF", "GBPUSD", "EURJPY", "USDJPY", "AUDUSD"]
PIP_RAW = 10.0     # FX: 1 pip = 10 raw price units (data scaled by 1e5)
SPREAD_PIPS = 0.9  # real measured spread used throughout S004 validation
ASIA_HOURS = set(range(0, 7))   # 00:00-06:59 server time, per passport S004 SS3
TREND_MA_DAYS = 20              # matches the S016 E1 precedent for this engine
ATR_WINDOW = 14                 # standard ATR window, used for H4 and D1


def load_combined(sym: str, until: str | None = None) -> pd.DataFrame:
    """ejtrader 2012-2022 (`<sym>m15.csv`) + histdata 2022-2026 (`<sym>m15fresh.csv`),
    concatenated. The two sources overlap ~2022-01..2022-03; the fresh
    (histdata) rows win the overlap since E10 cross-validated them as the
    OOS source of record."""
    d1 = pd.read_csv(ROOT / "data" / "raw" / sym / f"{sym}m15.csv")
    d2 = pd.read_csv(ROOT / "data" / "raw" / sym / f"{sym}m15fresh.csv")
    d = pd.concat([d1, d2], ignore_index=True)
    d["Date"] = pd.to_datetime(d["Date"])
    d = d.set_index("Date").sort_index()
    d = d[~d.index.duplicated(keep="last")]
    for col in ["open", "high", "low", "close"]:
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d = d.rename(columns={"tick_volume": "volume"})
    d = d.dropna(subset=["close"])[["open", "high", "low", "close", "volume"]]
    if until is not None:
        d = d.loc[: pd.Timestamp(until)]
    return d


def atr(df: pd.DataFrame, window: int) -> pd.Series:
    """Standard ATR (simple mean of true range), causal (only past+current bar)."""
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def build_features(sym: str, m15: pd.DataFrame, trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    h4 = resample_h4(m15)
    h4_atr = atr(h4, ATR_WINDOW)
    # ATR known as of a completed H4 bar close; merge_asof backward on zone_avail
    # (zone_avail IS itself an H4 bar close time, so this picks that same bar's ATR).
    atr_tbl = pd.DataFrame({"time": h4_atr.index, "h4_atr": h4_atr.values}).dropna()

    daily = m15["close"].resample("1D").agg(["first", "max", "min", "last"])
    daily.columns = ["open", "high", "low", "close"]
    daily = daily.dropna(subset=["close"])
    d_atr = atr(daily, ATR_WINDOW)
    d_sma = daily["close"].rolling(TREND_MA_DAYS).mean()
    # Only the PREVIOUS completed day's stats are usable for a trade during
    # today (shift by 1 day) -- same causality convention as run_backtest's
    # own trend_ma_days feature.
    daily_feat = pd.DataFrame({
        "day": daily.index,
        "prev_atr_pct": (d_atr / daily["close"]).shift(1).values,
        "prev_trend_dir": np.sign(daily["close"] - d_sma).shift(1).values,
    }).dropna(subset=["day"])

    tr = trades.sort_values("time_in").reset_index(drop=True)

    tr = pd.merge_asof(tr.sort_values("zone_avail"), atr_tbl.sort_values("time"),
                       left_on="zone_avail", right_on="time", direction="backward")
    tr = tr.drop(columns=["time"])

    tr["day_key"] = tr["time_in"].dt.floor("D")
    tr = pd.merge_asof(tr.sort_values("day_key"), daily_feat.sort_values("day"),
                       left_on="day_key", right_on="day", direction="backward")
    tr = tr.drop(columns=["day", "day_key"]).sort_values("time_in").reset_index(drop=True)

    pip = PIP_RAW
    tr["zone_width_atr"] = (tr["zone_top"] - tr["zone_bot"]).abs() / tr["h4_atr"]
    tr["time_to_touch_bars"] = (tr["time_in"] - tr["zone_avail"]) / pd.Timedelta(minutes=15)
    tr["daily_vol_pct"] = tr["prev_atr_pct"]
    tr["trend_dir"] = tr["prev_trend_dir"].fillna(0.0)
    risk = (tr["entry"] - tr["sl"]).abs()
    room = (tr["ref"] - tr["entry"]) * tr["dir"]
    tr["dist_to_ref_r"] = (room / risk).where(tr["ref"].notna(), np.nan)
    tr["has_ref"] = tr["ref"].notna().astype(int)
    tr["dist_to_ref_r"] = tr["dist_to_ref_r"].fillna(0.0)
    tr["sweep_flag"] = tr["sweep"].astype(int)
    tr["risk_pips"] = risk / pip
    tr["symbol"] = sym
    tr["win"] = (tr["r"] > 0).astype(int)
    return tr


# risk_pips added 2026-08-27: a handful of champion-config trades land on a
# near-zero-pip stop (touch point almost coincides with the zone edge), which
# blows up |r| once the fixed spread cost is normalized by that tiny risk
# (worst case observed: -10R on a 0.1-pip stop, USDJPY 2020-03-16). This is
# the same failure mode independently found and fixed for S016's H4 branch
# (H4_MIN_RISK_PIPS, experiments-log E3, 2026-08-26). It affects only 13.4%
# of trades here (671/5018) and is far less severe than S016's case (76% of
# losses from 13% of trades there) -- NOT proposed as an S004 engine fix in
# this experiment (that would be a separate modifier decision, out of scope
# for meta-labeling and requiring Anton's sign-off per strategy-modifiers).
# Kept here as a candidate meta-labeling FEATURE instead: the model itself
# can learn to discount tiny-risk entries if that turns out to help.
FEATURE_COLS = ["zone_width_atr", "time_to_touch_bars", "hour", "daily_vol_pct",
                "trend_dir", "dist_to_ref_r", "has_ref", "sweep_flag", "dir",
                "risk_pips"]


def build_all(until: str | None = None) -> pd.DataFrame:
    all_tr = []
    for sym in CORE_PAIRS:
        m15 = load_combined(sym, until=until)
        tr = run_backtest(m15, mode="base", stop="zone", rr=3.0,
                          pip=PIP_RAW, spread_pips=SPREAD_PIPS)
        tr = tr[tr["hour"].isin(ASIA_HOURS)].copy()
        tr = build_features(sym, m15, tr)
        all_tr.append(tr)
        print(f"{sym}: {len(m15)} bars, {len(tr)} Asia-session base/zone/rr3 trades")
    out = pd.concat(all_tr, ignore_index=True)
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    df = build_all(until=args.until)
    out_path = Path(args.out) if args.out else ROOT / "reports" / "s004_metalabel_dataset.csv"
    df.to_csv(out_path, index=False)
    print(f"\nTotal: {len(df)} trades -> {out_path}")
    print(df[["r", "win"] + FEATURE_COLS].describe())
