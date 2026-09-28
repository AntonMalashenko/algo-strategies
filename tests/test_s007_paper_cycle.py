"""End-to-end check of one S007 paper cycle (ALGODEV-45 step 3).

Exercises the real `make_decide()` closure against the real `PaperBook`, with
only `plan_now` faked, so the wiring between the two -- action shapes, volume
units, fill ordering, risk-budget feedback -- is verified here rather than on
a live account. The trading rules themselves are not retested: they belong to
the already-validated engine, and duplicating them here would just create a
second place for them to drift.
"""
from __future__ import annotations

import pandas as pd
import pytest

from bot import s007_paper
from bot.s007_paper_book import PaperBook
from bot.s007_paper_cycle import run_paper_cycle
from utils.trade_logger import StrategyLogger

SYMBOL = "GER40"
MONEY_PER_POINT_PER_LOT = 100.0


@pytest.fixture
def logger(tmp_path):
    return StrategyLogger("S007PAPERTEST", log_root=str(tmp_path), console=False)


def _m1(n_bars: int, *, high: float, low: float, start_min: int = 0):
    idx = pd.to_datetime([f"2026-09-25 10:{start_min + i:02d}:00" for i in range(n_bars)])
    mid = (high + low) / 2
    return pd.DataFrame({"open": [mid] * n_bars, "high": [high] * n_bars,
                         "low": [low] * n_bars, "close": [mid] * n_bars}, index=idx)


def _plan(positions, **over):
    base = dict(in_window=True, flat=False, day_done=False, filtered=False,
                direction="long", positions=positions, resolved=[], context={},
                breakeven_at_r=None, breakeven_offset_points=0.0)
    base.update(over)
    return lambda m1, preset=None: base


def _pos(label="S007:1", entry=100.0, sl=90.0, tp=130.0, **over):
    p = dict(label=label, side="buy", entry=entry, sl=sl, tp=tp, stop0=sl,
             is_add=False, be_moved=False)
    p.update(over)
    return p


def _run(book, logger, plan, m1):
    return run_paper_cycle(book, symbol=SYMBOL, m1=m1, logger=logger,
                           preset="TEST", use_fixed_lot=True, fixed_lot=0.01,
                           fx_rate=1.0,
                           money_per_point_per_lot=MONEY_PER_POINT_PER_LOT,
                           initial_balance=book.initial_balance)


def test_a_planned_entry_becomes_a_paper_position(monkeypatch, logger):
    monkeypatch.setattr(s007_paper, "plan_now", _plan([_pos()]))
    book = PaperBook(10_000.0, money_per_point_per_lot=MONEY_PER_POINT_PER_LOT)
    out = _run(book, logger, None, _m1(3, high=101.0, low=99.0))

    assert out["open_positions"] == 1
    pos = book.positions["S007:1"]
    assert (pos["price"], pos["stop_loss"], pos["take_profit"]) == (100.0, 90.0, 130.0)
    assert pos["volume"] == 1.0  # 0.01 lots in raw broker units


def test_a_stop_out_is_booked_on_the_next_cycle(monkeypatch, logger):
    """Nothing in decide() closes a stopped-out position -- the broker does,
    server-side. With no broker, the book must do it, or the position would
    stay open forever."""
    monkeypatch.setattr(s007_paper, "plan_now", _plan([_pos()]))
    book = PaperBook(10_000.0, money_per_point_per_lot=MONEY_PER_POINT_PER_LOT)
    _run(book, logger, None, _m1(3, high=101.0, low=99.0))

    # Next cycle: price collapsed through the stop. The plan still wants the
    # position (plan_now replays the day and is stateless), but it must not be
    # reopened -- skip_reopen keys off the logged close.
    later = pd.concat([_m1(3, high=101.0, low=99.0),
                       _m1(2, high=95.0, low=85.0, start_min=3)])
    out = _run(book, logger, None, later)

    assert out["open_positions"] == 0
    assert [c["reason"] for c in book.closed] == ["stop_loss"]
    assert book.balance == 10_000.0 - 10.0  # 10 pts * 0.01 lots * 100
    assert "S007:1" not in book.positions


def test_day_done_flattens_the_book(monkeypatch, logger):
    monkeypatch.setattr(s007_paper, "plan_now", _plan([_pos()]))
    book = PaperBook(10_000.0, money_per_point_per_lot=MONEY_PER_POINT_PER_LOT)
    _run(book, logger, None, _m1(3, high=101.0, low=99.0))

    monkeypatch.setattr(s007_paper, "plan_now", _plan([], day_done=True))
    out = _run(book, logger, None, _m1(4, high=105.0, low=99.0))
    assert out["open_positions"] == 0
    assert book.closed[-1]["reason"] == "target"


def test_ghost_trades_are_never_opened(monkeypatch, logger):
    """`resolved` trades entered AND exited inside bars that had already
    elapsed -- the live bot could not have taken them, so the paper book must
    not either, or it would report a profitability the real one cannot reach."""
    ghost = dict(label="S007:99", side="buy", entry=100.0, sl=90.0, exit=110.0,
                 status="tp", is_add=False, is_recovery=False, r=1.0)
    monkeypatch.setattr(s007_paper, "plan_now", _plan([], resolved=[ghost]))
    book = PaperBook(10_000.0, money_per_point_per_lot=MONEY_PER_POINT_PER_LOT)
    out = _run(book, logger, None, _m1(3, high=101.0, low=99.0))
    assert out["open_positions"] == 0
    assert book.closed == []
    assert book.balance == 10_000.0


def test_the_daily_risk_cap_stops_the_paper_book_too(monkeypatch, logger):
    """The cap is a percentage of the book's STARTING balance, so a paper book
    hits it on the same trade a live account would."""
    # Each position risks 10 pts * 0.01 lots * 100 = $10; the cap is 2% of
    # 1000 = $20, so the third of three wanted positions must be refused.
    monkeypatch.setattr(s007_paper, "plan_now",
                        _plan([_pos(label=f"S007:{i}") for i in range(3)]))
    book = PaperBook(1_000.0, money_per_point_per_lot=MONEY_PER_POINT_PER_LOT)
    out = _run(book, logger, None, _m1(3, high=101.0, low=99.0))
    assert out["open_positions"] == 2


def test_the_cycle_persists_nothing_by_itself(monkeypatch, logger, tmp_path):
    """run_paper_cycle must not write the book -- saving is the daemon's job,
    so a failed cycle can't leave half-applied state on disk."""
    monkeypatch.setattr(s007_paper, "plan_now", _plan([_pos()]))
    book = PaperBook(10_000.0, money_per_point_per_lot=MONEY_PER_POINT_PER_LOT)
    _run(book, logger, None, _m1(3, high=101.0, low=99.0))
    assert list(tmp_path.glob("*.json")) == []


def test_paper_pnl_is_booked_in_account_currency(monkeypatch, logger):
    """The broker's lotSize is in the QUOTE currency (EUR for DE40) but the
    balance is in USD. decide() converts on its own, the book does not -- so
    the book must be built with the ALREADY-converted figure, and the two are
    not interchangeable. Getting this wrong understated every paper loss by
    ~14% and was caught only by a live end-to-end run (2026-09-25)."""
    monkeypatch.setattr(s007_paper, "plan_now", _plan([_pos()]))
    fx = 1.1427
    raw_lot_size = 100.0
    book = PaperBook(10_000.0, money_per_point_per_lot=raw_lot_size * fx)
    run_paper_cycle(book, symbol=SYMBOL, m1=_m1(3, high=101.0, low=99.0),
                    logger=logger, preset="TEST", use_fixed_lot=True,
                    fixed_lot=0.01, fx_rate=fx,
                    money_per_point_per_lot=raw_lot_size,  # RAW, decide converts
                    initial_balance=book.initial_balance)
    later = pd.concat([_m1(3, high=101.0, low=99.0),
                       _m1(2, high=95.0, low=85.0, start_min=3)])
    run_paper_cycle(book, symbol=SYMBOL, m1=later, logger=logger, preset="TEST",
                    use_fixed_lot=True, fixed_lot=0.01, fx_rate=fx,
                    money_per_point_per_lot=raw_lot_size,
                    initial_balance=book.initial_balance)
    # 10 points * 0.01 lots * 100 EUR/point/lot * 1.1427 = $11.427
    assert book.closed[-1]["pnl"] == pytest.approx(-11.427, abs=0.01)
    assert book.balance == pytest.approx(10_000.0 - 11.427, abs=1e-9)


def test_money_per_point_must_be_passed_explicitly():
    """No silent fallback to the book's value: it uses the other convention
    (converted), and picking it up by default would double-apply the FX rate."""
    book = PaperBook(10_000.0, money_per_point_per_lot=MONEY_PER_POINT_PER_LOT)
    with pytest.raises(TypeError):
        run_paper_cycle(book, symbol=SYMBOL, m1=_m1(1, high=1.0, low=1.0),
                        logger=None, preset="TEST")
