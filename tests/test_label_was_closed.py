"""Fix 1 regression test: StrategyLogger.label_was_closed -- the idempotency
guard against re-placing a position whose own log already recorded a close,
even when the broker's reconcile no longer lists it (see decisions-log.md
2026-07-21: a real stop-out beat a lagging M1 bar to the punch and the bot
reopened the same label as a brand-new position)."""
from __future__ import annotations

from utils.trade_logger import StrategyLogger


def test_unknown_label_is_not_closed(tmp_path):
    log = StrategyLogger("S007TEST", log_root=str(tmp_path), console=False)
    assert log.label_was_closed("S007:2026-07-21:999") is False


def test_open_only_label_is_not_closed(tmp_path):
    log = StrategyLogger("S007TEST", log_root=str(tmp_path), console=False)
    label = "S007:2026-07-21:0"
    log.position(label, "open", side="buy", entry=100.0, sl=95.0, tp=110.0, is_add=False)
    assert log.label_was_closed(label) is False


def test_closed_label_is_closed(tmp_path):
    log = StrategyLogger("S007TEST", log_root=str(tmp_path), console=False)
    label = "S007:2026-07-21:0"
    log.position(label, "open", side="buy", entry=100.0, sl=95.0, tp=110.0, is_add=False)
    log.position(label, "close", reason="target")
    assert log.label_was_closed(label) is True


def test_closed_label_stays_closed_across_a_fresh_logger_instance(tmp_path):
    # The real bot is a brand-new process every cycle (see bot/s007_paper.py
    # module docstring) -- the guard MUST work from disk, not in-memory state.
    label = "S007:2026-07-21:74"
    first = StrategyLogger("S007TEST", log_root=str(tmp_path), console=False)
    first.position(label, "open", side="buy", entry=24865.1, sl=24850.35, tp=24975.4, is_add=True)
    first.position(label, "close", reason="stop")

    second = StrategyLogger("S007TEST", log_root=str(tmp_path), console=False)
    assert second.label_was_closed(label) is True
