"""S009 E4 — anchored walk-forward + robustness. Never touches the reserved OOS tail."""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

REPO = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from strategies.funding_carry import FundingCarryConfig, load_panels, run_backtest, MS_PER_DAY

DATA = REPO / "data" / "raw" / "crypto_funding"
TAKER = 0.00055  # real Bybit taker per side


def sharpe(s):
    s = s[s != 0]
    return s.mean() / s.std(ddof=0) * np.sqrt(365) if len(s) > 30 and s.std(ddof=0) > 0 else -np.inf


def main():
    base = FundingCarryConfig(taker_fee_per_side=TAKER)
    close, funding = load_panels(DATA, base.universe)
    idx = close.index
    dt = pd.to_datetime(idx * MS_PER_DAY, unit="ms", utc=True)
    oos_day = int(pd.Timestamp(base.reserved_oos_start, tz="UTC").timestamp() * 1000) // MS_PER_DAY

    # parameter grid to choose from (honest: chosen only on past data)
    grid = []
    for lb in [1, 3, 5, 7, 10, 14]:
        for n in [2, 3, 5]:
            grid.append(dict(signal_lookback_days=lb, top_n=n, bottom_n=n, min_universe=2 * n))

    # precompute net_ret per config once (returns use only past; choice uses only train)
    series = {}
    for i, g in enumerate(grid):
        out, _ = run_backtest(close, funding, base.with_(**g))
        series[i] = out["net_ret"]

    test_years = [2022, 2023, 2024, 2025]
    stitched = []
    print("Anchored walk-forward (net taker 0.055%/side), config chosen on prior years:")
    print(f"{'test_yr':>7} {'chosen cfg':>22} {'test Sharpe':>11} {'test ret%':>9}")
    for Y in test_years:
        test_mask = (dt.year == Y) & (idx < oos_day)
        if test_mask.sum() == 0:
            continue
        first_test_day = idx[test_mask][0]
        train_mask = idx < first_test_day
        best = None
        for i, g in enumerate(grid):
            sh = sharpe(series[i][train_mask])
            if best is None or sh > best[0]:
                best = (sh, i, g)
        _, bi, bg = best
        tr = series[bi][test_mask]
        stitched.append(tr)
        cfgtxt = f"lb{bg['signal_lookback_days']} n{bg['top_n']}"
        print(f"{Y:>7} {cfgtxt:>22} {sharpe(tr):>11.2f} {((1+tr[tr!=0]).prod()-1)*100:>8.1f}%")

    wf = pd.concat(stitched)
    wf = wf[wf != 0]
    eq = (1 + wf).cumprod()
    dd = (eq / eq.cummax() - 1).min()
    yrs = pd.to_datetime(wf.index * MS_PER_DAY, unit="ms", utc=True).year
    allpos = all(((1 + wf[yrs == y]).prod() - 1) > 0 for y in sorted(set(yrs)))
    print(f"\nSTITCHED walk-forward (2022..2025-07, out-of-sample choice):")
    print(f"  days={len(wf)}  Sharpe={wf.mean()/wf.std(ddof=0)*np.sqrt(365):+.2f}  "
          f"CAGR={eq.iloc[-1]**(365/len(wf))-1:+.2%}  MaxDD={dd:.1%}  "
          f"total={eq.iloc[-1]-1:+.1%}  hit={ (wf>0).mean()*100:.1f}%  all_years_positive={allpos}")

    # robustness: full-IS Sharpe across the whole grid — plateau vs needle?
    print("\nRobustness — IS(<OOS) net Sharpe across grid (plateau check):")
    hdr = "lb\\n"
    print(f"{hdr:>5}" + "".join(f"{n:>8}" for n in [2, 3, 5]))
    is_mask = idx < oos_day
    for lb in [1, 3, 5, 7, 10, 14]:
        row = f"{lb:>5}"
        for n in [2, 3, 5]:
            out, _ = run_backtest(close, funding, base.with_(signal_lookback_days=lb, top_n=n, bottom_n=n, min_universe=2*n))
            row += f"{sharpe(out['net_ret'][is_mask]):>8.2f}"
        print(row)


if __name__ == "__main__":
    main()
