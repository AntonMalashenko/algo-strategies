"""S020 variant (a) baseline runner: Gate 0 (no-look-ahead) + first IS numbers.

Reuses the already-calibrated 19-instrument universe and cost model from
S016/S004 (backtest/run_fvg.py::load_m15/SPEC/DEFAULT_SPEC) -- per
strategy-passport-S020.md section 7's recommendation, instead of
introducing a new bps cost model that has not been calibrated for these
instruments. (The passport's section 5 "cost_model: bps" field was a
forward-looking placeholder, not a committed choice; this first pass sticks
to the pip/points model this data pipeline already has real numbers for.)

Usage:
    python -m backtest.run_s020_baseline --gate0             # repaint proof only
    python -m backtest.run_s020_baseline --symbol EURUSD      # Gate 0 + baseline run
    python -m backtest.run_s020_baseline --all                # sweep the 19-instrument universe
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from backtest.run_fvg import DEFAULT_SPEC, SPEC, load_m15
from strategies.fibo_retracement.config import (
    BASE_S020, FVG_CONFLUENCE_S020, FVG_SWEEP_EITHER_S020, FVG_SWEEP_INDUCEMENT_S020,
    FVG_SWEEP_SWING_S020, FVG_UNMITIGATED_S020, RR1_BASE_S020, RR1_SWEEP_SWING_S020,
    RR15_BASE_S020, RR15_SWEEP_SWING_S020, SESSION_10_13_S020, SESSION_14_18_S020,
    NO_TREND_SWEEP_SWING_S020, ENTRY15M_SWEEP_SWING_S020, ENTRY15M_RR1_SWEEP_SWING_S020,
    ENTRY15M_RR15_SWEEP_SWING_S020, ENTRY15M_RR2_SWEEP_SWING_S020,
    STRUCTURE_TREND_SWEEP_SWING_S020, ENTRY50_SWEEP_SWING_S020,
    GOLDEN_POCKET_SWEEP_SWING_S020, FiboRetracementConfig)
from strategies.fibo_retracement.engine import resample_ohlc, run_backtest
from strategies.fibo_retracement.swing import get_swings

PRESETS = {
    "base": BASE_S020,
    "fvg_confluence": FVG_CONFLUENCE_S020,
    "fvg_unmitigated": FVG_UNMITIGATED_S020,
    "fvg_sweep_swing": FVG_SWEEP_SWING_S020,
    "fvg_sweep_inducement": FVG_SWEEP_INDUCEMENT_S020,
    "fvg_sweep_either": FVG_SWEEP_EITHER_S020,
    "rr1_base": RR1_BASE_S020,
    "rr15_base": RR15_BASE_S020,
    "rr1_sweep_swing": RR1_SWEEP_SWING_S020,
    "rr15_sweep_swing": RR15_SWEEP_SWING_S020,
    "session_10_13": SESSION_10_13_S020,
    "session_14_18": SESSION_14_18_S020,
    "no_trend_sweep_swing": NO_TREND_SWEEP_SWING_S020,
    "entry15m_sweep_swing": ENTRY15M_SWEEP_SWING_S020,
    "entry15m_rr1_sweep_swing": ENTRY15M_RR1_SWEEP_SWING_S020,
    "entry15m_rr15_sweep_swing": ENTRY15M_RR15_SWEEP_SWING_S020,
    "entry15m_rr2_sweep_swing": ENTRY15M_RR2_SWEEP_SWING_S020,
    "structure_trend_sweep_swing": STRUCTURE_TREND_SWEEP_SWING_S020,
    "entry50_sweep_swing": ENTRY50_SWEEP_SWING_S020,
    "golden_pocket_sweep_swing": GOLDEN_POCKET_SWEEP_SWING_S020,
}

# Same 19-instrument universe as S016 E2 (strategy-passport-S016.md 4):
# 12 FX/metal + 7 index CFD, all already calibrated in SPEC/DEFAULT_SPEC.
UNIVERSE = [
    "AUDJPY", "AUDUSD", "EURCHF", "EURGBP", "EURJPY", "EURUSD",
    "GBPJPY", "GBPUSD", "USDCAD", "USDCHF", "USDJPY", "XAUUSD",
    "DAX30M", "FR40M", "NAS100M", "SPX500M", "STOXX50M", "UK100M", "US2000M",
]

GATE0_CUTS = 8


def spec_for(sym: str) -> dict:
    return SPEC.get(sym, DEFAULT_SPEC)


def load_tfs(sym: str, cfg: FiboRetracementConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    m15 = load_m15(sym)
    higher = resample_ohlc(m15, cfg.higher_tf)
    entry = resample_ohlc(m15, cfg.entry_tf)
    return higher, entry


def gate0_repaint_proof(higher: pd.DataFrame, entry: pd.DataFrame,
                        cfg: FiboRetracementConfig) -> None:
    """Prove pivots and already-closed trades never change when the future
    is cut off -- same discipline as backtest/run_s017.py::gate0_repaint_proof."""
    n = len(entry)
    full_pivots = get_swings(higher, cfg.swing_atr_mult, cfg.atr_period)
    full_trades = run_backtest(higher, entry, cfg)
    cuts = np.linspace(n // 4, n - 1, GATE0_CUTS, dtype=int)
    for cut in cuts:
        cut = int(cut)
        part_higher = higher[higher.index <= entry.index[cut - 1]]
        part_pivots = get_swings(part_higher, cfg.swing_atr_mult, cfg.atr_period)
        want = [p for p in full_pivots if p.confirm_idx < len(part_higher)]
        assert want == part_pivots, (
            f"REPAINT at cut={cut}: confirmed pivots differ "
            f"({len(want)} vs {len(part_pivots)})")
        pt = run_backtest(higher, entry, cfg, end_i=cut)
        if len(full_trades) == 0:
            assert len(pt) == 0
            continue
        want_t = full_trades[full_trades["exit_i"] < cut].reset_index(drop=True)
        got_t = pt[pt["exit_i"] < cut].reset_index(drop=True)
        pd.testing.assert_frame_equal(want_t, got_t)
    print(f"Gate 0 OK: {GATE0_CUTS} truncation points, max|delta| = 0, "
          f"n_entry_bars={n}, n_trades_full={len(full_trades)}")


def stats(tr: pd.DataFrame) -> dict:
    if len(tr) == 0:
        return dict(n=0, wr=np.nan, avg_r=np.nan, total_r=np.nan, pf=np.nan)
    wins, losses = tr[tr["r"] > 0]["r"], tr[tr["r"] <= 0]["r"]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and losses.sum() != 0 else np.inf
    return dict(n=len(tr), wr=round((tr["r"] > 0).mean(), 3),
                avg_r=round(tr["r"].mean(), 4), total_r=round(tr["r"].sum(), 1),
                pf=round(pf, 2))


def run_one(sym: str, cfg: FiboRetracementConfig) -> dict:
    spec = spec_for(sym)
    higher, entry = load_tfs(sym, cfg)
    cfg2 = cfg.with_(spread_pts=spec["spread"])
    tr = run_backtest(higher, entry, cfg2)
    st = stats(tr)
    return dict(symbol=sym, **st)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--preset", default="base", choices=sorted(PRESETS),
                    help="which named preset to run (base | fvg_confluence)")
    ap.add_argument("--gate0", action="store_true", help="repaint proof only")
    ap.add_argument("--all", action="store_true", help="sweep the 19-instrument universe")
    args = ap.parse_args()

    cfg = PRESETS[args.preset]
    if args.all:
        rows = [run_one(sym, cfg) for sym in UNIVERSE]
        table = pd.DataFrame(rows)
        pd.set_option("display.width", 200)
        print(table.to_string(index=False))
        n_w = table["n"].sum()
        avg_w = (table["avg_r"] * table["n"]).sum() / n_w if n_w else float("nan")
        print(f"\ntrade-weighted avg_r across universe: {avg_w:.4f}  (n={n_w})")
        table.to_csv(f"reports/s020_{args.preset}_sweep.csv", index=False)
        return

    higher, entry = load_tfs(args.symbol, cfg)
    spec = spec_for(args.symbol)
    cfg = cfg.with_(spread_pts=spec["spread"])
    print(f"{args.symbol} {cfg.higher_tf}: {len(higher)} bars, "
          f"{cfg.entry_tf}: {len(entry)} bars, spread={spec['spread']}, preset={args.preset}")
    gate0_repaint_proof(higher, entry, cfg)
    if args.gate0:
        return
    tr = run_backtest(higher, entry, cfg)
    print(f"\n{args.symbol}: {stats(tr)}")


if __name__ == "__main__":
    main()
