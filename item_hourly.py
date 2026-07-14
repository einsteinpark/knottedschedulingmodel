"""
Item-level hourly data loader.

Reads Item_hourly.csv (date,hour,item,qty) and produces per-DOW per-hour
averages for tracked items (Breakfast Sandwich, Chicken Caesar Wrap).

Honors config.MANUAL_IGNORE_DATES so holidays/anomalies don't skew the
typical-day averages.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Dict, Tuple, Set, List
import csv

import config


TRACKED_ITEMS = ["Breakfast Sandwich", "Chicken Caesar Wrap"]
# Display labels (shorter for compact heatmap rows)
ITEM_LABELS = {
    "Breakfast Sandwich": "Bfast Sand",
    "Chicken Caesar Wrap": "Caesar Wrap",
}


def _ignored_dates() -> Set[date]:
    s: Set[date] = set()
    for entry in getattr(config, "MANUAL_IGNORE_DATES", []):
        try:
            s.add(date.fromisoformat(entry))
        except (ValueError, TypeError):
            continue
    return s


def load_item_hourly(
    data_dir: Path,
    date_from: date = None,
    date_to: date = None,
) -> Dict[Tuple[int, int, str], float]:
    """
    Returns {(dow, hour, item): avg_qty_per_day_of_week}

    Reads Item_hourly.csv and averages quantities for each (DOW, hour, item),
    excluding manually-ignored dates.

    Optional date_from/date_to (inclusive) filter the rows considered.
    Useful for computing a "last week only" baseline distinct from the
    8-week historical baseline.

    Also tracks the number of days seen per DOW so the average is correct
    (some weeks may have one occurrence of a DOW, others might have multiple).
    """
    file = data_dir / "Item_hourly.csv"
    if not file.exists():
        return {}

    ignored = _ignored_dates()

    # (dow, hour, item) -> [qty, qty, ...]
    bucket: Dict[Tuple[int, int, str], List[float]] = defaultdict(list)
    days_seen_per_dow: Dict[int, Set[date]] = defaultdict(set)

    with file.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                d = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            if d in ignored:
                continue
            if date_from and d < date_from:
                continue
            if date_to and d > date_to:
                continue
            try:
                h = int(row["hour"])
                qty = float(row["qty"])
            except (KeyError, ValueError):
                continue
            item = row["item"]
            if item not in TRACKED_ITEMS:
                continue
            bucket[(d.weekday(), h, item)].append(qty)
            days_seen_per_dow[d.weekday()].add(d)

    # Compute average per DOW: total qty / number of distinct days of that DOW
    result: Dict[Tuple[int, int, str], float] = {}
    for (dow, h, item), qtys in bucket.items():
        n_days = len(days_seen_per_dow[dow])
        if n_days == 0:
            continue
        result[(dow, h, item)] = sum(qtys) / n_days

    return result


def load_item_daily_totals(data_dir: Path) -> Dict[Tuple[int, str], float]:
    """
    Returns {(dow, item): avg_qty_per_day_of_week} — daily totals (sum over hours).
    """
    hourly = load_item_hourly(data_dir)
    daily: Dict[Tuple[int, str], float] = defaultdict(float)
    for (dow, h, item), qty in hourly.items():
        daily[(dow, item)] += qty
    return dict(daily)


def item_daily_by_date(
    data_dir: Path,
    cutoff: date = None,
) -> Dict[Tuple[date, str], float]:
    """{(date, item): qty/day} for tracked items, honoring MANUAL_IGNORE_DATES.

    If `cutoff` is given, only dates strictly before it are returned (so realized
    current-week days don't pollute item baselines/lift)."""
    file = data_dir / "Item_hourly.csv"
    if not file.exists():
        return {}
    ignored = _ignored_dates()
    out: Dict[Tuple[date, str], float] = defaultdict(float)
    with file.open() as f:
        for row in csv.DictReader(f):
            try:
                d = date.fromisoformat(row["date"])
            except (KeyError, ValueError):
                continue
            if d in ignored:
                continue
            if cutoff and d >= cutoff:
                continue
            item = row.get("item")
            if item not in TRACKED_ITEMS:
                continue
            try:
                out[(d, item)] += float(row["qty"])
            except (KeyError, ValueError):
                continue
    return dict(out)
