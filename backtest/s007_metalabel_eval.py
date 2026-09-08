"""Meta-labeling evaluation for S007 (ALGODEV-14, P2).

Three questions, answered in order:

1. UNIT OF LEARNING -- add vs day. S007's pyramided positions share one common
   mid_range stop and one day-level TP, so the outcomes of adds within a day
   are close to deterministic given the day outcome. Section 1 quantifies this
   on the actual per-position lists: if within-day outcomes are near-perfectly
   dependent, labeling adds individually would only pseudo-replicate the same
   day (Lopez de Prado's non-IID label problem) and inflate every CV score.
   Expected conclusion: the DAY is the unit; the dataset builder already
   commits to that.

2. DOES A META-MODEL FILTER HELP? Expanding-window walk-forward by calendar
   year (train on all years < Y, predict year Y, Y in 2024/2025/2026 -- 2023
   is training-only). Models: logistic regression (scaled) and a small
   gradient-boosting classifier. The probability threshold is chosen ON TRAIN
   ONLY (the config never sees the test year), by maximizing train net R/day
   subject to keeping >= MIN_KEEP_FRAC of train days (a filter that discards
   most days is a different strategy, not a filter). Metrics per year and
   stitched OOS: net R/day, sum R, days kept, maxDD, worst day.

3. DOES IT REDUCE DRAWDOWN? (the ticket's separate question -- S007's profile
   is martingale-like, maxDD ~ -40R gross). Same walk-forward, but the
   threshold is chosen on train to minimize maxDD subject to retaining
   >= MIN_TRAIN_RETURN_FRAC of train sum R. Reported side by side.

Daily R aggregation is preserved throughout (day_R from the engine), so all
numbers are directly comparable to the S007 passport.

VERDICT (2026-08-31, ALGODEV-14): FAIL -- meta-labeling does NOT add value on
S007. On both presets (LIQFLOOR, NEWSSAFE), both models, both objectives, the
days the filter skips are on average PROFITABLE (+0.66..+1.10 R/day skipped),
so total R always drops (e.g. LIQFLOOR logit/net: +539.7R -> +435.0R) while
maxDD barely moves (-37.4 -> -36.7R) or even worsens (NEWSSAFE logit/net:
-29.3 -> -42.1R); the best DD-targeted result (-37.4 -> -32.2R) costs -111R of
profit and the worst single day is untouched (-8.87 -> -8.56R). The ticket's
drawdown question is answered NO: S007's martingale-like DD comes from the
intraday add path on chop days, which entry-time features do not predict.
Same-day features simply carry too little signal about how the London session
will unfold. No config flag added -- nothing to wire; scripts kept for reuse.

Usage: .venv/bin/python backtest/s007_metalabel_eval.py [--preset liqfloor|newssafe]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from backtest.s007_metalabel_data import (
    FEATURE_COLS, PRESETS, REAL_SPREAD_PER_SIDE, build_features, load_m1,
)
from strategies.ger40_lonfra import data as D
from strategies.ger40_lonfra.engine import run

TEST_YEARS = [2024, 2025, 2026]
THRESH_GRID = np.round(np.arange(0.30, 0.71, 0.02), 2)
MIN_KEEP_FRAC = 0.60          # a usable filter must keep >= 60% of train days
MIN_TRAIN_RETURN_FRAC = 0.80  # DD-mode: keep >= 80% of train sum R
SEED = 7                      # fixed for reproducibility of the GBM
GBM_PARAMS = dict(n_estimators=100, max_depth=2, learning_rate=0.05,
                  subsample=0.8, random_state=SEED)  # small on purpose: 680 rows


def max_dd(day_R: pd.Series) -> float:
    if len(day_R) == 0:
        return 0.0
    cum = day_R.cumsum().to_numpy()
    return float((cum - np.maximum.accumulate(cum)).min())


# ---------------------------------------------------------------- section 1
def within_day_dependence(preset: str) -> pd.DataFrame:
    """Quantify how dependent add outcomes are on the first position's outcome,
    using the engine's actual per-position lists (re-run, not the CSV)."""
    name, base_cfg = PRESETS[preset]
    cfg = base_cfg.with_(spread_per_side=REAL_SPREAD_PER_SIDE)
    df = load_m1()
    res = run(df, cfg, D.daily_levels(df))

    multi = res[res["n_pos"] > 1]
    same_sign, n_adds, statuses_equal = 0, 0, 0
    for _, row in multi.iterrows():
        pos = row["positions"]
        first_sign = np.sign(pos[0]["R"])
        for p in pos[1:]:
            n_adds += 1
            if np.sign(p["R"]) == first_sign:
                same_sign += 1
            if p["status"] == pos[0]["status"]:
                statuses_equal += 1
    print(f"\n=== Section 1: unit of learning ({name}) ===")
    print(f"traded days: {len(res)}, days with adds: {len(multi)} "
          f"({100 * len(multi) / len(res):.0f}%), add positions: {n_adds}")
    if n_adds:
        print(f"add R-sign equals first-position R-sign: {100 * same_sign / n_adds:.1f}%")
        print(f"add exit status equals first-position status: "
              f"{100 * statuses_equal / n_adds:.1f}%")
    print("=> adds share the day's common stop/TP; labels per add would be "
          "pseudo-replicates. Unit of learning = DAY.")
    return res


# ---------------------------------------------------------------- section 2+3
def _fit_predict(model_name: str, tr: pd.DataFrame, te: pd.DataFrame) -> np.ndarray:
    X_tr, y_tr = tr[FEATURE_COLS].to_numpy(), tr["win"].to_numpy()
    X_te = te[FEATURE_COLS].to_numpy()
    if model_name == "logit":
        m = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=1000, C=1.0))
    elif model_name == "gbm":
        m = GradientBoostingClassifier(**GBM_PARAMS)
    else:
        raise ValueError(model_name)
    m.fit(X_tr, y_tr)
    return m.predict_proba(X_te)[:, 1]


def _pick_threshold(tr: pd.DataFrame, p_tr: np.ndarray, mode: str) -> float:
    """Threshold chosen on TRAIN only. mode='net': max net R/day with
    >= MIN_KEEP_FRAC days kept. mode='dd': min maxDD with >= MIN_TRAIN_RETURN_FRAC
    of train sum R retained. Falls back to 'keep everything' when no threshold
    qualifies."""
    base_sum = tr["day_R"].sum()
    best_thr, best_score = 0.0, -np.inf
    for thr in THRESH_GRID:
        keep = p_tr >= thr
        kept = tr.loc[keep, "day_R"]
        if len(kept) == 0:
            continue
        if mode == "net":
            if keep.mean() < MIN_KEEP_FRAC:
                continue
            score = kept.mean()
        else:  # 'dd'
            if base_sum > 0 and kept.sum() < MIN_TRAIN_RETURN_FRAC * base_sum:
                continue
            score = -abs(max_dd(kept.sort_index()))
        if score > best_score:
            best_score, best_thr = score, thr
    return best_thr


def walk_forward(tbl: pd.DataFrame, model_name: str, mode: str) -> pd.DataFrame:
    tbl = tbl.sort_values("date").reset_index(drop=True)
    year = tbl["date"].dt.year
    rows = []
    for y in TEST_YEARS:
        tr, te = tbl[year < y], tbl[year == y]
        if len(te) == 0 or len(tr) < 100:
            continue
        p_tr = _fit_predict(model_name, tr, tr)
        thr = _pick_threshold(tr, p_tr, mode)
        p_te = _fit_predict(model_name, tr, te)
        keep = p_te >= thr
        te = te.assign(keep=keep)
        rows.append(te)
        kept = te[te["keep"]]
        print(f"  {y}: thr={thr:.2f} kept {len(kept)}/{len(te)} days | "
              f"unfiltered net {te['day_R'].mean():+.4f} sum {te['day_R'].sum():+7.1f}R "
              f"maxDD {max_dd(te['day_R']):6.1f}R | "
              f"filtered net {kept['day_R'].mean():+.4f} sum {kept['day_R'].sum():+7.1f}R "
              f"maxDD {max_dd(kept['day_R']):6.1f}R")
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def stitched_report(oos: pd.DataFrame, label: str) -> None:
    if oos.empty:
        return
    base, filt = oos, oos[oos["keep"]]
    skipped = oos[~oos["keep"]]
    print(f"  stitched OOS {label}: "
          f"unfiltered n={len(base)} net {base['day_R'].mean():+.4f} "
          f"sum {base['day_R'].sum():+7.1f}R maxDD {max_dd(base['day_R']):6.1f}R "
          f"worst {base['day_R'].min():+.2f}R")
    print(f"  {'':16s}  filtered   n={len(filt)} net {filt['day_R'].mean():+.4f} "
          f"sum {filt['day_R'].sum():+7.1f}R maxDD {max_dd(filt['day_R']):6.1f}R "
          f"worst {filt['day_R'].min():+.2f}R")
    if len(skipped):
        print(f"  {'':16s}  skipped days avg day_R {skipped['day_R'].mean():+.4f} "
              f"(sum {skipped['day_R'].sum():+.1f}R over {len(skipped)} days) "
              f"-- negative = the filter skipped bad days")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=sorted(PRESETS), default="liqfloor")
    args = ap.parse_args()

    res = within_day_dependence(args.preset)
    tbl = build_features(load_m1(), res, PRESETS[args.preset][1].trade_start)
    tbl["date"] = pd.to_datetime(tbl["date"].astype(str))
    n0 = len(tbl)
    tbl = tbl.dropna(subset=FEATURE_COLS).reset_index(drop=True)
    # NaNs only exist in the ATR(14)/SMA(20) warm-up days at the very start of
    # the sample (mid-2023, train-only) -- dropping them cannot touch a test year.
    print(f"\ndropped {n0 - len(tbl)} warm-up days with NaN daily context "
          f"(all before {tbl['date'].min().date()})")

    for mode, title in [("net", "Section 2: expectancy-targeted filter"),
                        ("dd", "Section 3: drawdown-targeted filter")]:
        print(f"\n=== {title} ({PRESETS[args.preset][0]}) ===")
        for model_name in ["logit", "gbm"]:
            print(f"[{model_name}] walk-forward (train < Y, test = Y, threshold on train):")
            oos = walk_forward(tbl, model_name, mode)
            stitched_report(oos, f"[{model_name}/{mode}]")
