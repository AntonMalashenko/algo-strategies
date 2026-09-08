"""S016 -- MTF top-down engine: D1 trend -> H4 zone -> H1 confirm -> M15 entry.

Formalization source: claude/strategy-passport-S016.md v0.3 SS2-3 (Anton, voice session
2026-08-13 + chat 2026-08-26). This is a SEPARATE strategy from S004, not a modifier of it
(strategy-modifiers governs variations of an existing frozen baseline; S016 has no baseline
yet) -- lives in its own module. Reuses `strategies.fvg_mtf._last_confirmed_swings` (the
causal fractal-swing helper already regression-tested for S004) rather than re-implementing it,
per code-architecture's single-source-of-truth rule.

THE FUNNEL (E2, passport SS5.2):
  1. D1  -- trend filter (SMA of daily close), same convention as fvg_mtf.run_backtest's
            `trend_ma_days`/`trend_align`.
  2. H4  -- an order-block/rejection-block zone (SS2 object, see `find_order_blocks` below),
            direction aligned with the D1 trend.
  3. Wait for the H4 zone to be TESTED (first M15 bar whose [low,high] range touches the
     zone's [bot,top] band, at or after the zone's `avail` time).
  4. H1  -- after that test, the first H1-timeframe zone of the SAME direction that (a) FORMS
            strictly after the test and (b) does NOT overlap the H4 zone's price range
            (Anton's words: "те же типы зон что и 4H, но уже после теста 4Й зоны, то есть зоны
            не пересекаются"). This zone can be EITHER a confirmed order-block/rejection-block
            (`find_order_blocks`) OR an indyusment (`find_inducement`, Anton 2026-08-26) --
            whichever qualifies first is the "H1 confirm"; its type (`h1_zone_type`) drives the
            stop rule in step 7.
  5. Wait for the H1 zone to be TESTED (same touch rule as step 3).
  6. M15 -- after that test, whichever forms FIRST: an order-block/rejection-block zone (SS2)
            or an FVG (3-candle gap, same definition S004 uses on H4, here on M15) -- Anton's
            words: "формирование ОБ после теста 1 часа, или формирование фвг после
            тестирования 1 часа (если раньше чем ОБ)". This is the entry zone.
  7. Entry -- limit fill at the M15 zone's near edge on first touch (same "base" semantics as
     S004's H4-FVG entry). Stop (Anton 2026-08-26, second iteration -- see `run_backtest` for
     the full rationale): if the H1 confirm (step 4) was an indyusment, stop behind the M15
     entry zone's far edge (tight); otherwise stop is keyed to the H4 zone (step 2), at its
     MIDPOINT if the H4 zone's test (step 3) was a shallow touch, or its FAR edge if the test
     penetrated >=50% of the zone's depth. Target = RR * risk (unchanged, RR=2 default).

Two assumptions NOT dictated verbatim by Anton, applied by analogy and flagged in the passport
for his review:
  - The M15 zone is also required to not overlap the H1 zone (same non-overlap rule as H4->H1,
    extended one level down for consistency). Anton only stated the rule for H4->H1 explicitly.
  - Stop/target mechanics (buffer, RR) mirror S004's `run_backtest` "zone" stop mode, since this
    was not part of the funnel description itself.

Causality: every zone list is built bar-by-bar from ONLY already-closed bars of its own
timeframe (mirrors `find_h4_fvg`/`_last_confirmed_swings`). The cascade (test -> next-level
zone -> test -> ...) only ever looks FORWARD in time from a known event timestamp -- no stage
is allowed to use a zone/test that occurs before the timestamp that unlocked it. One H4 zone
produces at most one trade (same "one trade per zone" rule as S004).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.fvg_mtf import _last_confirmed_swings

BUFFER_PIPS = 2.0        # stop-loss buffer, same constant as fvg_mtf.py
IND_MIN_RISK_PIPS = 8.0  # floor on stop distance for indyusment-confirmed (m15_ind) trades --
                          # see run_backtest's stop-rule comment (v0.6 "попробуй" iteration,
                          # 2026-08-26) for the diagnosis this fixes; first-pass guess, untuned.
H4_MIN_RISK_PIPS = 8.0   # floor on stop distance for the H4-branch (h4_far/h4_touch) trades --
                          # full-universe trade-level analysis (2026-08-26, pooled 19-instrument
                          # E2 sweep) found 632/4786 H4-branch trades (13%) with risk_pips < 5
                          # (touch_px landing almost exactly on the H4 zone edge) -- these alone
                          # carried avg net_r=-0.88R and accounted for 76% of the H4-branch's
                          # total net R loss. Same disease as the m15_ind branch before
                          # IND_MIN_RISK_PIPS (2026-08-26 "попробуй" iteration), just never
                          # floored for this branch. First-pass guess, same value as
                          # IND_MIN_RISK_PIPS, untuned.
CONTINUATION_LOOKAHEAD_BARS = 3  # gated behind `require_continuation` (default off, see
                          # run_backtest). How many M15 bars after the entry-trigger bar to
                          # search for a fresh local extreme (new high for a long / new low for
                          # a short) beyond the entry bar's OWN high/low. Anton 2026-08-26 (chat,
                          # "цена должна показать реверс на м15 в виде ентри модели") -- formalized
                          # by hand-verifying 4 real trades (2 TP, 2 SL) against their raw M15
                          # bars: both winners (GBPJPY long, EURUSD short) printed a fresh
                          # high/low within 1-2 bars of the entry-trigger bar; both losers
                          # (EURCHF short, EURJPY short) never printed one during the ENTIRE
                          # holding period, even when the entry-trigger bar itself had a
                          # deceptive rejection wick (EURJPY case) -- a same-bar wick alone was
                          # NOT a reliable signal, only genuine continuation in the following bars
                          # was. First-pass guess (not swept/tuned), N=4 examples only.


def resample_tf(m15: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Build <freq> bars from M15 so all timeframes stay perfectly consistent
    (same approach as fvg_mtf.resample_h4, generalized to any pandas offset)."""
    agg = {"open": "first", "high": "max", "low": "min",
           "close": "last", "volume": "sum"}
    return m15.resample(freq).agg(agg).dropna(subset=["close"])


def find_order_blocks(bars: pd.DataFrame, bar_td: pd.Timedelta) -> list[dict]:
    """S016 SS2 object: order-block / rejection-block, causal, either direction.

    Algorithm (literal reading of SS2 -- see module docstring for the exact quote):
      1. Sweep bar i: low[i] breaks below the last CONFIRMED swing low known strictly
         before i (bullish case; mirrored for bearish using swing highs). Confirmation
         uses `_last_confirmed_swings` (k=2 bars), identical causal rule to S004's FVG
         sweep tag.
      2. "First candle of the series" = the first bullish bar j >= i (close > open).
         Upper bound = open[j].
      3. Lower bound = min(low[i..j]) -- the true extreme reached during the sweep, not
         just bar i's low, in case the sweep runs a few bars before reversing.
      4. The block becomes VALID (tradable) at the first bar t > j whose CLOSE breaks
         back above the upper bound, PROVIDED no bar between j and t closed below the
         lower bound first (that would invalidate the block instead). `avail` = that
         bar's close time.
    Bearish is the exact mirror (sweep above a confirmed swing high, first bearish
    candle of the series, upper bound = sweep extreme, lower bound = open of that
    candle, confirmed on a close back below the lower bound).
    """
    o, h, l, c = bars["open"].values, bars["high"].values, bars["low"].values, bars["close"].values
    times = bars.index
    n = len(bars)
    last_sh, last_sl = _last_confirmed_swings(h, l)

    zones = []
    i = 2
    while i < n:
        # bullish sweep: low[i] breaks below the last confirmed swing low
        if not np.isnan(last_sl[i]) and l[i] < last_sl[i]:
            j = i
            while j < n and not (c[j] > o[j]):
                j += 1
            if j < n:
                upper = o[j]
                lower = float(np.min(l[i:j + 1]))
                if upper > lower:
                    t = j + 1
                    invalidated = False
                    while t < n:
                        if c[t] < lower:
                            invalidated = True
                            break
                        if c[t] > upper:
                            break
                        t += 1
                    if t < n and not invalidated and c[t] > upper:
                        zones.append(dict(dir=1, top=upper, bot=lower,
                                          avail=times[t] + bar_td, formed_at=times[j]))
            i = j + 1
            continue
        # bearish sweep: high[i] breaks above the last confirmed swing high
        if not np.isnan(last_sh[i]) and h[i] > last_sh[i]:
            j = i
            while j < n and not (c[j] < o[j]):
                j += 1
            if j < n:
                lower = o[j]
                upper = float(np.max(h[i:j + 1]))
                if upper > lower:
                    t = j + 1
                    invalidated = False
                    while t < n:
                        if c[t] > upper:
                            invalidated = True
                            break
                        if c[t] < lower:
                            break
                        t += 1
                    if t < n and not invalidated and c[t] < lower:
                        zones.append(dict(dir=-1, top=upper, bot=lower,
                                          avail=times[t] + bar_td, formed_at=times[j]))
            i = j + 1
            continue
        i += 1
    zones.sort(key=lambda z: z["avail"])
    return zones


def find_fvg_generic(bars: pd.DataFrame, bar_td: pd.Timedelta) -> list[dict]:
    """3-candle-gap FVG, same rule as fvg_mtf.find_h4_fvg but timeframe-agnostic
    (no sweep tag -- not needed for the M15 entry stage)."""
    h, l = bars["high"].values, bars["low"].values
    times = bars.index
    zones = []
    for i in range(len(bars) - 2):
        avail = times[i + 2] + bar_td
        if l[i + 2] > h[i]:
            zones.append(dict(dir=1, top=l[i + 2], bot=h[i], avail=avail, formed_at=times[i + 2]))
        elif h[i + 2] < l[i]:
            zones.append(dict(dir=-1, top=l[i], bot=h[i + 2], avail=avail, formed_at=times[i + 2]))
    zones.sort(key=lambda z: z["avail"])
    return zones


def find_inducement(bars: pd.DataFrame, bar_td: pd.Timedelta) -> list[dict]:
    """S016 "indyusment" (liquidity sweep without full block confirmation) -- Anton's
    rule 2026-08-26 for H1 stop placement ("если по индьюсменту 1 час, то за M15"),
    formalized per his confirmed choice: same sweep-bar primitive as `find_order_blocks`
    (steps 1-3: sweep bar breaks a confirmed swing extreme, first opposite-color bar closes
    the "series", bounds = open of that bar vs the sweep extreme) but WITHOUT step 4's
    close-through confirmation -- the zone is available right after the reversal bar closes,
    a strictly weaker/faster signal than a confirmed order-block. This is the first practical
    formalization of SS3 item 1 ("индьюсмент", previously undefined) -- flagged in the
    passport for Anton's review, since he confirmed the mechanism but not a written spec.
    """
    o, h, l, c = bars["open"].values, bars["high"].values, bars["low"].values, bars["close"].values
    times = bars.index
    n = len(bars)
    last_sh, last_sl = _last_confirmed_swings(h, l)

    zones = []
    i = 2
    while i < n:
        if not np.isnan(last_sl[i]) and l[i] < last_sl[i]:
            j = i
            while j < n and not (c[j] > o[j]):
                j += 1
            if j < n:
                upper = o[j]
                lower = float(np.min(l[i:j + 1]))
                if upper > lower:
                    zones.append(dict(dir=1, top=upper, bot=lower,
                                      avail=times[j] + bar_td, formed_at=times[j]))
            i = j + 1
            continue
        if not np.isnan(last_sh[i]) and h[i] > last_sh[i]:
            j = i
            while j < n and not (c[j] < o[j]):
                j += 1
            if j < n:
                lower = o[j]
                upper = float(np.max(h[i:j + 1]))
                if upper > lower:
                    zones.append(dict(dir=-1, top=upper, bot=lower,
                                      avail=times[j] + bar_td, formed_at=times[j]))
            i = j + 1
            continue
        i += 1
    zones.sort(key=lambda z: z["avail"])
    return zones


def _overlaps(a: dict, b: dict) -> bool:
    return not (a["top"] <= b["bot"] or b["top"] <= a["bot"])


def first_test(zone: dict, m15_h: np.ndarray, m15_l: np.ndarray,
               m15_times: pd.DatetimeIndex, from_idx: int) -> int | None:
    """Index of the first M15 bar at/after `from_idx` whose [low,high] range
    touches the zone's [bot,top] band. Returns None if never touched."""
    bot, top = zone["bot"], zone["top"]
    for t in range(from_idx, len(m15_times)):
        if m15_l[t] <= top and m15_h[t] >= bot:
            return t
    return None


def _idx_at_or_after(times: pd.DatetimeIndex, ts: pd.Timestamp) -> int:
    pos = times.searchsorted(ts, side="left")
    return int(pos)


def run_backtest(m15: pd.DataFrame, rr: float = 2.0, pip: float = 1e-4,
                 spread_pips: float = 0.9,
                 trend_ma_days: int | None = 20, trend_align: str = "with",
                 require_continuation: bool = False,
                 continuation_lookahead_bars: int = CONTINUATION_LOOKAHEAD_BARS,
                 continuation_mode: str = "off") -> pd.DataFrame:
    """Event-driven backtest of the full D1->H4->H1->M15 funnel (SS5.2). Returns a
    DataFrame of trades, one row per H4 zone that survived the whole cascade to an
    actual entry (at most one trade per H4 zone, same rule as S004).
    """
    times = m15.index
    h_arr, l_arr, c_arr = m15["high"].values, m15["low"].values, m15["close"].values
    n = len(m15)

    h4 = resample_tf(m15, "4h")
    h1 = resample_tf(m15, "1h")

    h4_zones = find_order_blocks(h4, pd.Timedelta(hours=4))
    h1_zones = find_order_blocks(h1, pd.Timedelta(hours=1))
    h1_ind_zones = find_inducement(h1, pd.Timedelta(hours=1))
    m15_ob_zones = find_order_blocks(m15, pd.Timedelta(minutes=15))
    m15_fvg_zones = find_fvg_generic(m15, pd.Timedelta(minutes=15))
    h1_zones.sort(key=lambda z: z["avail"])
    h1_ind_zones.sort(key=lambda z: z["avail"])
    m15_ob_zones.sort(key=lambda z: z["avail"])
    m15_fvg_zones.sort(key=lambda z: z["avail"])

    if trend_ma_days is not None:
        daily_close = m15["close"].resample("1D").last().dropna()
        sma = daily_close.rolling(trend_ma_days).mean()
        t_daily = np.sign(daily_close - sma).shift(1)
        day_keys = times.floor("D")
        trend = t_daily.reindex(day_keys).fillna(0.0).values
    else:
        trend = None

    buf = BUFFER_PIPS * pip
    cost = spread_pips * pip
    trades = []
    used_m15 = set()   # id(z15) already consumed by an earlier H4 lineage --
                        # different H4 zones can independently cascade to the SAME
                        # downstream M15 entry zone; only the first lineage to reach
                        # it actually trades it (one trade per M15 entry zone, same
                        # "one trade per zone" rule S004 applies at the H4 level).

    def _next_zone(pool: list[dict], d: int, after_ts: pd.Timestamp, ref_zone: dict):
        for z in pool:
            if z["dir"] == d and z["formed_at"] > after_ts and not _overlaps(z, ref_zone):
                return z
        return None

    def _next_zone_typed(pools, d: int, after_ts: pd.Timestamp, ref_zone: dict):
        """Earliest-forming qualifying zone across several (pool, tag) candidates --
        used for the H1 confirm, which can be EITHER an order-block/rejection-block OR
        an indyusment (Anton 2026-08-26: "те же типы зон" was said for H4->H1 order-blocks;
        the indyusment alternative at H1 is his later addition, tagged separately because it
        changes the stop rule below)."""
        best, best_tag = None, None
        for pool, tag in pools:
            z = _next_zone(pool, d, after_ts, ref_zone)
            if z is not None and (best is None or z["formed_at"] < best["formed_at"]):
                best, best_tag = z, tag
        return best, best_tag

    for z4 in h4_zones:
        i4 = _idx_at_or_after(times, z4["avail"])
        if i4 >= n:
            continue
        t_test4 = first_test(z4, h_arr, l_arr, times, i4)
        if t_test4 is None:
            continue
        ts_test4 = times[t_test4]

        z1, z1_type = _next_zone_typed(
            [(h1_zones, "ob"), (h1_ind_zones, "ind")], z4["dir"], ts_test4, z4)
        if z1 is None:
            continue
        i1 = _idx_at_or_after(times, z1["avail"])
        if i1 >= n or i1 <= t_test4:
            i1 = t_test4 + 1
        t_test1 = first_test(z1, h_arr, l_arr, times, i1)
        if t_test1 is None:
            continue
        ts_test1 = times[t_test1]

        cand_ob = _next_zone(m15_ob_zones, z4["dir"], ts_test1, z1)
        cand_fvg = _next_zone(m15_fvg_zones, z4["dir"], ts_test1, z1)
        if cand_ob is None and cand_fvg is None:
            continue
        if cand_ob is None:
            z15 = cand_fvg
        elif cand_fvg is None:
            z15 = cand_ob
        else:
            z15 = cand_ob if cand_ob["avail"] <= cand_fvg["avail"] else cand_fvg

        if id(z15) in used_m15:
            continue
        i15 = _idx_at_or_after(times, z15["avail"])
        if i15 >= n or i15 <= t_test1:
            i15 = t_test1 + 1
        t_entry = first_test(z15, h_arr, l_arr, times, i15)
        if t_entry is None:
            continue
        used_m15.add(id(z15))

        d = z4["dir"]

        # Continuation-confirmation gate -- Anton 2026-08-26 (chat, formalization-fidelity
        # thread): a raw touch of the M15 zone is not enough evidence of a real "entry model"
        # (see CONTINUATION_LOOKAHEAD_BARS above for the 4-trade derivation). Off by default --
        # only the base engine's existing behavior applies unless the caller opts in.
        #
        # WARNING -- `require_continuation` is a NON-CAUSAL diagnostic/oracle, not an
        # executable rule: it decides whether to keep a trade that has ALREADY been filled at
        # `t_entry` using bars t_entry+1..t_entry+N, which have not happened yet at the moment
        # the limit order fills. In live trading you cannot un-fill an order because the next
        # 3 bars disappointed you. This mode exists ONLY to measure the ceiling of the idea
        # (2026-08-26 first full-universe run: n=3381/6789, WR 43.6%, net +0.246R, gross
        # +0.308R, positive in all 19 instruments AND both universe halves -- too clean to be
        # real, exactly because it's not real). The CAUSAL, executable version is
        # `continuation_mode="early_exit"` below -- use that one for any result meant to
        # inform a trading decision.
        if require_continuation:
            look_end = min(n, t_entry + 1 + continuation_lookahead_bars)
            if d == 1:
                extended = bool(np.any(h_arr[t_entry + 1:look_end] > h_arr[t_entry]))
            else:
                extended = bool(np.any(l_arr[t_entry + 1:look_end] < l_arr[t_entry]))
            if not extended:
                continue

        if trend is not None:
            tv = trend[t_entry]
            want = d if trend_align == "with" else -d
            if tv != want:
                continue

        entry = z15["top"] if d == 1 else z15["bot"]
        # Stop rule -- Anton 2026-08-26, THIRD iteration (previous: v0.5 stop=H1-zone was
        # worse than v0.4 stop=M15-zone on net AND gross; v0.6 introduced the ind/order-block
        # branch below and a zone-MIDPOINT anchor for the shallow-test branch). This pass
        # ("попробуй") fixes two data-diagnosed problems found in the v0.6 breakdown by
        # stop_mode (passport SS5.2):
        #   (a) "h4_mid" (shallow H4 test, was anchored at the H4 zone's geometric MIDPOINT)
        #       was NEGATIVE even gross (-0.11R on the EURUSD spot-check) and held trades for
        #       ~1 day median -- not a cost problem, a selection problem: a shallow test can
        #       happen on an abnormally TALL H4 zone, and "zone midpoint" then puts the stop
        #       far from where price actually reversed. Fixed by anchoring the stop at the
        #       ACTUAL test-bar penetration point (`touch_px`, already computed for the
        #       depth-fraction check) instead of the zone's geometric midpoint -- risk now
        #       reflects the observed reversal, not the raw size of the H4 zone. Renamed
        #       "h4_mid" -> "h4_touch" to match.
        #   (b) "m15_ind" (indyusment-confirmed H1, stop at the M15 zone) was near-breakeven
        #       GROSS (-0.01R) but had by far the tightest median risk (~4.7 pips) of the
        #       three modes, so a fixed spread crushed it in net -- the same cost-sensitivity
        #       disease diagnosed for the very first (v0.4) all-M15-stop design. Fixed with a
        #       floor: if the raw M15-zone risk is below IND_MIN_RISK_PIPS, the stop is pushed
        #       out to that floor instead of trading whatever tiny distance the M15 zone
        #       happens to have. IND_MIN_RISK_PIPS=8.0 is a first-pass guess (~1.7x the
        #       observed median, dropping cost/risk from ~19% to ~11% of the fixed 0.9-pip
        #       spread) -- not tuned, flagged for a follow-up sweep.
        depth_frac = None  # diagnostic field, only meaningful for the order-block branch below
        h4_height_pips = (z4["top"] - z4["bot"]) / pip
        if z1_type == "ind":
            raw_sl = (z15["bot"] - buf) if d == 1 else (z15["top"] + buf)
            raw_risk = (entry - raw_sl) * d
            min_risk = IND_MIN_RISK_PIPS * pip
            if raw_risk > 0 and raw_risk < min_risk:
                sl = entry - d * min_risk
            else:
                sl = raw_sl
            stop_mode = "m15_ind"
        else:
            top4, bot4 = z4["top"], z4["bot"]
            height4 = top4 - bot4
            if d == 1:
                touch_px = max(l_arr[t_test4], bot4)
                depth_frac = (top4 - touch_px) / height4 if height4 > 0 else 0.0
            else:
                touch_px = min(h_arr[t_test4], top4)
                depth_frac = (touch_px - bot4) / height4 if height4 > 0 else 0.0
            if depth_frac >= 0.5:
                raw_sl = (bot4 - buf) if d == 1 else (top4 + buf)
                stop_mode = "h4_far"
            else:
                raw_sl = (touch_px - buf) if d == 1 else (touch_px + buf)
                stop_mode = "h4_touch"
            raw_risk = (entry - raw_sl) * d
            min_risk4 = H4_MIN_RISK_PIPS * pip
            if raw_risk > 0 and raw_risk < min_risk4:
                sl = entry - d * min_risk4
            else:
                sl = raw_sl
        risk = (entry - sl) * d
        if risk <= 0:
            continue
        tp = entry + d * rr * risk

        # CAUSAL continuation check (`continuation_mode="early_exit"`): unlike
        # `require_continuation` above, this does NOT peek at future bars to decide whether to
        # take the trade -- the trade is entered exactly as the base engine always does, and is
        # only cut short at bar t_entry+continuation_lookahead_bars if, BY THAT BAR (using only
        # bars that have already happened relative to it), price never printed a fresh extreme
        # beyond the entry-trigger bar's own high/low in the trade's direction. This is the
        # realistic reading of Anton's rule: manage the position as if you watched it live and
        # bailed when it didn't confirm in time, rather than retroactively un-filling it.
        confirm_deadline = t_entry + continuation_lookahead_bars if continuation_mode == "early_exit" else None
        extended = False
        entry_extreme = h_arr[t_entry] if d == 1 else l_arr[t_entry]

        exit_px = exit_reason = exit_t = None
        for t in range(t_entry + 1, n):
            hit_sl = l_arr[t] <= sl if d == 1 else h_arr[t] >= sl
            if hit_sl:
                exit_px, exit_reason, exit_t = sl, "sl", t
                break
            hit_tp = h_arr[t] >= tp if d == 1 else l_arr[t] <= tp
            if hit_tp:
                exit_px, exit_reason, exit_t = tp, "tp", t
                break
            if confirm_deadline is not None and not extended:
                if (h_arr[t] > entry_extreme) if d == 1 else (l_arr[t] < entry_extreme):
                    extended = True
                elif t >= confirm_deadline:
                    exit_px, exit_reason, exit_t = c_arr[t], "no_continuation", t
                    break
        if exit_px is None:
            continue
        pnl = (exit_px - entry) * d - cost
        trades.append(dict(
            time_in=times[t_entry], time_out=times[exit_t], hour=times[t_entry].hour,
            dir=d, entry=entry, sl=sl, tp=tp, exit=exit_px, r=pnl / risk,
            bars_held=exit_t - t_entry, exit_reason=exit_reason,
            h4_zone_formed=z4["formed_at"], h1_zone_formed=z1["formed_at"],
            m15_zone_type=("ob" if z15 is cand_ob else "fvg"),
            h1_zone_type=z1_type, stop_mode=stop_mode,
            depth_frac=depth_frac, h4_height_pips=h4_height_pips, risk_pips=risk / pip,
        ))

    return pd.DataFrame(trades)
