"""s021_manual: reading the EA's own log files for ADR14, the part that genuinely needs MT5.

The geometry/formatting it shares with tools/s021_capital.py is covered in
tests/tools/test_orb_levels.py.
"""
from __future__ import annotations

import json

import pytest

from mt5.tools.s021_manual import latest_levels

# The real 2026-10-06 levels event, and the fill/stop the EA actually traded off it.
LIVE_EVENT = {"ts": "2026-10-06T16:30:10", "strategy": "S021-mt5-acct53084682",
              "kind": "levels", "day": "2026-10-06", "O": 31264.5, "ADR14": 334.278571,
              "U": 31331.355714, "L": 31197.644286, "stop_dist": 250.708929,
              "sessions_available": 30}


def test_latest_levels_picks_the_newest_event_and_ignores_other_kinds(tmp_path):
    log_dir = tmp_path / "AlgoTrading/logs/S021-mt5-acct1"
    log_dir.mkdir(parents=True)
    (log_dir / "events-2026-10-05.jsonl").write_text(
        json.dumps({"kind": "levels", "day": "2026-10-05", "ADR14": 272.757143}) + "\n",
        encoding="utf-8")
    (log_dir / "events-2026-10-06.jsonl").write_text(
        json.dumps(LIVE_EVENT) + "\n" + json.dumps({"kind": "fill", "day": "2026-10-06"}) + "\n",
        encoding="utf-8")
    assert latest_levels(tmp_path)["ADR14"] == pytest.approx(LIVE_EVENT["ADR14"])


def test_latest_levels_prefers_todays_session_over_a_higher_numbered_account(tmp_path):
    """Two accounts log side by side: the newest DATE wins, not the newest account number.

    The live case (2026-10-07): acct53084682 was detached before the open and its freshest
    file was yesterday's, while acct21007756 logged today -- a path-ordered sort handed the
    manual plan yesterday's ADR14.
    """
    logs = tmp_path / "AlgoTrading/logs"
    stale = logs / "S021-mt5-acct53084682"
    stale.mkdir(parents=True)
    (stale / "events-2026-10-06.jsonl").write_text(json.dumps(LIVE_EVENT) + "\n",
                                                   encoding="utf-8")
    (stale / "events-2026-10-07.jsonl").write_text(
        json.dumps({"kind": "deinit", "reason": 6}) + "\n", encoding="utf-8")
    live = logs / "S021-mt5-acct21007756"
    live.mkdir(parents=True)
    (live / "events-2026-10-07.jsonl").write_text(
        json.dumps({"kind": "levels", "day": "2026-10-07", "ADR14": 308.147857}) + "\n",
        encoding="utf-8")

    event = latest_levels(tmp_path)
    assert event["day"] == "2026-10-07"
    assert event["ADR14"] == pytest.approx(308.147857)


def test_latest_levels_returns_none_when_the_ea_never_logged(tmp_path):
    assert latest_levels(tmp_path) is None
