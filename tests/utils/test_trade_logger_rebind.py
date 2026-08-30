"""StrategyLogger text-handler rebinding across log_root changes.

Regression coverage for the 2026-08 production-log pollution (ALGODEV-24):
logging.getLogger() caches by name process-wide, so the FIRST
StrategyLogger("<name>", log_root=A) pinned the rotating text handler to
A/<name>/<name>.log forever — a later StrategyLogger("<name>", log_root=B)
kept writing its TEXT stream to A while its JSONL streams went to B. In one
pytest process, importing scripts/s009_tick.py at collection time (module-
level StrategyLogger("S009") with the real reports/logs root) meant the
isolated run_once() tests still leaked fake cycles into the LIVE S009.log
on every full-suite run.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.trade_logger import StrategyLogger  # noqa: E402


def test_second_log_root_rebinds_text_handler(tmp_path):
    first = StrategyLogger("REBINDTEST", log_root=str(tmp_path / "rootA"), console=False)
    first.info("written to A")

    second = StrategyLogger("REBINDTEST", log_root=str(tmp_path / "rootB"), console=False)
    second.info("written to B")

    log_a = tmp_path / "rootA" / "REBINDTEST" / "REBINDTEST.log"
    log_b = tmp_path / "rootB" / "REBINDTEST" / "REBINDTEST.log"
    assert "written to A" in log_a.read_text()
    assert "written to B" not in log_a.read_text()   # the leak this test guards against
    assert "written to B" in log_b.read_text()


def test_same_log_root_keeps_single_handler(tmp_path):
    a = StrategyLogger("SAMEROOT", log_root=str(tmp_path), console=False)
    b = StrategyLogger("SAMEROOT", log_root=str(tmp_path), console=False)
    assert a.log is b.log
    assert len([h for h in b.log.handlers]) == 1     # no duplicate handlers
    b.info("once")
    text = (tmp_path / "SAMEROOT" / "SAMEROOT.log").read_text()
    assert text.count("once") == 1
