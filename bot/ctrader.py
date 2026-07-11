"""cTrader Open API adapter (skeleton).

Implementation notes for going live on the IC Markets cTrader DEMO:

1. pip install ctrader-open-api   (Spotware OpenApiPy, twisted-based)
2. Create an application at https://openapi.ctrader.com -> client id/secret.
3. Authorise your demo account (OAuth playground on the same site) to get
   an access token; put credentials into environment variables (see
   bot/config.py docstring).
4. Endpoints used: demo.ctraderapi.com:5035 (protobuf over TLS).

Responsibilities of this adapter (all TODO):
  - get_m15(symbol, days)      -> pandas OHLCV in RAW POINTS + server tz
                                  (ProtoOAGetTrendbarsReq, M15)
  - list_orders()/list_positions()
  - place_limit(order)         -> with SL/TP attached (ProtoOANewOrderReq)
  - cancel(order_id)
  - account_balance()

The paper runner talks ONLY to this interface, so the dry-run mode works
without any of it installed.
"""
from __future__ import annotations


class CTraderAdapter:
    def __init__(self):
        raise NotImplementedError(
            "cTrader transport not wired yet: install ctrader-open-api, "
            "fill credentials env vars, then implement per the notes above.")
