"""Sort raw CSV files into per-instrument subdirectories.

Usage:
    python3 scripts/sort_raw_data.py
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

# Matches filenames like EURUSDd1.csv, AUDUSDh4.csv (instrument + timeframe suffix).
_PAIR_TIMEFRAME = re.compile(r"^(?P<instrument>[A-Z0-9]+?)(?P<timeframe>[dmhw]\d+)$", re.ASCII)

DATA_RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


def instrument_from_stem(stem: str) -> str:
    """Derive an instrument/folder name from a CSV stem.

    Examples
    --------
    >>> instrument_from_stem("EURUSDd1")
    'EURUSD'
    >>> instrument_from_stem("SP500")
    'SP500'
    """
    match = _PAIR_TIMEFRAME.fullmatch(stem)
    if match:
        return match.group("instrument")
    # No timeframe suffix: treat the whole stem as the instrument name.
    return stem


def sort_raw(root: Path = DATA_RAW) -> list[tuple[str, str]]:
    """Move all flat CSV files in *root* into per-instrument subdirectories."""
    moved: list[tuple[str, str]] = []

    for csv_path in sorted(root.glob("*.csv")):
        instrument = instrument_from_stem(csv_path.stem)
        target_dir = root / instrument
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / csv_path.name
        shutil.move(str(csv_path), str(target))
        moved.append((csv_path.name, target.relative_to(root).as_posix()))

    return moved


def main() -> None:
    moved = sort_raw()
    if moved:
        for src, dst in moved:
            print(f"  {src} -> {dst}")
        print(f"\nMoved {len(moved)} file(s).")
    else:
        print("Nothing to move — all CSV files are already inside instrument folders.")

    print("\nCurrent structure:")
    for item in sorted(DATA_RAW.iterdir()):
        if item.is_dir():
            files = ", ".join(sorted(c.name for c in item.glob("*.csv")))
            print(f"  {item.name}/: {files}")
        elif item.suffix == ".csv":
            print(f"  {item.name}  (not sorted)")


if __name__ == "__main__":
    main()

