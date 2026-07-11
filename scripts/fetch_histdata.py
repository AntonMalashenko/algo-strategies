"""Download 1-minute ASCII bar data directly from histdata.com.

Standalone replacement for `histdatacom -f ascii -t 1-minute-bar-quotes`,
which currently ships with a Temporal-based orchestration runtime that gets
stuck starting its worker lanes on this machine. This script replicates the
same page-scrape-then-POST flow the histdatacom package uses internally,
without any orchestration:

  1. GET the archive page for a pair/period to extract the hidden download
     form fields (tk, date, datemonth, platform, timeframe, fxpair).
  2. POST those fields to /get.php to receive the ZIP archive.
  3. Extract DAT_ASCII_<PAIR>_M1_<period>.csv from the ZIP into data/histdata/.

histdata.com only offers whole-year M1 archives for past years and
month-by-month archives for the current year, so this fetches full years for
[start_year, this_year) and individual months for the current year.

Usage:
    python scripts/fetch_histdata.py -p eurusd gbpusd usdjpy usdchf audusd eurjpy gbpjpy -s 2022-03
    python scripts/convert_histdata.py
"""
from __future__ import annotations

import argparse
import io
import sys
import time
import zipfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "histdata"
BASE_URL = "http://www.histdata.com/download-free-forex-data/"
GET_URL = "http://www.histdata.com/get.php"
FORM_FIELDS = ("tk", "date", "datemonth", "platform", "timeframe", "fxpair")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")

GET_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

POST_HEADERS = {
    "Origin": "http://www.histdata.com",
    "Content-Type": "application/x-www-form-urlencoded",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "User-Agent": UA,
}


class _FormParser(HTMLParser):
    """Collect input values from histdata.com's file_down form."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_form = False
        self.seen_form = False
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "form" and a.get("id") == "file_down":
            self.in_form = True
            self.seen_form = True
            return
        if tag.lower() != "input" or not self.in_form:
            return
        fid = a.get("id")
        if fid:
            self.values[fid] = a.get("value", "")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag.lower() == "form" and self.in_form:
            self.in_form = False


def periods_for_range(start_year: int) -> list[tuple[str, str]]:
    """Return (url_path, tag) pairs: full past years then current-year months."""
    now = datetime.now(timezone.utc)
    periods = [(str(y), str(y)) for y in range(start_year, now.year)]
    periods += [
        (f"{now.year}/{m}", f"{now.year}{m:02d}") for m in range(1, now.month + 1)
    ]
    return periods


def fetch_period(session: requests.Session, pair: str, url_path: str) -> bytes | None:
    page_url = f"{BASE_URL}?/ascii/1-minute-bar-quotes/{pair}/{url_path}"
    r = session.get(page_url, headers=GET_HEADERS, timeout=30)
    r.raise_for_status()
    parser = _FormParser()
    parser.feed(r.text)
    parser.close()
    if not parser.seen_form or not parser.values.get("tk"):
        print(f"  no data: {pair}/{url_path}")
        return None
    form = {k: parser.values.get(k, "") for k in FORM_FIELDS}
    headers = dict(POST_HEADERS)
    headers["Referer"] = page_url
    resp = session.post(GET_URL, data=form, headers=headers, timeout=60)
    resp.raise_for_status()
    if len(resp.content) < 1000 and b"PK" not in resp.content[:4]:
        print(f"  unexpected response for {pair}/{url_path}: {resp.content[:200]!r}")
        return None
    return resp.content


def fetch_pairs(pairs: list[str], start_year: int, *, refetch_current_month: bool = False) -> None:
    """Download every missing (pair, period) file into OUT.

    With refetch_current_month=True, the current year/month period is always
    re-downloaded even if a file for it already exists, since that period is
    still accumulating bars on histdata.com until the month closes. Used by
    the monthly refresh job (scripts/refresh_monthly.py) to top up the
    in-progress month; the one-off CLI below leaves it False.
    """
    periods = periods_for_range(start_year)
    now = datetime.now(timezone.utc)
    current_tag = f"{now.year}{now.month:02d}"
    OUT.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    for pair in pairs:
        pair = pair.lower()
        print(f"=== {pair} ===")
        for url_path, tag in periods:
            force = refetch_current_month and tag == current_tag
            existing = list(OUT.glob(f"DAT_ASCII_{pair.upper()}_M1_{tag}*.csv"))
            if existing and not force:
                print(f"  skip {pair}/{tag} (exists)")
                continue
            try:
                zip_bytes = fetch_period(session, pair, url_path)
            except Exception as e:
                print(f"  FAIL {pair}/{url_path}: {e}")
                continue
            if zip_bytes is None:
                continue
            try:
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    for name in zf.namelist():
                        if name.upper().endswith(".CSV"):
                            data = zf.read(name)
                            out_name = f"DAT_ASCII_{pair.upper()}_M1_{tag}.csv"
                            (OUT / out_name).write_bytes(data)
                            print(f"  OK {out_name}  {len(data) / 1e6:.1f} MB")
            except zipfile.BadZipFile:
                print(f"  bad zip for {pair}/{url_path}")
            time.sleep(1.0)

    print(f"\nDone -> {OUT}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-p", "--pairs", nargs="+", required=True)
    ap.add_argument("-s", "--start", required=True, help="YYYY-MM")
    args = ap.parse_args()

    start_year = int(args.start.split("-")[0])
    fetch_pairs(args.pairs, start_year)


if __name__ == "__main__":
    sys.exit(main())
