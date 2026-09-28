"""S007 paper-trading virtual book (ALGODEV-45 step 3).

A stand-in for the BROKER, not for the strategy. The trading rules are not
reimplemented here at all: the decision step stays
`bot.s007_paper.make_decide()` (the very same closure the live path runs), and
this module only does what the broker does on the other side of the wire --
hold positions, honour their server-side stop-loss/take-profit, report them
back, and apply the orders it is handed.

WHY A FILL SIMULATOR IS NEEDED HERE (a correction worth stating plainly)
It is tempting to assume the engine already resolves positions for us, because
`plan_now()` replays the whole day through `ger40_lonfra.simulate_day` on every
call and knows perfectly well which of its positions stopped out. It does --
but the LIVE execution layer never consumes that as a close signal. `decide()`
emits a `close` action only in the flat / day_done branch; an ordinary stop or
target exit happens because the ORDER CARRIES ITS OWN SL/TP
(`CTraderS007._place_market_step(symbol, side, sl, tp, ...)`) and the broker
fires it server-side, after which the position simply stops appearing in the
reconcile snapshot (see the `broker_side_close_detected` backfill in
`bot/s007_paper.py::decide`). With no broker in the loop, something must play
that role, and that something is this class -- otherwise a paper position whose
stop was breached would stay open forever, since nothing in `decide()` would
ever close it.

SAME-BAR ORDERING MIRRORS THE ENGINE
`strategies/ger40_lonfra/engine.py::_simulate_leg` tests the stop BEFORE the
target on each bar, so a bar whose range spans both is booked as a loss. That
conservative choice is copied here deliberately (see `_resolve_one`): the paper
book must not be luckier than the backtest that validated the strategy, and a
one-minute bar genuinely does not tell us which side printed first.

NOT A BROKER SIMULATOR IN GENERAL. Modelled on purpose: server-side SL/TP,
fills, and the position shape `decide()` reads. NOT modelled: margin, swap,
commission, partial fills, requotes, weekend gaps in the middle of a position,
or slippage beyond the deliberate choice below. Paper results are therefore an
UPPER bound on the real thing and must never be reported as if they were live
P&L.
"""
from __future__ import annotations

import json
from pathlib import Path

# Broker volume units: the Open API reports `volume` in units where
# raw = lots * 100 * lotSize, and CTraderS007._volume_from_lots builds it the
# same way. decide()'s open_risk math divides by exactly this constant
# (bot/s007_paper.py: `(p["volume"] / 100.0) * abs(...)`), so the paper book
# has to use the identical scaling or every risk figure silently differs.
VOLUME_UNITS_PER_LOT = 100.0

# Synthetic position ids start well above any plausible real cTrader id so a
# paper record can never be confused with a live one if the two ever end up
# side by side in an analysis.
PAPER_POSITION_ID_BASE = 900_000_000


class PaperBook:
    """The virtual account: open positions, cash balance, and the day's fills.

    Deliberately mirrors the broker's own vocabulary (`price` is the fill,
    `stop_loss`/`take_profit` are server-side, `volume` is in raw Open API
    units) so `as_broker_positions()` can be handed straight to `decide()`
    with no translation layer and therefore no place for a translation bug.
    """

    def __init__(self, balance: float, *, money_per_point_per_lot: float,
                 next_position_id: int = PAPER_POSITION_ID_BASE,
                 initial_balance: float | None = None):
        self.balance = float(balance)
        # The daily risk cap is a percentage of the balance the book STARTED
        # with, not of the live one (ALGODEV-37: a cap that floats with the
        # balance quietly widens after a win and tightens after a loss).
        self.initial_balance = float(balance if initial_balance is None
                                     else initial_balance)
        # In ACCOUNT currency (USD), i.e. the broker's raw quote-currency
        # lotSize ALREADY multiplied by the FX rate. The broker reports it in
        # the QUOTE currency (EUR for DE40, lotSize=100) and `decide()` applies
        # `fx_rate` itself on the way in -- but this class books realised P&L
        # straight onto `balance`, which is in USD, so it needs the converted
        # figure. Mixing the two silently understates every paper loss by ~14%.
        self.money_per_point_per_lot = float(money_per_point_per_lot)
        self.positions: dict[str, dict] = {}
        self.closed: list[dict] = []
        self._next_position_id = int(next_position_id)

    # ---------- state exposed to decide() ----------

    def as_broker_positions(self) -> list[dict]:
        """The open book in the exact shape `_reconcile_step` returns, because
        that is what `decide()` was written against (bot/ctrader_s007.py:203-223).
        Returns copies: `decide()` must never be able to mutate the book by
        holding on to a dict it was shown."""
        return [dict(p) for p in self.positions.values()]

    # ---------- broker-side lifecycle ----------

    def mark_fills(self, bars) -> list[dict]:
        """Honour server-side SL/TP against every bar newer than each
        position's own open time, and close whatever was breached.

        Called at the START of a tick, before `decide()` runs, so the decision
        step sees the same already-settled book a real reconcile would have
        returned. Returns the list of positions closed by this call, most
        recent last, for the caller to log.
        """
        filled = []
        for label in list(self.positions):
            pos = self.positions[label]
            outcome = self._resolve_one(pos, bars)
            if outcome is None:
                continue
            exit_price, reason = outcome
            filled.append(self._close(label, exit_price, reason))
        return filled

    def _resolve_one(self, pos: dict, bars) -> tuple[float, str] | None:
        """(exit_price, reason) for the first bar that breached this position's
        stop or target, or None if it is still open.

        Only bars strictly AFTER the position's open timestamp are considered:
        the bar a position was opened on has already partly happened by the
        time the order goes in, and replaying it would let the book resolve a
        trade on price action that preceded its own entry.
        """
        if bars is None or len(bars) == 0:
            return None
        opened_ts = pos["opened_ts"]
        window = bars[bars.index > opened_ts]
        if len(window) == 0:
            return None
        is_buy = pos["side"] == "buy"
        sl, tp = pos["stop_loss"], pos["take_profit"]
        for high, low in zip(window["high"].to_numpy(), window["low"].to_numpy()):
            # Stop first, target second -- same conservative ordering as
            # engine.py::_simulate_leg (see module docstring).
            if is_buy:
                if sl and low <= sl:
                    return float(sl), "stop_loss"
                if tp and high >= tp:
                    return float(tp), "take_profit"
            else:
                if sl and high >= sl:
                    return float(sl), "stop_loss"
                if tp and low <= tp:
                    return float(tp), "take_profit"
        return None

    def place(self, *, label: str, side: str, fill: float, sl: float,
              tp: float | None, volume_lots: float, opened_ts) -> dict:
        """Open a paper position at `fill` with server-side SL/TP attached,
        exactly as `_place_market_step` does for a real market order."""
        pos = dict(
            position_id=self._next_position_id,
            label=label,
            side=side,
            volume=float(volume_lots) * VOLUME_UNITS_PER_LOT,
            price=float(fill),
            stop_loss=float(sl),
            take_profit=float(tp) if tp is not None else None,
            opened_ts=opened_ts,
            volume_lots=float(volume_lots),
        )
        self._next_position_id += 1
        self.positions[label] = pos
        return pos

    def amend(self, label: str, sl: float) -> dict | None:
        """Move a position's server-side stop (the breakeven amend). Returns
        the updated position, or None if it is no longer open -- a stop that
        was breached in the same tick that wanted to amend it is gone, and
        that race exists against a real broker too."""
        pos = self.positions.get(label)
        if pos is None:
            return None
        pos["stop_loss"] = float(sl)
        return pos

    def close(self, label: str, price: float, reason: str) -> dict | None:
        """Close at market (the flat / day_done path in `decide()`)."""
        if label not in self.positions:
            return None
        return self._close(label, price, reason)

    def _close(self, label: str, exit_price: float, reason: str) -> dict:
        pos = self.positions.pop(label)
        direction = 1.0 if pos["side"] == "buy" else -1.0
        pnl = ((float(exit_price) - pos["price"]) * direction
               * pos["volume_lots"] * self.money_per_point_per_lot)
        self.balance += pnl
        rec = dict(label=label, position_id=pos["position_id"], side=pos["side"],
                   entry_price=pos["price"], exit_price=float(exit_price),
                   volume_lots=pos["volume_lots"], stop_loss=pos["stop_loss"],
                   take_profit=pos["take_profit"], opened_ts=pos["opened_ts"],
                   reason=reason, pnl=round(pnl, 2),
                   balance_after=round(self.balance, 2))
        self.closed.append(rec)
        return rec

    # ---------- persistence ----------
    #
    # A paper book that silently reset on every container restart would fake a
    # clean slate after each crash and quietly bias the results -- the daemon
    # runs under `restart: unless-stopped`, so that would not even be rare.
    # State is therefore written after every tick that changed it and reloaded
    # on startup.

    def to_dict(self) -> dict:
        return dict(balance=self.balance,
                    initial_balance=self.initial_balance,
                    money_per_point_per_lot=self.money_per_point_per_lot,
                    next_position_id=self._next_position_id,
                    positions={k: _jsonable(v) for k, v in self.positions.items()},
                    closed=[_jsonable(c) for c in self.closed])

    @classmethod
    def from_dict(cls, data: dict) -> "PaperBook":
        book = cls(data["balance"],
                   money_per_point_per_lot=data["money_per_point_per_lot"],
                   next_position_id=data.get("next_position_id", PAPER_POSITION_ID_BASE),
                   initial_balance=data.get("initial_balance"))
        book.positions = {k: dict(v) for k, v in data.get("positions", {}).items()}
        book.closed = list(data.get("closed", []))
        return book

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write must not leave a truncated book
        # that fails to parse on the next start and loses the day's positions.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, default=str),
                       encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path, *, balance: float,
             money_per_point_per_lot: float) -> "PaperBook":
        """Restore the book, or start a fresh one if there is nothing to
        restore. `balance`/`money_per_point_per_lot` are the fallbacks for a
        first-ever run only -- a restored book keeps its OWN balance, which is
        the whole point of a virtual account that diverges from the real one.
        """
        path = Path(path)
        if path.exists():
            try:
                return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, KeyError):
                # Corrupt state is recoverable only by starting over, but it
                # must be loud: the caller logs this and the file is kept.
                path.replace(path.with_suffix(".corrupt.json"))
        return cls(balance, money_per_point_per_lot=money_per_point_per_lot)


def _jsonable(rec: dict) -> dict:
    """Timestamps come from the bar index (pandas.Timestamp) and must survive
    a JSON round-trip as something `mark_fills` can compare against that same
    index again."""
    return {k: (str(v) if k == "opened_ts" else v) for k, v in rec.items()}
