"""S007 signal layer — derive the desired position set from recent M1 bars.

Reuses the VALIDATED engine (`ger40_lonfra.simulate_day`) on the day's bars up to
"now", so the live position lifecycle is BY CONSTRUCTION identical to the backtest
— no rule re-implementation. This mirrors S004's bot/signals.py.

Input: M1 DataFrame with a naive EET DatetimeIndex and open/high/low/close columns
(same timezone the engine was validated on — the cTrader adapter already returns
Europe/Bucharest naive bars).
"""
from __future__ import annotations

import pandas as pd

from strategies.ger40_lonfra import simulate_day, daily_levels
from strategies.ger40_lonfra import config as GC
from bot import s007_config as C


def _preset(name=None):
    return getattr(GC, name or C.PRESET)


def _prep(m1: pd.DataFrame) -> pd.DataFrame:
    dt = pd.DatetimeIndex(m1.index)
    df = pd.DataFrame({c.lower(): m1[c].to_numpy() for c in m1.columns})
    df["dt"] = dt
    df["date_only"] = dt.date
    df["time_only"] = dt.strftime("%H:%M")
    return df.sort_values("dt").reset_index(drop=True)


def plan_now(m1: pd.DataFrame, now: pd.Timestamp | None = None,
             preset: str | None = None) -> dict:
    """Return the desired broker state for GER40 as of `now`.

    dict:
      in_window   -- True while inside the trade session (10:00-16:59)
      day_done    -- True if the day's target was reached (=> close all)
      flat        -- True at/after EXIT_END (=> close all)
      positions   -- list of positions that SHOULD be open right now, each:
                     {label, side, entry, sl, orig_sl, tp, is_add, be_moved}
                     be_moved (ALGODEV-37): True once the engine's breakeven
                     rule (cfg.breakeven_at_r, off unless the preset sets it)
                     has fired for this position -- `sl` then already equals
                     the engine's entry. The live layer uses this flag (not
                     an sl==entry comparison) to decide to amend the broker
                     stop, targeting the broker's ACTUAL fill price so the
                     real breakeven includes slippage.
                     orig_sl (ALGODEV-38): the stop BEFORE any breakeven
                     move -- the live layer must use THIS, not `sl`, when
                     placing a position for the first time (a label not yet
                     seen open at the broker), since `sl` can already be
                     be_moved-collapsed to `entry` on the very first cycle a
                     late-detected reversal/add leg is seen.
      direction   -- 'up'/'down'/None for the day
      filtered    -- True only when today's Frankfurt range failed the
                     day-quality height filter -- a verdict that is FINAL for
                     the rest of today (see the height-check branch below).
                     Absent/falsy in every other case, including "not enough
                     bars yet" and "no A/B scenario yet", which can still
                     resolve later today and must keep being polled.
      breakeven_at_r -- (ALGODEV-39) the active preset's breakeven trigger, in
                     R (None if off). Lets decide() ALSO check breakeven
                     against the broker's REAL fill price/stop -- be_moved
                     above only ever reflects the engine's theoretical entry,
                     blind to slippage.
      breakeven_offset_points -- (ALGODEV-41) how far PAST the entry, in raw
                     index points, the breakeven stop is placed when the rule
                     fires; 0.0 when off (and meaningless while
                     breakeven_at_r is None). The live layer adds it to the
                     amend target (in the profit direction, off the broker's
                     REAL fill) so a BE exit clears the round-trip spread
                     instead of booking it as a small loss.
    """
    cfg = _preset(preset).with_(trade_start=C.TRADE_START, exit_end=C.EXIT_END,
                                fr_start=C.FR_START, fr_end=C.FR_END)
    df = _prep(m1)
    now = now or df["dt"].iloc[-1]
    today = now.date()
    now_hm = now.strftime("%H:%M")

    in_window = C.TRADE_START <= now_hm <= C.EXIT_END
    flat = now_hm >= C.EXIT_END

    fr = df[(df.date_only == today) & (df.time_only >= C.FR_START) & (df.time_only <= C.FR_END)]
    if len(fr) < cfg.min_fr_bars:
        return dict(in_window=in_window, day_done=False, flat=flat, positions=[],
                    resolved=[], direction=None, context=None)
    rh, rl = fr["high"].max(), fr["low"].min()
    mid = (rh + rl) / 2
    height = rh - rl
    if height <= 0 or (cfg.max_height is not None and height > cfg.max_height):
        # This verdict is FINAL for the day, not "no signal yet": the Frankfurt
        # range (09:00-09:59) is already fixed and closed by the time we can
        # compute rh/rl at all, so height cannot change on a later call this
        # same day. filtered=True lets a scheduler (scripts/s007_loop.py) stop
        # polling once it sees this, instead of re-checking every minute until
        # 16:59 for an answer that was already decided at ~10:00 (see
        # decisions-log.md 2026-07-23). Contrast with the two branches above/
        # below this one, which return the same in-window "nothing to do yet"
        # shape but for reasons that CAN still resolve later today (bars still
        # arriving, or no A/B scenario yet) -- those must NOT set filtered.
        return dict(in_window=in_window, day_done=False, flat=flat, positions=[],
                    resolved=[], direction=None, context=None, filtered=True)

    ld = df[(df.date_only == today) & (df.time_only >= C.TRADE_START) & (df.time_only <= now_hm)]
    if len(ld) < 2:
        return dict(in_window=in_window, day_done=False, flat=flat, positions=[],
                    resolved=[], direction=None, context=None)
    bars = ld.reset_index(drop=True)

    lv = daily_levels(df) if cfg.tp_mode == "liquidity" else {}
    r = simulate_day(bars, rh, rl, mid, height, lv.get(today, {}), cfg)
    if r.get("scenario") not in ("A", "B"):
        return dict(in_window=in_window, day_done=False, flat=flat, positions=[],
                    resolved=[], direction=None, context=None)

    reached = bool(r["reached_tp"])
    tp = r["tp"]
    context = dict(scenario=r["scenario"], direction=r["direction"],
                   rh=float(rh), rl=float(rl), mid=float(mid), height=float(height),
                   tp=float(tp), n_pos=int(r["n_pos"]), reached_tp=reached,
                   n_recovery=int(r["n_recovery"]))
    wanted = []
    # 'resolved' = positions the engine entered AND already saw stop/tp/daycap
    # within the SAME bars this call replayed to reach "now" -- the live bot
    # never had a chance to place a broker order for these (only 'eod' status
    # positions, still open as of "now", become wanted/live orders below), so
    # they would otherwise vanish from view entirely. Surfaced separately so a
    # caller can log them (see bot/s007_paper.py::decide ghost-trade logging)
    # instead of silently losing "the signal fired but resolved before we saw
    # it" history -- came up live 2026-08-05 investigating a B setup that
    # never produced an order.
    resolved = []
    for p in r["positions"]:
        side = "buy" if p["up"] else "sell"
        label = f"{C.MAGIC}:{today.isoformat()}:{p['idx']}"
        # Each position carries its OWN tp (engine.py::_simulate_leg stores the
        # tp it was actually simulated against) -- a b-reversal leg runs with a
        # different target (tp_A, the opposite boundary) than the primary B leg
        # (tp, the day-level value above). Using the day-level `tp` here for
        # every position sent a live reversal BUY out with the primary SELL
        # leg's (lower) target attached -- cTrader rejected it outright
        # (TRADING_BAD_STOPS: TP below entry on a BUY), found live 2026-08-06.
        p_tp = float(p.get("tp", tp))
        # orig_sl (ALGODEV-38, fixed ALGODEV-40): the position's stop BEFORE
        # any breakeven move. p["stop"] can already equal p["entry"]
        # (be_moved=True) the VERY FIRST cycle a position is seen as "eod",
        # if the replayed price path crossed its own breakeven trigger
        # before the live bot ever placed a broker order for it -- found
        # live 2026-09-03: a brand-new market order went out with sl==entry
        # (zero stop distance) because the caller used p["stop"]
        # unconditionally. orig_sl lets bot/s007_paper.py's decide() use the
        # correct WIDE stop for a genuinely new placement.
        #
        # ALGODEV-40 (found live 2026-09-07): this used to be RE-DERIVED as
        # `entry -+ risk0` (direction-based), which silently mirrors the
        # stop to the "textbook" side of entry -- wrong whenever the
        # position's REAL stop sits on the opposite side, which is a normal,
        # expected situation under stop_mode="mid_range": every position in
        # a leg (primary AND every pyramided add) shares ONE common stop
        # regardless of that add's own entry price, so an add entered on a
        # pullback can legitimately have its shared stop on the "wrong"
        # side of its own entry. Two such adds got a wrong-side, too-tight
        # stop sent to the broker and turned engine-validated wins into
        # real losses (-$60 combined). Fixed by reading engine.py's own
        # p["stop0"] directly -- the position's real stop as of creation,
        # stored once and never mutated by the breakeven block (mirrors how
        # p["risk0"] is already handled) -- instead of re-deriving it.
        orig_sl = float(p.get("stop0", p["stop"]))
        if p["status"] == "eod":
            wanted.append(dict(label=label, side=side, entry=float(p["entry"]),
                               sl=float(p["stop"]), orig_sl=float(orig_sl), tp=p_tp,
                               is_add=bool(p["is_add"]),
                               be_moved=bool(p.get("be_moved", False))))
        else:
            resolved.append(dict(label=label, side=side, entry=float(p["entry"]),
                                 sl=float(p["stop"]), exit=float(p["exit"]),
                                 status=p["status"], is_add=bool(p["is_add"]),
                                 is_recovery=bool(p.get("is_recovery", False)), r=float(p["R"])))
    return dict(in_window=in_window, day_done=reached, flat=flat,
                positions=wanted, resolved=resolved, direction=r["direction"], context=context,
                # ALGODEV-39: surfaced so decide() can ALSO check breakeven
                # against the broker's real fill price (the engine-only
                # be_moved above can't see slippage) -- None when the active
                # preset has breakeven off, same as cfg.breakeven_at_r itself.
                breakeven_at_r=cfg.breakeven_at_r,
                # ALGODEV-41: how far past entry the BE stop goes, in raw
                # points. decide() applies it to the broker's real fill so a
                # live BE exit clears the round-trip spread; 0.0 (default on
                # every preset but an explicit _OFF<N> one) reproduces the
                # original "amend to exactly the fill price" behaviour.
                breakeven_offset_points=cfg.breakeven_offset_points)
