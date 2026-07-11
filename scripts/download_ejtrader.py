"""Download ALL historical data from ejtraderLabs/historical-data (GitHub).

Run from a machine with internet access (e.g. PyCharm terminal):

    python scripts/download_ejtrader.py            # everything (all symbols, all TFs)
    python scripts/download_ejtrader.py --tf m15 h4  # only selected timeframes
    python scripts/download_ejtrader.py --symbols EURUSD GBPUSD --tf m15

Files are saved to data/raw/<SYMBOL>/<original_filename> (spaces stripped),
matching the layout already used in this repo.

Uses the GitHub API to list repo folders/files, so it is robust to naming
differences. Unauthenticated API limit is 60 requests/hour — one listing
request per symbol folder, which is fine for this repo (~12 folders).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

REPO = "ejtraderLabs/historical-data"
API_ROOT = f"https://api.github.com/repos/{REPO}/contents"
OUT = Path(__file__).resolve().parent.parent / "data" / "raw"
UA = {"User-Agent": "algo-data-downloader"}  # GitHub API requires a UA header


def api_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def download(url: str, dest: Path) -> int:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    dest.write_bytes(data)
    return len(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=None,
                    help="only these symbols (default: all folders in repo)")
    ap.add_argument("--tf", nargs="*", default=None,
                    help="only these timeframes, e.g. m15 h1 d1 (default: all)")
    args = ap.parse_args()

    print(f"Listing folders in {REPO} ...")
    root = api_json(API_ROOT)
    folders = [e["name"] for e in root if e["type"] == "dir"]
    if args.symbols:
        want = {s.upper() for s in args.symbols}
        folders = [f for f in folders if f.upper() in want]
    print(f"Symbols: {', '.join(folders) or '(none matched)'}")

    total_files = 0
    total_bytes = 0
    for sym in folders:
        try:
            entries = api_json(f"{API_ROOT}/{sym}")
        except Exception as e:
            print(f"FAIL list {sym}: {e}")
            continue
        for e in entries:
            if e["type"] != "file" or not e["name"].lower().endswith(".csv"):
                continue
            if args.tf and not any(tf.lower() in e["name"].lower() for tf in args.tf):
                continue
            dest_dir = OUT / sym
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / e["name"].replace(" ", "")
            if dest.exists() and dest.stat().st_size == e["size"]:
                print(f"skip {dest.relative_to(OUT.parent.parent)} (up to date)")
                continue
            try:
                n = download(e["download_url"], dest)
                total_files += 1
                total_bytes += n
                print(f"OK   {dest.relative_to(OUT.parent.parent)}  {n/1e6:.1f} MB")
            except Exception as ex:
                print(f"FAIL {sym}/{e['name']}: {ex}")
            time.sleep(0.2)  # be polite

    print(f"\nDone: {total_files} files, {total_bytes/1e6:.1f} MB -> {OUT}")


if __name__ == "__main__":
    sys.exit(main())
