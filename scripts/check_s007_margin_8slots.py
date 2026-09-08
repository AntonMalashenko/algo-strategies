"""ALGODEV-37 pre-flight: can this account hold 8 simultaneous GER40 positions?

Read-only margin check for the "maximally prop" scheme (8 slots @ 0.25%/R,
WORKING_S007_NEWSSAFE_MAX8_BE05): asks the BROKER's own margin engine
(ProtoOAExpectedMarginReq) what one typical S007 position costs in margin,
multiplies by 8 and compares against the account balance. Places NO orders,
modifies nothing -- safe to run against the live demo account any time.

The lot size used is the typical S007 size at RISK_PCT=0.25%: risk_amount /
(stop_distance * money_per_point_per_lot * FX), evaluated for a range of
realistic stop distances (S007 mid-range stops historically run ~20-80 pts),
floored at the broker minimum like bot/risk.py::lots_for_risk does.

Usage: python3 scripts/check_s007_margin_8slots.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twisted.internet import defer

from bot import s007_config as C
from bot.ctrader_s007 import CTraderS007
from bot.risk import lots_for_risk

from ctrader_open_api import Protobuf
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExpectedMarginReq

N_SLOTS = 8
# representative S007 stop distances in points (historical mid-range stops)
STOP_DISTANCES_PTS = [20.0, 40.0, 80.0]


def main():
    api = CTraderS007()

    def work(done):
        @defer.inlineCallbacks
        def flow():
            yield api._load_symbols()
            up = {n.upper(): n for n in api._symbols.keys()}
            symbol = next((up[c.upper()] for c in C.SYMBOL_CANDIDATES if c.upper() in up), None)
            if symbol is None:
                raise RuntimeError(f"none of {C.SYMBOL_CANDIDATES} found")
            full = yield api._get_full_symbol_step(symbol)
            balance = yield api._get_balance_step()
            money_ppl = full.lotSize * C.EUR_TO_USD_FX_RATE_APPROX
            risk_amount = balance * C.RISK_PCT / 100.0

            print(f"symbol={symbol}  balance={balance:.2f}  "
                  f"risk/pos={risk_amount:.2f} ({C.RISK_PCT}%)")
            rows = []
            for sd in STOP_DISTANCES_PTS:
                lot = lots_for_risk(risk_amount, sd, money_ppl, min_lot=C.FIXED_LOT)
                volume = api._volume_from_lots(lot, full)
                req = ProtoOAExpectedMarginReq()
                req.ctidTraderAccountId = api.account
                req.symbolId = full.symbolId
                req.volume.append(volume)
                d = api.client.send(req)
                d.addCallback(api._check_response)
                res = yield d
                scale = 10.0 ** (res.moneyDigits or 2)
                # buyMargin/sellMargin inside each ProtoOAExpectedMargin entry
                m = res.margin[0]
                margin_1 = max(m.buyMargin, m.sellMargin) / scale
                rows.append((sd, lot, margin_1))
            return dict(balance=balance, rows=rows)

        d = flow()
        d.addCallbacks(lambda r: done(r), lambda f: done(error=f))

    out = api._run(work)
    balance = out["balance"]
    print(f"\n{'stop(pts)':>10} {'lot':>8} {'margin/pos':>12} {'x{n}':>12} {'% of balance':>14}"
          .format(n=N_SLOTS))
    worst = 0.0
    for sd, lot, m1 in out["rows"]:
        m8 = m1 * N_SLOTS
        pct = 100.0 * m8 / balance if balance else float("nan")
        worst = max(worst, pct)
        print(f"{sd:>10.0f} {lot:>8.3f} {m1:>12.2f} {m8:>12.2f} {pct:>13.1f}%")
    print(f"\nworst case: {N_SLOTS} slots need {worst:.1f}% of balance as margin.")
    if worst < 50:
        print("VERDICT: comfortable -- margin will not block the 8-slot scheme.")
    elif worst < 90:
        print("VERDICT: tight -- workable, but a losing day shrinks free margin; monitor.")
    else:
        print("VERDICT: NOT viable -- the broker would start rejecting adds. "
              "Reduce size or slots before switching the preset.")


if __name__ == "__main__":
    main()
