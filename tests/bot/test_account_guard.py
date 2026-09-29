"""bot/account_guard.py -- account-level loss guard (ALGODEV-55) -- plus the
S007 new-risk gate wrapper that applies it (bot/s007_paper.py::_gate_new_risk)."""
from __future__ import annotations

from datetime import datetime, timezone

from bot.account_guard import (REASON_DAILY, REASON_MAX, AccountLimits, check_new_risk,
                               day_start_ms, realized_pnl_since)
from bot.s007_paper import _gate_new_risk

# 2026-09-29 12:00 UTC == 14:00 Prague (CEST, UTC+2) -> Prague midnight = 22:00 UTC the day before
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
PRAGUE_MIDNIGHT_MS = int(datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc).timestamp() * 1000)
FTMO = AccountLimits(initial_balance=10_000, daily_loss_pct=4.0, max_loss_pct=8.0,
                     day_reset_tz="Europe/Prague")


def _deal(pnl, ms):
    return dict(pnl=pnl, executed_ms=ms)


def test_day_start_is_local_midnight_of_the_reset_tz():
    assert day_start_ms("Europe/Prague", NOW) == PRAGUE_MIDNIGHT_MS


def test_realized_counts_only_deals_after_the_boundary():
    deals = [_deal(-100, PRAGUE_MIDNIGHT_MS - 1), _deal(-50, PRAGUE_MIDNIGHT_MS),
             _deal(20, PRAGUE_MIDNIGHT_MS + 5)]
    assert realized_pnl_since(deals, PRAGUE_MIDNIGHT_MS) == -30


def test_no_limits_or_inactive_limits_always_allow():
    assert check_new_risk(None, balance=1, closed_deals=[], open_risk=1e9, new_risk=1e9).allowed
    off = AccountLimits(initial_balance=10_000)
    assert check_new_risk(off, balance=1, closed_deals=[], open_risk=1e9, new_risk=1e9).allowed


def test_fresh_day_within_budget_is_allowed():
    v = check_new_risk(FTMO, balance=10_000, closed_deals=[], open_risk=150, new_risk=25, now=NOW)
    assert v.allowed and v.details["worst_day_loss"] == 175


def test_daily_guard_counts_realized_loss_plus_open_plus_new():
    # lost $300 today already, $75 still at risk, next trade risks $50 -> 425 > 400
    deals = [_deal(-300, PRAGUE_MIDNIGHT_MS + 60_000)]
    v = check_new_risk(FTMO, balance=9_700, closed_deals=deals, open_risk=75, new_risk=50, now=NOW)
    assert not v.allowed and v.reason == REASON_DAILY
    assert v.details["day_start_balance"] == 10_000


def test_yesterdays_losses_do_not_count_against_today():
    deals = [_deal(-300, PRAGUE_MIDNIGHT_MS - 60_000)]
    v = check_new_risk(FTMO, balance=9_700, closed_deals=deals, open_risk=75, new_risk=50, now=NOW)
    assert v.allowed


def test_daily_profit_gives_no_extra_room_beyond_the_budget():
    # +$200 today: worst loss from the day's START is (-200) + 0 + 650 = 450 > 400
    deals = [_deal(200, PRAGUE_MIDNIGHT_MS + 1)]
    v = check_new_risk(FTMO, balance=10_200, closed_deals=deals, open_risk=0, new_risk=650, now=NOW)
    assert not v.allowed and v.reason == REASON_DAILY


def test_max_loss_guard_uses_static_initial_balance():
    # account already down to $9,250 over previous days: floor $9,200
    v = check_new_risk(FTMO, balance=9_250, closed_deals=[], open_risk=25, new_risk=50, now=NOW)
    assert not v.allowed and v.reason == REASON_MAX


class _Log:
    def __init__(self):
        self.events, self.orders = [], []

    def event(self, kind, **kw):
        self.events.append((kind, kw))

    def order(self, label, kind, **kw):
        self.orders.append((label, kind, kw))


def _place(label, lot=1.0, entry=100.0, sl=90.0):
    return dict(kind="place", label=label, side="buy", sl=sl, tp=120.0,
                volume_lots=lot, entry=entry, is_add=False)


def _run_gate(actions, *, mode, limits=None, balance=10_000):
    log = _Log()

    def decide(*_a, **_kw):
        return list(actions)
    gated = _gate_new_risk(decide, broker_mode=mode, account_limits=limits, magic="S007",
                           fx_rate=1.0, logger=log, cid="c1")
    out = gated("GER40", None, [], balance, 1.0, closed_deals=[])
    return out, log


def test_gate_execute_passes_places_and_closes():
    close = dict(kind="close", label="S007:x", position_id=1, volume=100, reason="flat")
    out, _ = _run_gate([_place("S007:a"), close], mode="execute")
    assert [a["kind"] for a in out] == ["place", "close"]


def test_gate_dry_logs_places_and_keeps_closes():
    close = dict(kind="close", label="S007:x", position_id=1, volume=100, reason="flat")
    out, log = _run_gate([_place("S007:a"), close], mode="dry")
    assert [a["kind"] for a in out] == ["close"]
    assert log.orders and log.orders[0][2]["result"] == "dry-run"


def test_gate_off_drops_places_silently():
    out, log = _run_gate([_place("S007:a")], mode="off")
    assert out == [] and log.orders == []


def test_gate_account_guard_accumulates_risk_within_the_cycle():
    # each place risks 1 lot * 10 pts * $1 = $10... scaled: lot=20 -> $200 each; budget $400
    limits = AccountLimits(initial_balance=10_000, daily_loss_pct=4.0)
    out, log = _run_gate([_place("S007:a", lot=20), _place("S007:b", lot=20),
                          _place("S007:c", lot=20)], mode="execute", limits=limits)
    assert [a["label"] for a in out] == ["S007:a", "S007:b"]
    assert log.events[0][0] == "skip_account_guard"
