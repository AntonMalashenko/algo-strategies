"""No-network test for webapp/sync_positions.py's reconciliation core.

apply_snapshot() is deliberately pure: it takes the snapshot dict that
bot/ctrader_s007.py::sync_snapshot would have returned and folds it into the
session, without touching the broker and without committing. That is what
makes the interesting cases (a position that vanished, a partial close, a
stranger's trade on the account) testable at all -- none of them can be
provoked on demand against a live demo server.

Run: python test_sync_positions.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="synctest-")
os.environ["APP_DB_URL"] = f"sqlite:///{TMP}/t.db"
os.environ.setdefault("APP_SECRET_KEY", "test-only-not-a-real-key")

from webapp.db import Base, engine, get_session          # noqa: E402
from webapp.models import Account, Broker, Position, Strategy, User  # noqa: E402
from webapp.sync_positions import (                      # noqa: E402
    REASON_BROKER_CLOSED, _aggregate_deals, apply_snapshot)

FAILURES: list[str] = []
NOW = datetime(2026, 7, 23, 12, 0, 0)


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def seed():
    Base.metadata.create_all(engine)
    s = get_session()
    u = User(username="t", password_hash="x", is_admin=True)
    broker = Broker(name="IC Markets", platforms="CTRADER")
    s.add_all([u, broker])
    s.flush()
    acc = Account(user_id=u.id, broker="CTRADER", broker_id=broker.id,
                  external_account_id="111", env="demo", label="demo")
    s007 = Strategy(name="S007", broker="CTRADER")
    s009 = Strategy(name="S009", broker="BYBIT")
    s.add_all([acc, s007, s009])
    s.commit()
    return s, acc, s007, s009


def add_pos(s, acc, strat, label, **kw):
    p = Position(account_id=acc.id, strategy_id=strat.id, label=label,
                 side=kw.pop("side", "buy"), entry=kw.pop("entry", 100.0),
                 sl=kw.pop("sl", 90.0), tp=kw.pop("tp", 120.0),
                 is_add=False, status="open",
                 opened_at=kw.pop("opened_at", NOW - timedelta(hours=2)), **kw)
    s.add(p)
    s.commit()
    return p


def main() -> int:
    s, acc, s007, s009 = seed()

    # ---- 1. still open at the broker -> refreshed, id captured -------------
    print("\n1. open position refreshed from broker truth")
    live = add_pos(s, acc, s007, "S007-D1")          # runner never set an id
    snap = dict(positions=[dict(position_id=9001, label="S007-D1", side="buy",
                                volume=10000, price=101.25, stop_loss=95.5,
                                take_profit=0.0, symbol_id=1, symbol="GER40",
                                volume_lots=0.1, opened_ts=ms(NOW - timedelta(hours=2)))],
                deals=[])
    n = apply_snapshot(s, acc, s007, snap, now=NOW)
    s.flush()
    check("counted as refreshed", n["refreshed"] == 1, str(n))
    check("broker_position_id captured", live.broker_position_id == 9001)
    check("entry overwritten with fill price", live.entry == 101.25)
    check("sl overwritten", live.sl == 95.5)
    check("tp NOT zeroed by broker's 0", live.tp == 120.0, f"tp={live.tp}")
    check("volume_lots filled", live.volume_lots == 0.1)
    check("still open", live.status == "open")
    check("synced_at stamped", live.synced_at == NOW)

    # ---- 2. vanished with a matching deal -> closed AND priced -------------
    print("\n2. vanished position priced from its closing deal")
    closed_ts = NOW - timedelta(minutes=30)
    snap2 = dict(positions=[], deals=[dict(
        deal_id=555, position_id=9001, symbol_id=1, symbol="GER40", side="buy",
        exit_price=118.0, entry_price=101.25, closed_volume=10000,
        volume_lots=0.1, gross_profit=16.75, swap=-0.4, commission=-1.35,
        pnl=15.0, balance_after=10015.0, executed_ms=ms(closed_ts))])
    n = apply_snapshot(s, acc, s007, snap2, now=NOW)
    s.flush()
    check("counted closed+priced", n["closed"] == 1 and n["priced"] == 1, str(n))
    check("status closed", live.status == "closed")
    check("reason broker_closed", live.reason == REASON_BROKER_CLOSED)
    check("exit_price from deal", live.exit_price == 118.0)
    check("pnl from deal", live.pnl == 15.0)
    check("components kept separately",
          (live.gross_profit, live.swap, live.commission) == (16.75, -0.4, -1.35))
    check("broker_deal_id stored", live.broker_deal_id == 555)
    check("closed_at from deal timestamp", live.closed_at == closed_ts.replace(tzinfo=None))

    # ---- 3. vanished with NO broker id -> closed, money stays NULL ---------
    print("\n3. vanished position with no broker id closes with NULL money")
    orphan = add_pos(s, acc, s007, "S007-D2")
    n = apply_snapshot(s, acc, s007, dict(positions=[], deals=[]), now=NOW)
    s.flush()
    check("counted closed, not priced", n["closed"] == 1 and n["priced"] == 0, str(n))
    check("status closed", orphan.status == "closed")
    check("pnl stays NULL (unknown != 0.0)", orphan.pnl is None)
    check("exit_price stays NULL", orphan.exit_price is None)
    check("closed_at falls back to now", orphan.closed_at == NOW)

    # ---- 4. partial closes aggregate into one result -----------------------
    print("\n4. two partial closing deals sum into one position result")
    part = add_pos(s, acc, s007, "S007-D3")
    part.broker_position_id = 9002
    s.commit()
    t1, t2 = NOW - timedelta(minutes=20), NOW - timedelta(minutes=5)
    deals = [
        dict(deal_id=601, position_id=9002, symbol_id=1, side="buy",
             exit_price=110.0, entry_price=100.0, closed_volume=5000,
             volume_lots=0.05, gross_profit=5.0, swap=-0.1, commission=-0.5,
             pnl=4.4, executed_ms=ms(t1)),
        dict(deal_id=602, position_id=9002, symbol_id=1, side="buy",
             exit_price=112.0, entry_price=100.0, closed_volume=5000,
             volume_lots=0.05, gross_profit=6.0, swap=-0.1, commission=-0.5,
             pnl=5.4, executed_ms=ms(t2)),
    ]
    agg = _aggregate_deals(deals)[9002]
    check("money summed", round(agg["pnl"], 6) == 9.8, str(agg["pnl"]))
    check("volume summed", round(agg["volume_lots"], 6) == 0.1)
    check("last deal wins for exit_price", agg["exit_price"] == 112.0)
    check("last deal wins for deal_id", agg["deal_id"] == 602)
    n = apply_snapshot(s, acc, s007, dict(positions=[], deals=deals), now=NOW)
    s.flush()
    check("row priced with the sum", round(part.pnl, 6) == 9.8)
    check("closed_at is the LAST partial", part.closed_at == t2.replace(tzinfo=None))

    # ---- 5. unknown broker position adopted exactly once -------------------
    print("\n5. stranger position adopted once, never twice")
    stranger = dict(position_id=9100, label="", side="sell", volume=20000,
                    price=200.0, stop_loss=210.0, take_profit=190.0,
                    symbol_id=2, symbol="US500", volume_lots=0.2,
                    opened_ts=ms(NOW - timedelta(days=1)))
    n = apply_snapshot(s, acc, s007, dict(positions=[stranger], deals=[]), now=NOW)
    s.commit()
    check("counted adopted", n["adopted"] == 1, str(n))
    row = s.query(Position).filter_by(broker_position_id=9100).one()
    check("origin=adopted", row.origin == "adopted")
    check("deterministic label", row.label == "adopted:9100")
    check("opened_at from broker", row.opened_at == (NOW - timedelta(days=1)))
    check("sl/tp carried over", (row.sl, row.tp) == (210.0, 190.0))

    n = apply_snapshot(s, acc, s007, dict(positions=[stranger], deals=[]), now=NOW)
    s.commit()
    check("second pass adopts nothing", n["adopted"] == 0, str(n))
    check("second pass refreshes it instead", n["refreshed"] == 1, str(n))
    check("still exactly one row",
          s.query(Position).filter_by(broker_position_id=9100).count() == 1)

    # ---- 6. another strategy's labelled position is left alone ------------
    print("\n6. a position labelled for another strategy is skipped")
    foreign = dict(position_id=9200, label="S009-D9", side="buy", volume=1000,
                   price=50.0, stop_loss=45.0, take_profit=0.0, symbol_id=3,
                   symbol="BTCUSDT", volume_lots=0.01, opened_ts=ms(NOW))
    before = s.query(Position).count()
    n = apply_snapshot(s, acc, s007, dict(positions=[stranger, foreign], deals=[]),
                       now=NOW)
    s.commit()
    check("counted as skipped", n["skipped"] == 1, str(n))
    check("no row created for it", s.query(Position).count() == before)

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
