"""S009 hardening: cost regime, universe/survivorship robustness, return attribution.
Config is FROZEN (lb7, top/bot2) — so evaluating on OOS here is legitimate (no tuning)."""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

REPO = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from strategies.funding_carry import FundingCarryConfig, load_panels, run_backtest, metrics, MS_PER_DAY

DATA = REPO / "data" / "raw" / "crypto_funding"
FROZEN = dict(signal_lookback_days=7, top_n=2, bottom_n=2, min_universe=4)


def sh(s):
    s = s[s != 0]
    return s.mean() / s.std(ddof=0) * np.sqrt(365) if len(s) > 20 and s.std(ddof=0) > 0 else float("nan")


def split(r, oos):
    return r[r.index < oos], r[r.index >= oos]


def main():
    base = FundingCarryConfig(**FROZEN)
    close, funding = load_panels(DATA, base.universe)
    oos = int(pd.Timestamp(base.reserved_oos_start, tz="UTC").timestamp() * 1000) // MS_PER_DAY

    # ---- 1. Cost regime: maker vs taker + death threshold ----
    print("=== 1. Cost regime (frozen lb7 n2). fee/side: maker≈0.02%, taker≈0.055% ===")
    print(f"{'fee/side':>10} {'IS Sharpe':>10} {'IS CAGR%':>9} {'OOS Sharpe':>11} {'OOS CAGR%':>10}")
    for fee in [-0.0001, 0.0, 0.0001, 0.0002, 0.00035, 0.00055, 0.0008]:
        out, _ = run_backtest(close, funding, base.with_(taker_fee_per_side=fee))
        isr, oosr = split(out["net_ret"], oos)
        mi, mo = metrics(isr, close), metrics(oosr, close)
        print(f"{fee*100:9.3f}% {sh(isr):10.2f} {mi['CAGR']*100:9.1f} {sh(oosr):11.2f} {mo['CAGR']*100:10.1f}")
    # death threshold on IS (linear search)
    lo, hi = 0.0, 0.005
    for _ in range(40):
        mid = (lo + hi) / 2
        out, _ = run_backtest(close, funding, base.with_(taker_fee_per_side=mid))
        m = metrics(out["net_ret"][out.index < oos], close)
        if m["CAGR"] > 0: lo = mid
        else: hi = mid
    print(f"IS death threshold: net CAGR→0 at fee/side ≈ {lo*100:.3f}%  "
          f"(taker 0.055% → {0.00055/lo:.1f}x margin)")

    # ---- 2. Universe / survivorship robustness ----
    print("\n=== 2. Universe robustness (frozen lb7 n2 @ taker 0.055%) ===")
    starts = {s: int(close[s].first_valid_index()) for s in close.columns}
    cutoff_2021_07 = int(pd.Timestamp("2021-07-01", tz="UTC").timestamp() * 1000) // MS_PER_DAY
    early = tuple(s for s in close.columns if starts[s] <= cutoff_2021_07)
    majors = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT")
    majors = tuple(s for s in majors if s in close.columns)
    print(f"{'universe':>28} {'n':>3} {'IS Sharpe':>10} {'OOS Sharpe':>11}")
    for name, uni in [("full ~24", base.universe), (f"early cohort (≤2021-07)", early), ("majors 10", majors)]:
        cfg = base.with_(universe=uni, taker_fee_per_side=0.00055)
        c2, f2 = load_panels(DATA, uni)
        out, _ = run_backtest(c2, f2, cfg)
        isr, oosr = split(out["net_ret"], oos)
        print(f"{name:>28} {len(uni):>3} {sh(isr):10.2f} {sh(oosr):11.2f}")
    print("NOTE: truly delisted coins are NOT in the data (can't recover) — this probes")
    print("      dependence on the newest coins / expanding universe, not full survivorship.")

    # ---- 3. Attribution: carry (funding) vs reversal (price) ----
    print("\n=== 3. Attribution: carry (funding) vs price/reversal (frozen, gross pre-cost) ===")
    out, w = run_backtest(close, funding, base.with_(taker_fee_per_side=0.00055))
    price_ret = close.pct_change()
    carry = (w * (-funding)).fillna(0.0).sum(axis=1)      # pure funding harvested
    price = (w * price_ret).fillna(0.0).sum(axis=1)       # price move of positions (crowding fade)
    for label, mask in [("IS", out.index < oos), ("OOS", out.index >= oos)]:
        c, p = carry[mask], price[mask]
        tot = (c + p)
        cc, pp = c[c != 0], p[p != 0]
        print(f"{label}: carry mean/day={c.mean():+.5f} (Sharpe {sh(cc):+.2f}, cum {(c.sum())*100:+.0f}%)  |  "
              f"price mean/day={p.mean():+.5f} (Sharpe {sh(pp):+.2f}, cum {(p.sum())*100:+.0f}%)  |  "
              f"carry share={c.sum()/tot.sum()*100:.0f}%")


if __name__ == "__main__":
    main()
