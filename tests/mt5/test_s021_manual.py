"""Manual-assist levels: the geometry must match what the EA logged live."""
from __future__ import annotations

import json
from datetime import date

import pytest

from mt5.tools.s021_manual import latest_levels, levels_from_adr, telegram_message

# The real 2026-10-06 levels event, and the fill/stop the EA actually traded off it.
LIVE_EVENT = {"ts": "2026-10-06T16:30:10", "strategy": "S021-mt5-acct53084682",
              "kind": "levels", "day": "2026-10-06", "O": 31264.5, "ADR14": 334.278571,
              "U": 31331.355714, "L": 31197.644286, "stop_dist": 250.708929,
              "sessions_available": 30}
LIVE_LONG_STOP_LOSS = 31080.646785


def test_levels_reproduce_the_live_event():
    levels = levels_from_adr(LIVE_EVENT["O"], LIVE_EVENT["ADR14"])
    assert levels["upper"] == pytest.approx(LIVE_EVENT["U"], abs=1e-6)
    assert levels["lower"] == pytest.approx(LIVE_EVENT["L"], abs=1e-6)
    assert levels["stop_distance"] == pytest.approx(LIVE_EVENT["stop_dist"], abs=1e-6)
    assert levels["long_stop_loss"] == pytest.approx(LIVE_LONG_STOP_LOSS, abs=1e-6)


def test_stop_losses_sit_one_stop_distance_inside_the_bands():
    levels = levels_from_adr(20_000.0, 300.0)
    assert levels["upper"] - levels["long_stop_loss"] == pytest.approx(levels["stop_distance"])
    assert levels["short_stop_loss"] - levels["lower"] == pytest.approx(levels["stop_distance"])


def test_a_foreign_open_moves_the_bands_but_not_the_distances():
    """The whole point: another broker's quote shifts O only, the geometry is unchanged."""
    ours = levels_from_adr(31_264.5, 334.278571)
    theirs = levels_from_adr(31_264.5 - 420.0, 334.278571)
    assert theirs["upper"] == pytest.approx(ours["upper"] - 420.0)
    assert theirs["stop_distance"] == pytest.approx(ours["stop_distance"])


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


def test_latest_levels_returns_none_when_the_ea_never_logged(tmp_path):
    assert latest_levels(tmp_path) is None


def test_telegram_message_puts_every_order_price_in_a_copyable_span():
    levels = levels_from_adr(LIVE_EVENT["O"], LIVE_EVENT["ADR14"])
    message = telegram_message(levels, date(2026, 10, 7), "US100, 14 sessions")
    for value in (levels["upper"], levels["lower"],
                  levels["long_stop_loss"], levels["short_stop_loss"]):
        assert f"<code>{value:.2f}</code>" in message
    assert "cancel the other" in message


def test_telegram_message_escapes_the_source_label():
    levels = levels_from_adr(20_000.0, 300.0)
    message = telegram_message(levels, date(2026, 10, 7), "feed <b>spoof</b>")
    assert "feed &lt;b&gt;spoof&lt;/b&gt;" in message
