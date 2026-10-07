"""Order parameters for trading S021 by hand, on an account the EA does not run on.

Only the session open O is broker-specific. Every other number in the rule is a
DISTANCE measured off ADR14 -- the bands are O +/- k_range*ADR14 and the stop is
stop_adr_mult*ADR14 -- and ADR14 is the mean daily high-low of the same underlying
index, so it carries across brokers even when their quotes sit hundreds of points
apart (dividend adjustments, financing). That is why nothing here needs quotes to be
"synchronised": take ADR14 from the EA's live log and apply it to the open price YOUR
platform printed at 09:30 New York.

ADR14 comes from the newest "levels" event the EA wrote (it logs one at 09:30:10 New
York every session), so it is current by construction. Use --adr when the EA is not
running. The geometry/formatting this shares with tools/s021_capital.py (the feed-only
alternative) lives in tools/orb_levels.py; what is specific to this tool is reading the
EA's own log files, below.

Usage:
    python -m mt5.tools.s021_manual --open 31264.50
    python -m mt5.tools.s021_manual --open 31264.50 --adr 334.278571
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from tools.orb_levels import NY, both_clocks, deliver, levels_from_adr
from strategies.orb_intraday.config import ORB_BASE

# MT5 under Wine on macOS: the terminal's shared Common\Files folder.
COMMON_FILES = (Path.home() / "Library/Application Support/net.metaquotes.wine.metatrader5"
                / "drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files")
EVENTS_GLOB = "AlgoTrading/logs/S021-mt5-acct*/events-*.jsonl"


def latest_levels(common_files: Path) -> dict | None:
    """Newest "levels" event across every S021 account log, or None if there is none.

    Ordered by the file's DATE first (its name is events-YYYY-MM-DD.jsonl) and only then
    by account folder: a plain reverse sort of the full paths is dominated by the account
    number, so the highest-numbered account's yesterday would beat today's real session.
    """
    for path in sorted(common_files.glob(EVENTS_GLOB), key=lambda p: (p.name, p), reverse=True):
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("kind") == "levels":
                return event
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--open", dest="open_price", type=float, required=True,
                        help="the 09:30 New York open as YOUR platform printed it")
    parser.add_argument("--adr", type=float, default=None,
                        help="ADR14 override; default: newest levels event in the EA log")
    parser.add_argument("--logs", type=Path, default=COMMON_FILES,
                        help="MT5 Common\\Files folder holding the EA logs")
    parser.add_argument("--telegram", action="store_true",
                        help="also send the plan to Telegram (see utils/telegram.py)")
    args = parser.parse_args()

    today = datetime.now(NY).date()
    source = f"--adr {args.adr}"
    adr = args.adr
    if adr is None:
        event = latest_levels(args.logs)
        if event is None:
            parser.error(f"no levels event under {args.logs / EVENTS_GLOB}; pass --adr instead")
        adr = float(event["ADR14"])
        source = f"EA log, session {event['day']}"
        if event["day"] != today.isoformat():
            source += f" -- NOT today ({today}); rerun after {both_clocks(today, ORB_BASE.session_open)}"

    levels = levels_from_adr(args.open_price, adr)
    deliver(levels, today, source, args.telegram)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
