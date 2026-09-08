"""S012 reserved-OOS opening — ONE final run, config fixed in advance.

Config selection rule (declared before looking): the same anchored-WF
procedure's choice trained on ALL pre-OOS data (this is what would have been
deployed on 2025-07-20). S009 side = its champion (lb7, taker 0.055%).
Decision metric: does 25/75 S012/S009 beat S009 alone on the OOS tail.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from strategies.xsect_momentum import XSectMomentumConfig, load_panels, run_backtest, MS_PER_DAY  # noqa: E402
from strategies.funding_carry import FundingCarryConfig, run_backtest as run_s009  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / 'data' / 'raw' / 'crypto_funding'
base = XSectMomentumConfig(taker_fee_per_side=0.00055)
close, funding = load_panels(DATA, base.universe)
idx = close.index
oos = int(pd.Timestamp(base.reserved_oos_start, tz='UTC').timestamp()*1000)//MS_PER_DAY

def sharpe(s):
    s = s[s != 0]
    return s.mean()/s.std(ddof=0)*np.sqrt(365) if len(s) > 30 and s.std(ddof=0) > 0 else float('nan')

def stats(s, label):
    s = s[s != 0]
    eq = (1+s).cumprod()
    dd = (eq/eq.cummax()-1).min()
    print(f"  {label:24s} days={len(s):4d} Sharpe={sharpe(s):+.2f} total={eq.iloc[-1]-1:+.1%} MaxDD={dd:.1%} hit={(s>0).mean()*100:.1f}%")

# 1. Pick S012 config on train only (same grid & rule as wf_xsect_momentum.py)
grid = [dict(lookback_days=lb, top_n=n, bottom_n=n, min_universe=2*n)
        for lb in [7, 14, 30, 60, 90] for n in [2, 3, 5]]
train = idx < oos
best = None
for g in grid:
    out, _ = run_backtest(close, funding, base.with_(**g))
    sh = sharpe(out['net_ret'][train])
    if best is None or sh > best[0]:
        best = (sh, g, out['net_ret'])
print(f"deployed config (chosen on train only): {best[1]}  train Sharpe={best[0]:+.2f}")

s12 = best[2][idx >= oos]
s9all, _ = run_s009(close, funding, FundingCarryConfig(taker_fee_per_side=0.00055, signal_lookback_days=7))
s9 = s9all['net_ret'][s9all.index >= oos]

print(f"\n=== RESERVED OOS {base.reserved_oos_start}..{pd.to_datetime(int(idx.max())*MS_PER_DAY, unit='ms', utc=True).date()} — final run ===")
stats(s12, 'S012 (deployed cfg)')
stats(s9, 'S009 champion lb7')
both = pd.concat([s12.rename('s12'), s9.rename('s9')], axis=1).dropna()
print(f"  corr(S012,S009) OOS = {both['s12'].corr(both['s9']):+.3f}")
stats(0.25*both['s12'] + 0.75*both['s9'], 'combo 25/75')
stats(0.50*both['s12'] + 0.50*both['s9'], 'combo 50/50')
