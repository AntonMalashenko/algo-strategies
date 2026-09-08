"""Meta-labeling evaluation for S004 (research-2026-08-06 A1, roadmap P1).

Question: does a secondary classifier, trained on entry-time-only features
to predict whether an S004 champion-config trade (mode="base", stop="zone",
rr=3.0, Asia session, core 7-pair universe) wins or loses, add value as a
FILTER on top of the already-validated primary signal? Per project
convention the decision metric is net R/trade (and total R, since a filter
that raises average R by dropping many trades can still lose on total R),
not accuracy/AUC -- the primary signal already decides direction, the
model only decides whether to take this specific instance of it.

Two independent checks, in this order:

1. PURGED K-FOLD (Lopez de Prado ch.7), pooled over 2012-2025 (2026 held
   out, see below): out-of-fold win probabilities for every trade, with an
   embargo >= the single longest trade's holding period, so no fold's test
   trades can leak into a temporally-adjacent fold's training set. S004
   trades overlap in wall-clock time (multiple pairs trade concurrently,
   holding up to hours -- unlike S007's day-level aggregation in P2, where
   a naive per-day split is already non-overlapping), so this is the
   necessary Gate-0-equivalent for the meta-model itself, not just the
   features (features already passed their own Gate 0 in
   s004_metalabel_gate0.py). Reports several fixed thresholds side by
   side (not the single best one -- picking only the best of many would
   repeat the multiple-comparisons mistake caught in S016 E4) with a
   bootstrap CI on the R/trade delta (filtered - unfiltered).

2. ANCHORED WALK-FORWARD BY YEAR, mirroring the S004 E9 / S007 P2
   convention: train on all years < Y, predict year Y, threshold chosen
   on TRAIN ONLY (grid search maximizing train avgR, same rule as P2).
   Y ranges 2016..2025. **2026 is intentionally never a test year here** --
   S004's true OOS (2022-2026) is already spent validating the base engine
   (passport SS4); this filter is judged on walk-forward only, and 2026
   stays untouched as a still-fresh point for a later decision.

Usage: .venv/bin/python -m backtest.s004_metalabel_eval [--model logit|gbm]
  (default: runs both models)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from backtest.s004_metalabel_data import FEATURE_COLS

ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT / "reports" / "s004_metalabel_dataset.csv"

HOLDOUT_YEAR = 2026            # true OOS tail already spent on the base engine (passport SS4) -- untouched here
WF_TEST_YEARS = list(range(2016, 2026))  # anchored walk-forward test years
N_SPLITS_CV = 8
CV_THRESHOLDS = [0.35, 0.40, 0.45, 0.50, 0.55]  # reported side by side, not cherry-picked (S016 E4 lesson)
THRESH_GRID = np.round(np.arange(0.30, 0.81, 0.02), 2)  # walk-forward: searched on TRAIN only
MIN_KEEP_FRAC = 0.60            # a filter dropping >40% of trades is a different strategy, not a filter
N_BOOT = 5000
SEED = 7
GBM_PARAMS = dict(n_estimators=100, max_depth=3, learning_rate=0.05,
                   subsample=0.8, random_state=SEED)  # small on purpose: <5k rows, 10 features


def load_dataset() -> pd.DataFrame:
    raw = pd.read_csv(DATASET_PATH, parse_dates=["time_in", "time_out"])
    df = raw.dropna(subset=FEATURE_COLS).reset_index(drop=True)
    print(f"loaded {len(raw)} trades, dropped {len(raw) - len(df)} with NaN warm-up "
          f"features -> {len(df)} usable, {df['time_in'].min().date()}..{df['time_in'].max().date()}")
    return df.sort_values("time_in").reset_index(drop=True)


def fit_predict(model_name: str, tr: pd.DataFrame, te: pd.DataFrame) -> np.ndarray:
    X_tr, y_tr = tr[FEATURE_COLS].to_numpy(), tr["win"].to_numpy()
    X_te = te[FEATURE_COLS].to_numpy()
    if model_name == "logit":
        m = make_pipeline(StandardScaler(),
                          LogisticRegression(max_iter=1000, C=1.0, class_weight="balanced"))
    elif model_name == "gbm":
        m = GradientBoostingClassifier(**GBM_PARAMS)
    else:
        raise ValueError(model_name)
    m.fit(X_tr, y_tr)
    return m.predict_proba(X_te)[:, 1]


def bootstrap_ci(all_r: np.ndarray, keep: np.ndarray, n_boot: int, seed: int) -> tuple[float, float, float]:
    """95% CI on mean(r[keep]) - mean(r) via paired bootstrap resampling of
    trade indices (with replacement); NaN draws (a resample with zero kept
    trades) are dropped rather than treated as zero."""
    rng = np.random.default_rng(seed)
    n = len(all_r)
    deltas = np.empty(n_boot)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        k = keep[idx]
        deltas[b] = (all_r[idx][k].mean() if k.any() else np.nan) - all_r[idx].mean()
    deltas = deltas[~np.isnan(deltas)]
    return float(np.mean(deltas)), float(np.percentile(deltas, 2.5)), float(np.percentile(deltas, 97.5))


# ------------------------------------------------------------- purged k-fold
def purged_kfold_oof(df: pd.DataFrame, model_name: str, n_splits: int, embargo: pd.Timedelta) -> np.ndarray:
    """Out-of-fold win-probability for every trade. Any train trade whose
    [time_in, time_out] falls within `embargo` of the test block's
    [time_in, time_out] span is purged from that fold's training set."""
    n = len(df)
    folds = np.array_split(np.arange(n), n_splits)
    oof = np.full(n, np.nan)
    for test_idx in folds:
        t_start = df.loc[test_idx[0], "time_in"]
        t_end = df.loc[test_idx[-1], "time_out"]
        emb_lo, emb_hi = t_start - embargo, t_end + embargo
        overlaps = ~((df["time_out"] < emb_lo) | (df["time_in"] > emb_hi))
        train_mask = (~overlaps).to_numpy()
        train_mask[test_idx] = False
        tr, te = df.loc[train_mask], df.loc[test_idx]
        oof[test_idx] = fit_predict(model_name, tr, te)
    assert not np.isnan(oof).any(), "purged k-fold left an un-predicted trade"
    return oof


def run_purged_gate(df: pd.DataFrame, model_name: str, embargo: pd.Timedelta) -> None:
    print(f"\n=== Gate: purged {N_SPLITS_CV}-fold CV, embargo={embargo}, model={model_name} ===")
    p = purged_kfold_oof(df, model_name, N_SPLITS_CV, embargo)
    r = df["r"].to_numpy()
    print(f"unfiltered: n={len(r)} avgR={r.mean():+.4f} sumR={r.sum():+8.1f} WR={df['win'].mean() * 100:.1f}%")
    for thr in CV_THRESHOLDS:
        keep = p >= thr
        if keep.mean() < 0.02:
            print(f"  thr={thr:.2f}: kept {keep.sum()}/{len(r)} -- too few to evaluate")
            continue
        kr = r[keep]
        d_mean, lo, hi = bootstrap_ci(r, keep, N_BOOT, SEED)
        sig = "  <-- 95% CI excludes 0" if (lo > 0 or hi < 0) else ""
        print(f"  thr={thr:.2f}: kept {keep.sum():4d}/{len(r)} ({keep.mean() * 100:4.1f}%) "
              f"avgR={kr.mean():+.4f} sumR={kr.sum():+8.1f} WR={df['win'].to_numpy()[keep].mean() * 100:4.1f}% | "
              f"delta avgR={d_mean:+.4f} 95%CI=[{lo:+.4f},{hi:+.4f}]{sig}")


# ---------------------------------------------------------- walk-forward by year
def pick_threshold(tr: pd.DataFrame, p_tr: np.ndarray) -> float:
    best_thr, best_avg = 0.0, tr["r"].mean()
    for thr in THRESH_GRID:
        keep = p_tr >= thr
        if keep.mean() < MIN_KEEP_FRAC:
            continue
        avg = tr.loc[keep, "r"].mean()
        if avg > best_avg:
            best_avg, best_thr = avg, thr
    return best_thr


def walk_forward(df: pd.DataFrame, model_name: str) -> pd.DataFrame:
    year = df["time_in"].dt.year
    rows = []
    print(f"\n=== Anchored walk-forward by year, model={model_name} (threshold chosen on TRAIN only) ===")
    for y in WF_TEST_YEARS:
        tr, te = df[year < y], df[year == y]
        if len(te) == 0 or len(tr) < 200:
            continue
        p_tr = fit_predict(model_name, tr, tr)
        thr = pick_threshold(tr, p_tr)
        p_te = fit_predict(model_name, tr, te)
        keep = p_te >= thr
        te = te.assign(keep=keep)
        rows.append(te)
        kept = te[te["keep"]]
        kept_avg = kept["r"].mean() if len(kept) else float("nan")
        print(f"  {y}: thr={thr:.2f} train_n={len(tr):4d} kept {len(kept):3d}/{len(te):3d} | "
              f"unfiltered avgR={te['r'].mean():+.4f} sumR={te['r'].sum():+7.1f} | "
              f"filtered avgR={kept_avg:+.4f} sumR={kept['r'].sum():+7.1f}")
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def stitched_report(oos: pd.DataFrame) -> None:
    if oos.empty:
        print("  (no OOS rows)")
        return
    base, filt = oos, oos[oos["keep"]]
    y0, y1 = oos["time_in"].dt.year.min(), oos["time_in"].dt.year.max()
    print(f"  stitched OOS {y0}-{y1}: unfiltered n={len(base)} avgR={base['r'].mean():+.4f} "
          f"sumR={base['r'].sum():+7.1f} WR={base['win'].mean() * 100:.1f}%")
    print(f"  {'':10s}filtered   n={len(filt)} avgR={filt['r'].mean():+.4f} "
          f"sumR={filt['r'].sum():+7.1f} WR={filt['win'].mean() * 100:.1f}%")
    d_mean, lo, hi = bootstrap_ci(base["r"].to_numpy(), base["keep"].to_numpy(), N_BOOT, SEED)
    sig = "  <-- excludes 0" if (lo > 0 or hi < 0) else ""
    print(f"  {'':10s}delta avgR={d_mean:+.4f}  95% bootstrap CI=[{lo:+.4f}, {hi:+.4f}]{sig}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["logit", "gbm"], default=None,
                    help="restrict to one model; default runs both")
    args = ap.parse_args()
    models = [args.model] if args.model else ["logit", "gbm"]

    full = load_dataset()
    held_out_n = int((full["time_in"].dt.year == HOLDOUT_YEAR).sum())
    df = full[full["time_in"].dt.year < HOLDOUT_YEAR].reset_index(drop=True)
    print(f"holding out {HOLDOUT_YEAR} untouched ({held_out_n} trades) -- "
          f"true OOS already spent validating the base engine (passport SS4)")

    embargo_hours = float(np.ceil(df["bars_held"].max() * 15 / 60))
    embargo = pd.Timedelta(hours=embargo_hours)
    print(f"embargo = {embargo} (longest single trade held {int(df['bars_held'].max())} M15 bars)")

    for model_name in models:
        run_purged_gate(df, model_name, embargo)
        oos = walk_forward(df, model_name)
        stitched_report(oos)
