"""Run S012 cross-sectional crypto momentum: IS sweep + Gate 0 + S009 correlation.

Run from the algo repo root, with S009 data fetched:

    python backtest/run_xsect_momentum.py

Requires data/raw/crypto_funding/<SYM>/{funding,d1}.csv (scripts/fetch_funding_bybit.py).

Everything printed is IN-SAMPLE (before the reserved OOS boundary 2025-07-20,
shared with S009) — the OOS tail stays untouched until a promotion decision.
The primary research question (ALGODEV-13) is printed last: does the momentum
book have BOTH an edge and a low correlation to the S009 funding-carry book?
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from strategies.funding_carry import FundingCarryConfig  # noqa: E402
from strategies.funding_carry import run_backtest as run_s009  # noqa: E402
from strategies.xsect_momentum import (  # noqa: E402
    MS_PER_DAY, XSectMomentumConfig, load_panels, metrics, run_backtest,
)

DATA = REPO / "data" / "raw" / "crypto_funding"

# S009's cost-surviving champion (lb7 @ taker 0.055%/side) — the book S012
# must decorrelate from for the pairing to add value.
S009_CHAMPION = FundingCarryConfig(taker_fee_per_side=0.00055, signal_lookback_days=7)
REALISTIC_TAKER_FEE = 0.00055  # Bybit non-VIP taker per side, same as S009 runs


def per_year(ret: pd.Series, label: str) -> None:
    r = ret[ret != 0]
    if r.empty:
        print(f"  {label}: no traded days")
        return
    yrs = pd.to_datetime(r.index * MS_PER_DAY, unit="ms", utc=True).year
    print(f"  {label} — per year:")
    for y in sorted(set(yrs)):
        rr = r[yrs == y]
        sh = rr.mean() / rr.std(ddof=0) * np.sqrt(365) if rr.std(ddof=0) > 0 else float("nan")
        print(f"    {y}: days={len(rr):4d}  mean/day={rr.mean():+.5f}  Sharpe={sh:+.2f}  "
              f"year={((1 + rr).prod() - 1) * 100:+.1f}%")


def gate0(close, funding, cfg) -> None:
    print("\n=== Gate 0 no-look-ahead ===")
    cutoff = int(close.index.min()) + (int(close.index.max()) - int(close.index.min())) // 2
    full, _ = run_backtest(close, funding, cfg)
    tr, _ = run_backtest(close[close.index <= cutoff], funding[funding.index <= cutoff], cfg)
    common = [d for d in tr.index if d < cutoff and d in full.index]
    md = float(np.max(np.abs(full.loc[common, "net_ret"].values - tr.loc[common, "net_ret"].values))) if common else 0.0
    print(f"common past days={len(common)}  max|Δ net_ret|={md:.2e}  -> {'PASS' if md < 1e-12 else 'FAIL'}")


def main() -> None:
    base = XSectMomentumConfig()
    close, funding = load_panels(DATA, base.universe)
    oos = int(pd.Timestamp(base.reserved_oos_start, tz="UTC").timestamp() * 1000) // MS_PER_DAY
    is_mask = lambda out: out.index < oos  # noqa: E731
    print(f"panel: {close.shape[0]} days x {close.shape[1]} coins  "
          f"OOS reserved from {base.reserved_oos_start} (untouched)")

    print("\n=== Lookback/skip sweep — IS, GROSS (fee=0) ===")
    print(f"{'lb':>4} {'skip':>4} {'CAGR%':>8} {'Sharpe':>7} {'MaxDD%':>7} {'hit%':>5} {'turn/d':>6}")
    for lb in [7, 14, 30, 60, 90]:
        for skip in [0, 1, 7]:
            out, _ = run_backtest(close, funding, base.with_(lookback_days=lb, skip_days=skip))
            r = out["net_ret"][is_mask(out)]
            m = metrics(r, close)
            print(f"{lb:4d} {skip:4d} {m['CAGR']*100:8.2f} {m['Sharpe']:7.2f} "
                  f"{m['MaxDD']*100:7.1f} {m['hit_day']*100:5.1f} "
                  f"{out['turnover'][is_mask(out)].mean():6.2f}")

    print("\n=== Cost sensitivity — IS net, baseline lb30 skip0 ===")
    print(f"{'fee/side':>9} {'CAGR%':>8} {'Sharpe':>7} {'MaxDD%':>7}")
    for fee in [0.0, 0.00035, 0.00055, 0.0008]:
        out, _ = run_backtest(close, funding, base.with_(taker_fee_per_side=fee))
        m = metrics(out["net_ret"][is_mask(out)], close)
        print(f"{fee*100:8.3f}% {m['CAGR']*100:8.2f} {m['Sharpe']:7.2f} {m['MaxDD']*100:7.1f}")

    print("\n=== Primary hypothesis (ALGODEV-13): edge AND low corr vs S009 — IS ===")
    s009_out, _ = run_s009(close, funding, S009_CHAMPION)
    s009_r = s009_out["net_ret"][s009_out.index < oos]
    for lb in [14, 30, 60, 90]:
        out, _ = run_backtest(close, funding,
                              base.with_(lookback_days=lb, taker_fee_per_side=REALISTIC_TAKER_FEE))
        r = out["net_ret"][is_mask(out)]
        m = metrics(r, close)
        both = pd.concat([r.rename("s012"), s009_r.rename("s009")], axis=1).dropna()
        corr = both["s012"].corr(both["s009"])
        combo = metrics((0.5 * both["s012"] + 0.5 * both["s009"]))
        print(f"  lb{lb:<3d} net: Sharpe={m['Sharpe']:+.2f} CAGR={m['CAGR']*100:+.1f}%  "
              f"corr(S009)={corr:+.3f}  50/50 combo Sharpe={combo['Sharpe']:+.2f} "
              f"(S009 alone={metrics(s009_r)['Sharpe']:+.2f})")

    gate0(close, funding, base)
    print("\nNOTE: all numbers are IS; the reserved OOS tail (>=2025-07-20) has not "
          "been evaluated. Any lookback picked here must survive walk-forward + OOS.")


if __name__ == "__main__":
    main()
