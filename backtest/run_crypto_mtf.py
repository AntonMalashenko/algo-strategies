"""Run the S008 skeleton backtest on one symbol; IS metrics + Gate 0b check."""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from strategies.crypto_mtf.engine import run_backtest
from strategies.crypto_mtf.config import BASELINE_S008

DATA = REPO / "data" / "raw" / "crypto"


def load(sym, tf):
    df = pd.read_csv(DATA / sym / f"{tf}.csv")
    df["ts"] = df["ts"].astype("int64")
    return df[["ts", "open", "high", "low", "close", "volume"]].to_dict("records")


def metrics(trades, label):
    if not trades:
        print(f"{label}: no trades"); return
    r = np.array([t.r_net for t in trades])
    pnl = np.array([t.net_pnl for t in trades])
    wins = r > 0
    gw = pnl[pnl > 0].sum(); gl = -pnl[pnl < 0].sum()
    eq = np.cumsum(r); dd = (eq - np.maximum.accumulate(eq)).min()
    print(f"\n=== {label} ===")
    print(f"trades={len(trades)}  WR={wins.mean()*100:.1f}%  avgR={r.mean():+.4f}  "
          f"totR={r.sum():+.1f}  PF={gw/gl if gl>0 else float('inf'):.3f}")
    print(f"net PnL={pnl.sum():+.2f} USDT  maxDD={dd:.1f}R  "
          f"reasons={{tp:{sum(t.reason=='tp' for t in trades)}, "
          f"sl:{sum(t.reason=='sl' for t in trades)}, rev:{sum(t.reason=='reverse' for t in trades)}}}")
    yrs = sorted({t.year for t in trades})
    for y in yrs:
        ry = r[[t.year == y for t in trades]]
        print(f"   {y}: n={len(ry):4d}  avgR={ry.mean():+.4f}  totR={ry.sum():+.1f}")


def main():
    sym = sys.argv[1] if len(sys.argv) > 1 else "ETHUSDT"
    cfg = BASELINE_S008.with_(symbol=sym)
    m15 = load(sym, "m15"); h1 = load(sym, "h1"); h4 = load(sym, "h4"); d1 = load(sym, "d1")
    oos_ts = int(pd.Timestamp(cfg.reserved_oos_start, tz="UTC").timestamp() * 1000)

    print(f"[{sym}] bars: m15={len(m15)} h1={len(h1)} h4={len(h4)} d1={len(d1)}  "
          f"OOS reserved from {cfg.reserved_oos_start}")
    trades = run_backtest(m15, h1, h4, d1, cfg)
    is_trades = [t for t in trades if t.exit_ts < oos_ts]
    metrics(is_trades, f"{sym} IS (net, taker {cfg.taker_fee_per_side*100:.3f}%/side)")
    print(f"\n(OOS-хвост {sym}: {sum(t.exit_ts>=oos_ts for t in trades)} сделок — не оцениваем, зарезервировано)")

    # Gate 0b: no-look-ahead. Truncate at cutoff, trades closed before it must match.
    cutoff = int(pd.Timestamp("2024-01-01", tz="UTC").timestamp() * 1000)
    def trunc(bars): return [b for b in bars if b["ts"] <= cutoff]
    t_full = [t for t in trades if t.exit_ts <= cutoff - 8 * 3600_000]
    t_trunc_all = run_backtest(trunc(m15), trunc(h1), trunc(h4), trunc(d1), cfg)
    t_trunc = [t for t in t_trunc_all if t.exit_ts <= cutoff - 8 * 3600_000]
    ok = len(t_full) == len(t_trunc) and all(
        a.entry_ts == b.entry_ts and a.exit_ts == b.exit_ts and a.side == b.side
        and abs(a.entry - b.entry) < 1e-9 and abs(a.exit - b.exit) < 1e-9
        for a, b in zip(t_full, t_trunc))
    print(f"\nGate 0b no-look-ahead: full={len(t_full)} trunc={len(t_trunc)} "
          f"-> {'PASS' if ok else 'FAIL'}")


if __name__ == "__main__":
    main()
