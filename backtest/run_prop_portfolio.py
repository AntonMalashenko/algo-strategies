"""Prop portfolio S021 (NAS100 ORB) + S004-intraday (H4 FVG, Asia, FX) under a 4%/day budget.

Requested 2026-10-06 (Anton): propose an intraday system with hard stops for prop
challenges, daily risk up to 4%, re-buy the challenge (~$100) after a bust, and compute
the economics on the prop emulator already used for S021.

Step `fx`  : S004-intraday = frozen S004 champion trades (reports/s004_metalabel_dataset.csv,
             base/zone/RR3, Asia 00-06 server, 7 core pairs, 0.9 pip round-trip) with three
             prop rules fixed BEFORE looking at results (no sweep):
               1. INTRADAY: a trade still open at FX_CUTOFF (22:45 server bar, closes 23:00,
                  i.e. before rollover) is closed at that bar's close (cost still charged).
               2. COST-INCLUSIVE SIZING: position sized on (stop + spread), so a full stop
                  loses exactly 1R (removes the -10R tiny-stop blowups, see
                  s004_metalabel_data.py note on risk_pips).
               3. DAILY CAP: only the first FX_MAX_TRADES_PER_DAY entries of a server day.
             Caveat: post-processing keeps the engine's one-position-per-pair blocking as
             in the original run (positions held overnight there still block later entries),
             so trade count is slightly understated -- conservative.
Step `prop`: 2-step challenge emulator (same state machine as run_s021_dax.ftmo_multiyear),
             parametrised rules (FTMO-like 10/5, FundingPips-like 8/5), $10k, fee $100 per
             attempt, block bootstrap 5d x N paths x 3 years + historical monthly-start replays.
             Day P/L % = S021 R x risk_nas + sum(S004 R') x risk_fx (pure R scaling).
             Optional phase-dependent risk (challenge vs funded) and a stop-slippage stress
             (every losing trade x SLIP_STRESS).

             SOURCE OF THE S004 LEG (changed 2026-10-06 evening, ALGODEV-62 phase A): `prop`
             now reads the ENGINE-produced trade list from backtest/run_s004_intraday.py, not
             the `fx` step's post-processed one. The three prop rules run inside the engine
             there, which both frees S004's one-position-per-pair lock the same evening and
             drops the shadow non-Asia trades the post-processing left in, so 2022-03+ carries
             1790 trades at +0.164 R/trade instead of 1291 at +0.120. `fx` is kept as the
             archived research path (it is the conservative lower bound), but its output is no
             longer what the emulator scales.

Run: python backtest/run_s004_intraday.py run   # produces the S004 leg
     python backtest/run_prop_portfolio.py fx   # archived research path, optional
     python backtest/run_prop_portfolio.py prop
"""
from __future__ import annotations
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backtest"))

REPORTS = ROOT / "reports"
S004_TRADES = REPORTS / "s004_metalabel_dataset.csv"
NAS_TRADES = REPORTS / "s021_nas_trades_for_combo.csv"
FX_INTRADAY = REPORTS / "s004_intraday_trades.csv"            # `fx` step output (archived research path)
FX_ENGINE_TRADES = REPORTS / "s004_intraday_portfolio_trades.csv"  # backtest/run_s004_intraday.py output

PIP_RAW = 10.0
SPREAD_PIPS = 0.9
FX_CUTOFF = pd.Timedelta(hours=22, minutes=45)      # last M15 bar before 23:00 server
FX_MAX_TRADES_PER_DAY = 2
TRUE_OOS_FROM = pd.Timestamp("2022-03-01")           # S004 E10 untouched window start


def fx():
    from s004_metalabel_data import load_combined
    t = pd.read_csv(S004_TRADES, parse_dates=["time_in", "time_out"])
    rows = []
    for sym, g in t.groupby("symbol"):
        m15 = load_combined(sym)
        close = m15["close"]
        for _, tr in g.iterrows():
            risk = abs(tr.entry - tr.sl)
            cost = SPREAD_PIPS * PIP_RAW
            cutoff_bar = tr.time_in.normalize() + FX_CUTOFF
            if tr.time_out > cutoff_bar + pd.Timedelta(minutes=15):
                px = close.loc[:cutoff_bar]
                exit_px = float(px.iloc[-1])
                pnl = (exit_px - tr.entry) * tr.dir - cost
                reason, t_out = "time", px.index[-1] + pd.Timedelta(minutes=15)
            else:
                pnl = tr.r * risk
                reason, t_out = tr.exit_reason, tr.time_out
            rows.append(dict(symbol=sym, time_in=tr.time_in, time_out=t_out,
                             r_orig=tr.r, r_intraday=pnl / risk, R=pnl / (risk + cost),
                             risk_pips=risk / PIP_RAW, exit_reason=reason))
    d = pd.DataFrame(rows).sort_values("time_in").reset_index(drop=True)
    d["day"] = d.time_in.dt.normalize()
    d["nth_in_day"] = d.groupby("day").cumcount() + 1
    d["taken"] = d.nth_in_day <= FX_MAX_TRADES_PER_DAY
    d.to_csv(FX_INTRADAY, index=False)

    def rep(x, col, label):
        y = x.groupby(x.day.dt.year)[col].sum()
        eq = x.groupby("day")[col].sum().cumsum()
        print(f"{label:44s} n={len(x):5d} R/trade={x[col].mean():+.4f} total={x[col].sum():+7.1f} "
              f"maxDD={(eq - eq.cummax()).min():+6.1f} years+={(y > 0).sum()}/{len(y)} "
              f"worst day={x.groupby('day')[col].sum().min():+.2f}")
        return y

    print("exit reasons after cutoff:", d.exit_reason.value_counts().to_dict())
    rep(d, "r_orig", "S004 champion as validated (r, swing hold)")
    rep(d, "r_intraday", "+ intraday exit 22:45")
    rep(d, "R", "+ cost-inclusive sizing")
    tk = d[d.taken]
    y = rep(tk, "R", f"+ max {FX_MAX_TRADES_PER_DAY}/day  = S004-intraday")
    print("   by year:", {int(k): round(v, 1) for k, v in y.items()})
    for lab, m in (("  pre-2022-03 (research window)", tk.time_in < TRUE_OOS_FROM),
                   ("  2022-03+ (true OOS window)", tk.time_in >= TRUE_OOS_FROM),
                   ("  2019-01..2026-06 (overlap w/ S021)", tk.time_in >= "2019-01-23")):
        rep(tk[m], "R", lab)
    for sym, g in tk[tk.time_in >= TRUE_OOS_FROM].groupby("symbol"):
        print(f"   OOS {sym}: n={len(g)} R/trade={g.R.mean():+.3f}")


# ---------------------------------------------------------------- prop emulator
BALANCE = 10000.0
CHALLENGE_FEE = 100.0
PAYOUT_CYCLE_DAYS, PAYOUT_SPLIT = 10, 0.80
YEARS = 3
TRADING_DAYS = 252 * YEARS
BLOCK_LEN, N_SIM, SEED = 5, 6000, 20261006
REPLAY_STEP_DAYS = 21                     # one historical replay per ~month of start dates
SLIP_STRESS = 1.15                        # stress: every losing trade loses 15% more
RULES = {  # phase-1 target %, phase-2 target %, daily limit %, static max loss %
    "FTMO-like 10/5": (10.0, 5.0, -5.0, 10.0),
    "FundingPips-like 8/5": (8.0, 5.0, -5.0, 10.0),
}
# (label, NAS risk %/trade in challenge, FX risk %/trade in challenge, NAS funded, FX funded)
CONFIGS = [
    ("S021 alone 2%",                 2.0, 0.0,  2.0, 0.0),
    ("S021 alone 4%",                 4.0, 0.0,  4.0, 0.0),
    ("S004i alone 0.5%x2",            0.0, 0.5,  0.0, 0.5),
    ("S004i alone 1%x2",              0.0, 1.0,  0.0, 1.0),
    ("S021 1% + S004i 0.5%x2 (2%)",   1.0, 0.5,  1.0, 0.5),
    ("S021 1.5% + S004i 0.5%x2 (2.5%)", 1.5, 0.5, 1.5, 0.5),
    ("S021 2% + S004i 0.5%x2 (3%)",   2.0, 0.5,  2.0, 0.5),
    ("S021 2% + S004i 1%x2 (4%)",     2.0, 1.0,  2.0, 1.0),
    ("S021 3% + S004i 0.5%x2 (4%)",   3.0, 0.5,  3.0, 0.5),
    ("chall 4% (2+1x2) / funded 2.5% (1.5+0.5x2)", 2.0, 1.0, 1.5, 0.5),
    ("chall 3% (2+0.5x2) / funded 2% (1+0.5x2)",   2.0, 0.5, 1.0, 0.5),
]


def _daily_R(stress: float = 1.0, start: str | None = None):
    tn = pd.read_csv(NAS_TRADES, parse_dates=["day"])
    if not FX_ENGINE_TRADES.exists():
        raise SystemExit(f"{FX_ENGINE_TRADES.name} missing -- run "
                         f"`python -m backtest.run_s004_intraday run` first")
    # the engine already reports R against (stop + spread), so its `r` IS the
    # emulator's R -- no post-processing left to do here, only the rename.
    tf = pd.read_csv(FX_ENGINE_TRADES, parse_dates=["day", "time_in"]).rename(columns={"r": "R"})
    tf = tf[tf.taken]
    for t in (tn, tf):
        t.loc[t.R < 0, "R"] *= stress
    nas = tn.groupby("day").R.sum()
    fxd = tf.groupby("day").R.sum()
    lo, hi = max(nas.index.min(), fxd.index.min()), min(nas.index.max(), fxd.index.max())
    if start is not None:                 # e.g. TRUE_OOS_FROM: only S004's untouched window
        lo = max(lo, pd.Timestamp(start))
    # calendar = weekdays in the overlap (both strategies can trade any weekday)
    cal = pd.bdate_range(lo, hi)
    return nas.reindex(cal, fill_value=0.0), fxd.reindex(cal, fill_value=0.0)


def emulate(paths_ch, paths_fu, rules):
    """paths_*: (n_days, n_sim) day % P/L under challenge / funded risk. Returns dict."""
    p1, p2, dlim, maxloss = rules
    n_days, n_sim = paths_ch.shape
    phase = np.ones(n_sim, dtype=int)
    eq = np.zeros(n_sim); last_pay = np.zeros(n_sim); since = np.zeros(n_sim, dtype=int)
    fees = np.full(n_sim, CHALLENGE_FEE); paid = np.zeros(n_sim)
    first_funded = np.full(n_sim, -1); ever = np.zeros(n_sim, dtype=bool)
    n_payouts = np.zeros(n_sim, dtype=int); funded_days = np.zeros(n_sim, dtype=int)
    cum = np.zeros((n_days, n_sim), dtype=np.float32)
    for day in range(n_days):
        today = np.where(phase == 3, paths_fu[day], paths_ch[day])
        eq = eq + today
        busted = (today <= dlim) | ((last_pay - eq) >= maxloss)
        target = np.where(phase == 1, p1, p2)
        passed = (eq >= target) & (phase != 3) & ~busted
        funded = phase == 3
        funded_days += funded
        since = np.where(funded, since + 1, since)
        do_pay = funded & (since >= PAYOUT_CYCLE_DAYS) & ~busted & (eq > last_pay)
        paid = np.where(do_pay, paid + (eq - last_pay) * PAYOUT_SPLIT / 100.0 * BALANCE, paid)
        n_payouts += do_pay
        last_pay = np.where(do_pay, eq, last_pay); since = np.where(do_pay, 0, since)
        fees = np.where(busted, fees + CHALLENGE_FEE, fees)
        phase = np.where(busted, 1, phase)
        eq = np.where(busted, 0.0, eq); last_pay = np.where(busted, 0.0, last_pay)
        since = np.where(busted, 0, since)
        to_f = passed & (phase == 2)
        first_funded = np.where(to_f & ~ever, day, first_funded); ever |= to_f
        phase = np.where(passed & (phase == 1), 2, phase); phase = np.where(to_f, 3, phase)
        eq = np.where(passed, 0.0, eq)
        last_pay = np.where(to_f, 0.0, last_pay); since = np.where(to_f, 0, since)
        cum[day] = paid - fees
    ff = first_funded[first_funded >= 0]
    return dict(attempts=float((fees / CHALLENGE_FEE).mean()),
                fees=float(fees.mean()),
                p_funded_6m=float(((first_funded >= 0) & (first_funded < 126)).mean()),
                p_funded_1y=float(((first_funded >= 0) & (first_funded < 252)).mean()),
                med_days_to_funded=float(np.median(ff)) if len(ff) else float("nan"),
                payouts=float(n_payouts.mean()), funded_share=float(funded_days.mean() / n_days),
                cum=cum)


def _boot_idx(rng, dn):
    nb = int(np.ceil(TRADING_DAYS / BLOCK_LEN))
    st = rng.integers(0, dn - BLOCK_LEN, size=(N_SIM, nb))
    idx = (st[:, :, None] + np.arange(BLOCK_LEN)[None, None, :]).reshape(N_SIM, -1)[:, :TRADING_DAYS]
    return idx.T


def prop(stress: float = 1.0, start: str | None = None):
    nas, fxd = _daily_R(stress, start)
    print(f"calendar {nas.index[0].date()}..{nas.index[-1].date()} ({len(nas)} weekdays); "
          f"daily R corr NAS vs FX = {nas.corr(fxd):+.3f} "
          f"(on days both traded: {nas[(nas != 0) & (fxd != 0)].corr(fxd[(nas != 0) & (fxd != 0)]):+.3f}); "
          f"stress x{stress}")
    a_nas, a_fx = nas.to_numpy(), fxd.to_numpy(); dn = len(a_nas)
    rows = []
    for rname, rules in RULES.items():
        idx = _boot_idx(np.random.default_rng(SEED), dn)
        starts = np.arange(0, dn - TRADING_DAYS, REPLAY_STEP_DAYS)
        ridx = (starts[None, :] + np.arange(TRADING_DAYS)[:, None])
        for label, nc, fc, nf, ff in CONFIGS:
            dch = a_nas * nc + a_fx * fc; dfu = a_nas * nf + a_fx * ff
            b = emulate(dch[idx], dfu[idx], rules)
            h = emulate(dch[ridx], dfu[ridx], rules)
            eqc = np.cumsum(dch)
            r = dict(rules=rname, config=label, worst_day=float(dch.min()),
                     hist_maxdd_pct=float((eqc - np.maximum.accumulate(eqc)).min()),
                     **{k: v for k, v in b.items() if k != "cum"})
            for yr in (1, 3):
                row = b["cum"][yr * 252 - 1]
                r[f"y{yr}_median"] = float(np.median(row)); r[f"y{yr}_mean"] = float(row.mean())
                r[f"y{yr}_p5"] = float(np.percentile(row, 5)); r[f"y{yr}_p95"] = float(np.percentile(row, 95))
            r["p_loss_3y"] = float((b["cum"][-1] < 0).mean())
            hc = h["cum"][-1]
            r.update(hist_n=len(starts), hist_3y_median=float(np.median(hc)), hist_3y_min=float(hc.min()),
                     hist_3y_max=float(hc.max()), hist_p_loss=float((hc < 0).mean()),
                     hist_attempts=h["attempts"])
            rows.append(r)
            print(f"{rname:20s} | {label:44s} worst {r['worst_day']:+5.2f}% histDD {r['hist_maxdd_pct']:+6.1f}% | "
                  f"att {r['attempts']:5.2f} funded<=6m {r['p_funded_6m']:4.0%} <=1y {r['p_funded_1y']:4.0%} "
                  f"| y1 med {r['y1_median']:+6.0f} | y3 med {r['y3_median']:+6.0f} mean {r['y3_mean']:+6.0f} "
                  f"[p5 {r['y3_p5']:+6.0f} p95 {r['y3_p95']:+6.0f}] P(loss) {r['p_loss_3y']:4.1%} "
                  f"| hist3y med {r['hist_3y_median']:+6.0f} min {r['hist_3y_min']:+6.0f} att {r['hist_attempts']:.1f}",
                  flush=True)
    tag = ("" if stress == 1.0 else f"_stress{stress}") + ("" if start is None else "_oos")
    pd.DataFrame(rows).to_csv(REPORTS / f"prop_portfolio_s021_s004{tag}.csv", index=False, float_format="%.4f")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "fx":
        fx()
    elif cmd == "prop":
        prop(float(sys.argv[2]) if len(sys.argv) > 2 else 1.0,
             str(TRUE_OOS_FROM.date()) if "oos" in sys.argv[3:] else None)
