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
running.

Usage:
    python -m mt5.tools.s021_manual --open 31264.50
    python -m mt5.tools.s021_manual --open 31264.50 --adr 334.278571
"""
from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from strategies.orb_intraday.config import ORB_BASE

NY = ZoneInfo("America/New_York")

# MT5 under Wine on macOS: the terminal's shared Common\Files folder.
COMMON_FILES = (Path.home() / "Library/Application Support/net.metaquotes.wine.metatrader5"
                / "drive_c/users/user/AppData/Roaming/MetaQuotes/Terminal/Common/Files")
EVENTS_GLOB = "AlgoTrading/logs/S021-mt5-acct*/events-*.jsonl"


def latest_levels(common_files: Path) -> dict | None:
    """Newest "levels" event across every S021 account log, or None if there is none."""
    for path in sorted(common_files.glob(EVENTS_GLOB), reverse=True):
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("kind") == "levels":
                return event
    return None


def levels_from_adr(open_price: float, adr: float, cfg=ORB_BASE) -> dict:
    """The frozen ORB_BASE geometry around one session open."""
    half = cfg.k_range * adr
    stop_distance = cfg.stop_adr_mult * adr
    upper = open_price + half
    lower = open_price - half
    return {
        "open": open_price,
        "adr": adr,
        "upper": upper,
        "lower": lower,
        "stop_distance": stop_distance,
        "long_stop_loss": upper - stop_distance,
        "short_stop_loss": lower + stop_distance,
    }


def _both_clocks(ny_date, clock_time) -> str:
    ny = datetime.combine(ny_date, clock_time, tzinfo=NY)
    local = ny.astimezone()
    return f"{local:%H:%M} local / {ny:%H:%M} New York"


def telegram_message(levels: dict, ny_date, source: str) -> str:
    """The same plan as print_plan, shaped for a phone: every number is tap-to-copy and
    sits on its own line, so a tap grabs the number and nothing else."""
    from utils.telegram import code

    buy = code(f"{levels['upper']:.2f}")
    buy_sl = code(f"{levels['long_stop_loss']:.2f}")
    sell = code(f"{levels['lower']:.2f}")
    sell_sl = code(f"{levels['short_stop_loss']:.2f}")
    distance = code(f"{levels['stop_distance']:.2f}")
    open_price = code(f"{levels['open']:.2f}")
    place = html.escape(_both_clocks(ny_date, ORB_BASE.session_open))
    cutoff = html.escape(_both_clocks(ny_date, ORB_BASE.entry_cutoff))
    close = html.escape(_both_clocks(ny_date, ORB_BASE.session_close))

    return "\n".join([
        f"<b>S021 — {ny_date}</b>",
        f"<i>{html.escape(source)}</i>",
        "",
        "🟢 <b>BUY STOP</b>",
        buy,
        f"stop loss {buy_sl}",
        "",
        "🔴 <b>SELL STOP</b>",
        sell,
        f"stop loss {sell_sl}",
        "",
        f"open {open_price} · stop distance {distance}",
        f"ADR14 {levels['adr']:.2f}",
        "",
        f"⏰ {place} — place both",
        "⚠️ one fills → <b>cancel the other</b>",
        f"⏱ {cutoff} — cancel if unfilled",
        f"🏁 {close} — close by the clock",
    ])


def print_plan(levels: dict, ny_date, source: str) -> None:
    """The levels block and the by-hand checklist, shared by every S021 manual tool."""
    print(f"S021 manual levels -- {ny_date} New York")
    print(f"ADR14 {levels['adr']:.3f}   ({source})")
    print()
    print(f"  your open  O   {levels['open']:>12.2f}")
    print(f"  buy stop   U   {levels['upper']:>12.2f}   stop loss {levels['long_stop_loss']:.2f}")
    print(f"  sell stop  L   {levels['lower']:>12.2f}   stop loss {levels['short_stop_loss']:.2f}")
    print(f"  stop distance  {levels['stop_distance']:>12.2f}")
    print()
    print("  the EA does none of this for you here -- by hand:")
    print(f"    1. at {_both_clocks(ny_date, ORB_BASE.session_open)}, place both stop orders "
          "with their stop losses")
    print("    2. when one fills, CANCEL THE OTHER (no OCO: both filling doubles your risk)")
    print(f"    3. cancel both if unfilled by {_both_clocks(ny_date, ORB_BASE.entry_cutoff)}")
    print(f"    4. close whatever is open at {_both_clocks(ny_date, ORB_BASE.session_close)}")


def deliver(levels: dict, ny_date, source: str, to_telegram: bool) -> None:
    """Print the plan, and optionally push the phone-shaped copy to Telegram."""
    print_plan(levels, ny_date, source)
    if not to_telegram:
        return
    from utils import telegram

    if telegram.send(telegram_message(levels, ny_date, source)):
        print("\nsent to Telegram")
    else:
        print("\nTelegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")


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
            source += f" -- NOT today ({today}); rerun after {_both_clocks(today, ORB_BASE.session_open)}"

    levels = levels_from_adr(args.open_price, adr)
    deliver(levels, today, source, args.telegram)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
