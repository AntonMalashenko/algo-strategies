"""Tests for the S007 paper-trading virtual book (ALGODEV-45 step 3).

The first test is the important one: it asserts the SAFETY INVARIANT -- that
no paper-trading module can reach an order-sending call -- as a test rather
than as a code review, because "I read it and there's no call" stops being
true the moment someone adds an import.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pandas as pd
import pytest

from bot.s007_paper_book import PaperBook

ROOT = Path(__file__).resolve().parent.parent

# The order-sending surface of the cTrader adapter. Any of these appearing in
# the paper path means paper trading could hit the real account.
ORDER_SENDING_NAMES = {
    "place_market", "_place_market_step", "amend_position_sltp",
    "_amend_position_sltp_step", "close_position", "_close_position_step",
    "run_live_cycle", "run_cycle_for_account",
}

PAPER_MODULES = ["bot/s007_paper_book.py", "bot/s007_paper_cycle.py"]


@pytest.mark.parametrize("rel", PAPER_MODULES)
def test_paper_modules_cannot_send_orders(rel):
    """No attribute access or call by any order-sending name, anywhere in the
    paper path. Checked on the AST so a name inside a comment or docstring
    (this file's own module docstring names several) doesn't trip it."""
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    used |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not (used & ORDER_SENDING_NAMES), (
        f"{rel} references order-sending call(s): {sorted(used & ORDER_SENDING_NAMES)}")


def _bars(rows):
    """rows: [(minute, high, low)] -> an m1 frame shaped like the adapter's."""
    idx = pd.to_datetime([f"2026-09-25 10:{m:02d}:00" for m, _, _ in rows])
    return pd.DataFrame(
        {"open": [(h + l) / 2 for _, h, l in rows],
         "high": [h for _, h, _ in rows],
         "low": [l for _, _, l in rows],
         "close": [(h + l) / 2 for _, h, l in rows]}, index=idx)


def _book(balance=10_000.0):
    return PaperBook(balance, money_per_point_per_lot=1.0)


def test_stop_wins_over_target_in_the_same_bar():
    """Mirrors engine.py::_simulate_leg -- a bar spanning both is a loss. A
    one-minute bar genuinely doesn't say which side printed first, and the
    paper book must not be luckier than the backtest that validated S007."""
    book = _book()
    book.place(label="S007:1", side="buy", fill=100.0, sl=90.0, tp=110.0,
               volume_lots=1.0, opened_ts=pd.Timestamp("2026-09-25 10:00:00"))
    closed = book.mark_fills(_bars([(1, 115.0, 85.0)]))
    assert [c["reason"] for c in closed] == ["stop_loss"]
    assert closed[0]["exit_price"] == 90.0


def test_bars_at_or_before_entry_cannot_resolve_a_position():
    """Replaying the entry bar would let a position resolve on price action
    that happened before its own order existed."""
    book = _book()
    opened = pd.Timestamp("2026-09-25 10:05:00")
    book.place(label="S007:1", side="buy", fill=100.0, sl=90.0, tp=110.0,
               volume_lots=1.0, opened_ts=opened)
    # Bars 00..05 blow through both levels; only 05 is the entry bar itself.
    assert book.mark_fills(_bars([(m, 120.0, 80.0) for m in range(6)])) == []
    assert "S007:1" in book.positions


def test_sell_side_levels_are_inverted():
    book = _book()
    book.place(label="S007:1", side="sell", fill=100.0, sl=110.0, tp=90.0,
               volume_lots=1.0, opened_ts=pd.Timestamp("2026-09-25 10:00:00"))
    closed = book.mark_fills(_bars([(1, 101.0, 89.0)]))
    assert closed[0]["reason"] == "take_profit"
    assert closed[0]["pnl"] == 10.0  # sold at 100, bought back at 90, 1 lot


def test_pnl_and_balance_follow_the_fill():
    book = _book(balance=10_000.0)
    book.money_per_point_per_lot = 2.0
    book.place(label="S007:1", side="buy", fill=100.0, sl=90.0, tp=110.0,
               volume_lots=0.5, opened_ts=pd.Timestamp("2026-09-25 10:00:00"))
    rec = book.mark_fills(_bars([(1, 95.0, 89.0)]))[0]
    assert rec["pnl"] == -10.0  # (90-100) * 0.5 lots * 2.0 per point
    assert book.balance == 9_990.0
    assert rec["balance_after"] == 9_990.0


def test_amend_moves_the_server_side_stop():
    book = _book()
    book.place(label="S007:1", side="buy", fill=100.0, sl=90.0, tp=110.0,
               volume_lots=1.0, opened_ts=pd.Timestamp("2026-09-25 10:00:00"))
    book.amend("S007:1", 100.0)
    closed = book.mark_fills(_bars([(1, 105.0, 99.0)]))
    assert closed[0]["reason"] == "stop_loss"
    assert closed[0]["pnl"] == 0.0  # breakeven, as intended


def test_amend_on_an_already_closed_position_is_not_an_error():
    """The same race exists against a real broker, which would reject it."""
    assert _book().amend("S007:nope", 100.0) is None


def test_broker_position_shape_is_what_decide_reads():
    """decide() divides `volume` by 100 to get lots and reads exactly these
    keys (bot/ctrader_s007.py::_parse_positions)."""
    book = _book()
    book.place(label="S007:1", side="buy", fill=100.0, sl=90.0, tp=110.0,
               volume_lots=0.01, opened_ts=pd.Timestamp("2026-09-25 10:00:00"))
    pos, = book.as_broker_positions()
    assert {"position_id", "label", "side", "volume", "price",
            "stop_loss", "take_profit", "opened_ts"} <= set(pos)
    assert pos["volume"] / 100.0 == 0.01


def test_as_broker_positions_hands_out_copies():
    """decide() must not be able to mutate the book by holding a dict."""
    book = _book()
    book.place(label="S007:1", side="buy", fill=100.0, sl=90.0, tp=110.0,
               volume_lots=1.0, opened_ts=pd.Timestamp("2026-09-25 10:00:00"))
    book.as_broker_positions()[0]["stop_loss"] = 1.0
    assert book.positions["S007:1"]["stop_loss"] == 90.0


def test_book_survives_a_restart(tmp_path):
    """A book that reset on every container restart would fake a clean slate
    after each crash and quietly bias the results."""
    path = tmp_path / "book.json"
    book = _book(balance=10_000.0)
    book.place(label="S007:1", side="buy", fill=100.0, sl=90.0, tp=110.0,
               volume_lots=1.0, opened_ts=pd.Timestamp("2026-09-25 10:00:00"))
    book.mark_fills(_bars([(1, 95.0, 89.0)]))  # realise a loss
    book.place(label="S007:2", side="sell", fill=200.0, sl=210.0, tp=190.0,
               volume_lots=1.0, opened_ts=pd.Timestamp("2026-09-25 10:02:00"))
    book.save(path)

    back = PaperBook.load(path, balance=999.0, money_per_point_per_lot=999.0)
    assert back.balance == book.balance  # NOT the 999.0 fallback
    assert back.initial_balance == 10_000.0
    assert len(back.closed) == 1
    assert set(back.positions) == {"S007:2"}
    # And the restored position still resolves -- its timestamp round-tripped.
    closed = back.mark_fills(_bars([(3, 211.0, 205.0)]))
    assert [c["reason"] for c in closed] == ["stop_loss"]


def test_restored_position_ids_do_not_collide(tmp_path):
    path = tmp_path / "book.json"
    book = _book()
    book.place(label="S007:1", side="buy", fill=100.0, sl=90.0, tp=None,
               volume_lots=1.0, opened_ts=pd.Timestamp("2026-09-25 10:00:00"))
    book.save(path)
    back = PaperBook.load(path, balance=0.0, money_per_point_per_lot=1.0)
    nxt = back.place(label="S007:2", side="buy", fill=100.0, sl=90.0, tp=None,
                     volume_lots=1.0, opened_ts=pd.Timestamp("2026-09-25 10:01:00"))
    assert nxt["position_id"] != book.positions["S007:1"]["position_id"]


def test_corrupt_state_is_quarantined_not_silently_reused(tmp_path):
    path = tmp_path / "book.json"
    path.write_text("{not json", encoding="utf-8")
    book = PaperBook.load(path, balance=5_000.0, money_per_point_per_lot=1.0)
    assert book.balance == 5_000.0
    assert path.with_suffix(".corrupt.json").exists(), "bad state must be kept"


def test_save_is_atomic(tmp_path):
    """Write-then-rename: a crash mid-write must not leave a truncated book."""
    path = tmp_path / "book.json"
    book = _book()
    book.save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["balance"] == 10_000.0
    assert not list(tmp_path.glob("*.tmp"))


def test_empty_bar_frame_resolves_nothing():
    book = _book()
    book.place(label="S007:1", side="buy", fill=100.0, sl=90.0, tp=110.0,
               volume_lots=1.0, opened_ts=pd.Timestamp("2026-09-25 10:00:00"))
    assert book.mark_fills(_bars([])) == []
    assert book.mark_fills(None) == []
