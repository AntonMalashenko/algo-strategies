"""Paper-bot configuration — frozen per strategy passport S004 v1.0.

Do NOT tune these values outside the experiments log. Broker credentials
come from environment variables (never commit them):

    CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET, CTRADER_ACCESS_TOKEN,
    CTRADER_ACCOUNT_ID  (the demo account id)
"""
from __future__ import annotations

PAIRS = ["GBPJPY", "EURUSD", "USDCHF", "GBPUSD", "EURJPY", "USDJPY", "AUDUSD"]

# engine parameters (passport §3)
MODE = "base"
STOP = "zone"
RR = 3.0
PIP_RAW = 10.0            # ejtrader/histdata point convention: 1 pip = 10 raw
SPREAD_PIPS = 0.9         # engine bookkeeping only; real costs come from broker
BUFFER_PIPS = 2.0         # engine constant, here for reference

# session window (server/EET hours): orders live only inside this window
ASIA_START_H = 0
ASIA_END_H = 7            # exclusive

RISK_PCT = 0.5            # % of balance risked per trade (passport §3)
MAX_CONCURRENT = 4        # portfolio cap (passport §3)

HISTORY_DAYS = 60         # M15 lookback to rebuild engine state each run
PAPER_LOG_DIR = "reports/paper"
