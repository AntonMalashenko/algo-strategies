"""Run S009 funding-carry: baseline + cost sensitivity + Gate 0 + synthetic check.

Run from the algo repo root, with S009 data fetched:

    python backtest/run_funding_carry.py

Requires data/raw/crypto_funding/<SYM>/{funding,d1}.csv (scripts/fetch_funding_bybit.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from strategies.funding_carry import (  # noqa: E402
    FundingCarryConfig, load_panels, run_backtest, metrics, MS_PER_DAY,
)

DATA = REPO / "data" / "raw" / "crypto_funding"


def per_year(ret: pd.Series, label: str) -> None:
    r = ret[ret != 0]
    yrs = pd.to_datetime(r.index * MS_PER_DAY, unit="ms", utc=True).year
    print(f"  {label} — per year:")
    for y in sorted(set(yrs)):
        rr = r[yrs == y]
        sh = rr.mean() / rr.std(ddof=0) * np.sqrt(365) if rr.std(ddof=0) > 0 else float("nan")
        print(f"    {y}: days={len(rr):4d}  mean/day={rr.mean():+.5f}  Sharpe={sh:+.2f}  "
              f"year={((1 + rr).prod() - 1) * 100:+.1f}%")


def synthetic_check(cfg: FundingCarryConfig) -> None:
    print("\n=== SYNTHETIC funding-only check (flat prices → carry only) ===")
    days = np.arange(20000, 20200)
    syms = [f"C{i}" for i in range(10)]
    close = pd.DataFrame(100.0, index=days, columns=syms)
    fvals = np.linspace(-0.0003, 0.0006, len(syms))
    funding = pd.DataFrame({s: fvals[i] for i, s in enumerate(syms)}, index=days)
    out, _ = run_backtest(close, funding, cfg.with_(universe=tuple(syms)))
    g = out["gross_ret"]; g = g[g.index >= days[2]]
    exp = 0.5 * (np.mean(sorted(fvals)[-cfg.top_n:]) - np.mean(sorted(fvals)[:cfg.bottom_n]))
    print(f"gross/day mean={g.mean():+.6f}  expected 0.5*spread={exp:+.6f}  "
          f"match={abs(g.mean() - exp) < 1e-9}  all_positive={(g > 0).all()}")


def gate0(close, funding, cfg) -> None:
    print("\n=== Gate 0 no-look-ahead ===")
    cutoff = int(close.index.min()) + (int(close.index.max()) - int(close.index.min())) // 2
    full, _ = run_backtest(close, funding, cfg)
    tr, _ = run_backtest(close[close.index <= cutoff], funding[funding.index <= cutoff], cfg)
    common = [d for d in tr.index if d < cutoff and d in full.index]
    md = float(np.max(np.abs(full.loc[common, "net_ret"].values - tr.loc[common, "net_ret"].values))) if common else 0.0
    print(f"common past days={len(common)}  max|Δ net_ret|={md:.2e}  -> {'PASS' if md < 1e-12 else 'FAIL'}")


def main() -> None:
    base = FundingCarryConfig()
    close, funding = load_panels(DATA, base.universe)
    oos = int(pd.Timestamp(base.reserved_oos_start, tz="UTC").timestamp() * 1000) // MS_PER_DAY
    print(f"panel: {close.shape[0]} days x {close.shape[1]} coins  "
          f"OOS reserved from {base.reserved_oos_start}")

    print("\n=== Cost sensitivity (IS net, taker per side) — baseline lb1 top/bot3 ===")
    print(f"{'fee/side':>9} {'CAGR%':>7} {'Sharpe':>7} {'MaxDD%':>7} {'hit%':>5}")
    for fee in [0.0, 0.00035, 0.00055, 0.0008]:
        out, _ = run_backtest(close, funding, base.with_(taker_fee_per_side=fee))
        m = metrics(out["net_ret"][out.index < oos], close)
        print(f"{fee*100:8.3f}% {m['CAGR']*100:7.2f} {m['Sharpe']:7.2f} {m['MaxDD']*100:7.1f} {m['hit_day']*100:5.1f}")

    # Smoothed signal cuts turnover → survives real costs (chosen on IS; validate via walk-forward)
    champ = base.with_(taker_fee_per_side=0.00055, signal_lookback_days=7)
    out, _ = run_backtest(close, funding, champ)
    m = metrics(out["net_ret"][out.index < oos], close)
    print(f"\n=== lb7 @ taker 0.055%/side — IS net ===")
    print(f"CAGR={m['CAGR']*100:+.2f}%  Sharpe={m['Sharpe']:+.2f}  Sortino={m['Sortino']:+.2f}  "
          f"MaxDD={m['MaxDD']*100:.1f}%  hit={m['hit_day']*100:.1f}%  corrBTC={m.get('corr_BTC',float('nan')):+.3f}  "
          f"turn/day={out['turnover'][out.index<oos].mean():.2f}")
    per_year(out["net_ret"][out.index < oos], "lb7 net")

    gate0(close, funding, base)
    synthetic_check(base)
    print("\nNOTE: lb7 was selected on IS — believe it only after walk-forward + the reserved OOS tail.")


if __name__ == "__main__":
    main()
