"""Standalone S021 order prices, computed entirely on Capital.com's own feed.

Start it any time before the open and leave it. It works out when the New York cash
open is (DST-aware, so it shifts by itself in the weeks when Kyiv and New York are not
in step), waits for it, pulls the history it needs, prints the order prices and exits.
No MT5, no EA, no numbers typed in.

Why this beats a public index feed: the opening price must come from the feed the orders
will actually sit on. Measured on 493 sessions, the cash index ^NDX opens within +-40
points of a broker's quote at the 5-95% range -- against a 67-point band half-width,
that is a third of the distance to the entry, because the index is still stale at
09:30:00 while its constituents open one by one. ADR14 carries across feeds (it is a
daily range, biased only ~2% between index and CFD); the open does not.

Credentials come from the environment (never passed on the command line, never logged):

    CAPITAL_API_KEY       the key generated in Settings > API integrations
    CAPITAL_IDENTIFIER    the account email
    CAPITAL_PASSWORD      the custom API password set when the key was created
    CAPITAL_DEMO          1 (default) for the demo endpoint, 0 for live

Usage:
    python -m tools.s021_capital                  # wait for the open, then print
    python -m tools.s021_capital --no-wait        # compute now, open must have passed
    python -m tools.s021_capital --search nasdaq  # find the right epic, then exit
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from tools.orb_levels import deliver, levels_from_adr
from strategies.orb_intraday.config import ORB_BASE

NY = ZoneInfo("America/New_York")
BASE_LIVE = "https://api-capital.backend-capital.com"
BASE_DEMO = "https://demo-api-capital.backend-capital.com"
API_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"      # the API filters on snapshotTimeUTC
HTTP_TIMEOUT_S = 30
REQUEST_SPACING_S = 0.15                   # the documented ceiling is 10 requests/second

HISTORY_RESOLUTION = "MINUTE_5"            # bar extremes are resolution-independent
HISTORY_MINUTES_PER_BAR = 5
HISTORY_LOOKBACK_DAYS = 30                 # calendar days scanned for ADR_WINDOW sessions
MAX_BARS_PER_REQUEST = 1000

OPEN_BAR_ATTEMPTS = 10                     # the 09:30 bar can lag the clock by a few seconds
OPEN_BAR_RETRY_S = 10
DEFAULT_OPEN_DELAY_S = 40

# A session the broker cut short (holiday half-day) is not a valid ADR day, same rule the
# engine applies in M1 bars -- scaled to this resolution.
MIN_SESSION_BARS = -(-ORB_BASE.min_session_bars // HISTORY_MINUTES_PER_BAR)


class CapitalError(RuntimeError):
    pass


class CapitalSession:
    """One authenticated Capital.com session. Tokens live 10 minutes per the docs."""

    def __init__(self, api_key: str, identifier: str, password: str, demo: bool = True):
        self._base = BASE_DEMO if demo else BASE_LIVE
        self._http = requests.Session()
        self._http.headers["X-CAP-API-KEY"] = api_key
        response = self._http.post(f"{self._base}/api/v1/session", timeout=HTTP_TIMEOUT_S,
                                   json={"identifier": identifier, "password": password})
        if not response.ok:
            raise CapitalError(f"login failed: HTTP {response.status_code} {response.text[:200]}")
        self._http.headers["CST"] = response.headers["CST"]
        self._http.headers["X-SECURITY-TOKEN"] = response.headers["X-SECURITY-TOKEN"]

    def _get(self, path: str, **params) -> dict:
        time.sleep(REQUEST_SPACING_S)
        response = self._http.get(f"{self._base}{path}", params=params, timeout=HTTP_TIMEOUT_S)
        if not response.ok:
            raise CapitalError(f"GET {path} -> HTTP {response.status_code} {response.text[:200]}")
        return response.json()

    def search(self, term: str) -> list[tuple[str, str]]:
        markets = self._get("/api/v1/markets", searchTerm=term).get("markets", [])
        return [(m["epic"], m["instrumentName"]) for m in markets]

    def bars(self, epic: str, start: datetime, end: datetime, resolution: str) -> list[dict]:
        """Bid OHLC bars in [start, end), both UTC."""
        payload = self._get(f"/api/v1/prices/{epic}", resolution=resolution,
                            max=MAX_BARS_PER_REQUEST,
                            **{"from": start.strftime(API_TIME_FORMAT),
                               "to": end.strftime(API_TIME_FORMAT)})
        return [
            {
                "time": datetime.strptime(bar["snapshotTimeUTC"][:19], API_TIME_FORMAT),
                "open": bar["openPrice"]["bid"],
                "high": bar["highPrice"]["bid"],
                "low": bar["lowPrice"]["bid"],
            }
            for bar in payload.get("prices", [])
        ]


def session_window_utc(day: date) -> tuple[datetime, datetime]:
    """The engine's 09:30-15:59 New York session as a half-open UTC interval."""
    start = datetime.combine(day, ORB_BASE.session_open, tzinfo=NY)
    end = datetime.combine(day, ORB_BASE.session_close, tzinfo=NY) + timedelta(minutes=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def session_ranges(api: CapitalSession, epic: str, before: date, wanted: int) -> list[tuple[date, float]]:
    """The last `wanted` valid session ranges strictly before `before`, oldest first."""
    found: list[tuple[date, float]] = []
    for back in range(1, HISTORY_LOOKBACK_DAYS + 1):
        day = before - timedelta(days=back)
        if day.weekday() >= 5:
            continue
        start, end = session_window_utc(day)
        bars = api.bars(epic, start, end, HISTORY_RESOLUTION)
        if len(bars) < MIN_SESSION_BARS:
            continue                       # holiday, half day, or a feed gap
        found.append((day, max(b["high"] for b in bars) - min(b["low"] for b in bars)))
        if len(found) == wanted:
            break
    found.reverse()
    return found


def open_price(api: CapitalSession, epic: str, day: date) -> float:
    """The open of the 09:30 New York minute, retried while the bar is still forming."""
    start, _ = session_window_utc(day)
    for attempt in range(OPEN_BAR_ATTEMPTS):
        bars = api.bars(epic, start, start + timedelta(minutes=1), "MINUTE")
        if bars:
            return bars[0]["open"]
        if attempt < OPEN_BAR_ATTEMPTS - 1:
            time.sleep(OPEN_BAR_RETRY_S)
    raise CapitalError(f"no 09:30 bar for {day} -- a holiday, or the market is closed")


def wait_for_open(day: date, delay_s: int) -> None:
    target = datetime.combine(day, ORB_BASE.session_open, tzinfo=NY) + timedelta(seconds=delay_s)
    remaining = (target - datetime.now(NY)).total_seconds()
    if remaining <= 0:
        return
    print(f"waiting for the open: {target.astimezone():%H:%M:%S} local "
          f"/ {target:%H:%M:%S} New York ({remaining / 60:.0f} min)", flush=True)
    time.sleep(remaining)


def connect(demo_override: bool | None = None) -> CapitalSession:
    load_dotenv()
    missing = [name for name in ("CAPITAL_API_KEY", "CAPITAL_IDENTIFIER", "CAPITAL_PASSWORD")
               if not os.environ.get(name)]
    if missing:
        raise CapitalError(f"missing in the environment: {', '.join(missing)} (see .env.example)")
    demo = os.environ.get("CAPITAL_DEMO", "1") != "0" if demo_override is None else demo_override
    return CapitalSession(os.environ["CAPITAL_API_KEY"], os.environ["CAPITAL_IDENTIFIER"],
                          os.environ["CAPITAL_PASSWORD"], demo=demo)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--epic", default="US100", help="Capital.com instrument epic")
    parser.add_argument("--search", metavar="TERM", help="list matching epics and exit")
    parser.add_argument("--no-wait", action="store_true", help="do not wait for the open")
    parser.add_argument("--open-delay", type=int, default=DEFAULT_OPEN_DELAY_S,
                        help="seconds to let the 09:30 bar settle before reading it")
    parser.add_argument("--telegram", action="store_true",
                        help="also send the plan to Telegram (see utils/telegram.py)")
    args = parser.parse_args()

    try:
        if args.search:
            api = connect()
            for epic, name in api.search(args.search):
                print(f"{epic:<20} {name}")
            return 0

        # Login AFTER the wait, not before: a Capital.com token lives 10 minutes (see
        # CapitalSession's docstring), but the wait for the open can run up to ~85
        # minutes in winter (s021_signal.yml's own worst case) -- logging in first would
        # hand session_ranges()/open_price() a token that expired long before their
        # first request, failing every scheduled run with HTTP 401.
        today = datetime.now(NY).date()
        if not args.no_wait:
            wait_for_open(today, args.open_delay)
        api = connect()

        ranges = session_ranges(api, args.epic, today, ORB_BASE.adr_window)
        if len(ranges) < ORB_BASE.adr_window:
            raise CapitalError(f"only {len(ranges)} valid sessions in the last "
                               f"{HISTORY_LOOKBACK_DAYS} days, need {ORB_BASE.adr_window}")
        adr = sum(r for _, r in ranges) / len(ranges)
        levels = levels_from_adr(open_price(api, args.epic, today), adr)
    except CapitalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    deliver(levels, today, f"{args.epic}, {len(ranges)} sessions "
                           f"{ranges[0][0]}..{ranges[-1][0]}", args.telegram)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
