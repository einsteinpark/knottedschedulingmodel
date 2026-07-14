"""
Sales analyzer.

Takes raw hourly sales from Toast and produces:
  1. A clean dataset with outliers and manually-ignored dates removed
  2. An hourly demand profile keyed by (day_of_week, hour) that projects
     forward into next week's schedule
  3. Auto-derived FOH / BOH staffing thresholds based on labor target

Outlier detection: per day-of-week, compute mean and stddev of TOTAL daily sales,
then z-score each day. Days with |z| > OUTLIER_ZSCORE_CUTOFF are flagged.
The user gets a printable summary of what was excluded.
"""

from __future__ import annotations

import statistics
import logging
from datetime import date, timedelta
from typing import Dict, List, Tuple
from collections import defaultdict
from dataclasses import dataclass

import config

log = logging.getLogger(__name__)


@dataclass
class HourlyProjection:
    """Projected sales for a single (day_of_week, hour) slot."""
    day_of_week: int       # 0=Mon
    hour: int              # 0-23
    foh_sales: float       # avg donut/pastry/beverage $
    boh_sales: float       # avg savory $
    total_sales: float     # foh + boh + other


@dataclass
class OutlierReport:
    auto_excluded: List[Tuple[date, float, float]]   # (date, total_$, zscore)
    manually_excluded: List[date]


def project_hourly_demand(
    hourly_sales: Dict[Tuple[date, int], Dict[str, float]],
) -> Tuple[Dict[Tuple[int, int], HourlyProjection], OutlierReport]:
    """
    Args:
        hourly_sales: output of ToastClient.fetch_hourly_sales

    Returns:
        ({(dow, hour): HourlyProjection}, OutlierReport)
    """
    # 1) Roll up to daily totals so we can outlier-detect
    daily_totals: Dict[date, float] = defaultdict(float)
    for (d, _h), buckets in hourly_sales.items():
        daily_totals[d] += buckets.get("total", 0.0)

    # 2) Apply manual ignore list
    manually_excluded = set()
    for s in config.MANUAL_IGNORE_DATES:
        try:
            manually_excluded.add(date.fromisoformat(s))
        except ValueError:
            log.warning("Bad MANUAL_IGNORE_DATES entry: %r", s)

    # 3) Auto-detect outliers WITHIN each day-of-week bucket
    by_dow: Dict[int, List[Tuple[date, float]]] = defaultdict(list)
    for d, total in daily_totals.items():
        if d in manually_excluded:
            continue
        by_dow[d.weekday()].append((d, total))

    auto_excluded: List[Tuple[date, float, float]] = []
    excluded_dates = set(manually_excluded)
    for dow, items in by_dow.items():
        if len(items) < 3:
            continue  # not enough data to z-score
        totals = [t for _, t in items]
        mean = statistics.mean(totals)
        sd = statistics.pstdev(totals) or 1.0
        for d, t in items:
            z = (t - mean) / sd
            if abs(z) > config.OUTLIER_ZSCORE_CUTOFF:
                auto_excluded.append((d, t, z))
                excluded_dates.add(d)

    # 4) Build the projection: average each (dow, hour) across kept days
    by_slot: Dict[Tuple[int, int], List[Dict[str, float]]] = defaultdict(list)
    for (d, h), buckets in hourly_sales.items():
        if d in excluded_dates:
            continue
        by_slot[(d.weekday(), h)].append(buckets)

    projection: Dict[Tuple[int, int], HourlyProjection] = {}
    for (dow, h), samples in by_slot.items():
        foh = sum(s.get("foh", 0.0) for s in samples) / len(samples)
        boh = sum(s.get("boh", 0.0) for s in samples) / len(samples)
        tot = sum(s.get("total", 0.0) for s in samples) / len(samples)
        projection[(dow, h)] = HourlyProjection(
            day_of_week=dow, hour=h, foh_sales=foh, boh_sales=boh, total_sales=tot,
        )

    report = OutlierReport(
        auto_excluded=sorted(auto_excluded, key=lambda x: x[0]),
        manually_excluded=sorted(manually_excluded),
    )
    return projection, report


def derive_foh_thresholds(
    projection: Dict[Tuple[int, int], HourlyProjection],
    avg_foh_wage: float,
) -> List[Tuple[float, int]]:
    """
    Compute hourly FOH-sales thresholds for adding the Nth person.

    Logic (throughput-based, not labor-breakeven):
      Add the Nth person when projected hourly pastry/beverage sales exceed
      what (N-1) people can comfortably handle. We anchor "comfortable
      throughput per person" to the 75th-percentile single-person hour in
      historical data — i.e. what an opener actually handles solo when busy.

    This empirical anchor is then bracketed by the labor-target floor (don't
    add a person when their slice of sales wouldn't pay their wage at target%)
    so we don't overstaff dead hours.

    Returns thresholds list: [(min_sales, headcount), ...] sorted ascending.
    """
    target = config.LABOR_TARGET_PCT or 0.28
    labor_floor_per_person = avg_foh_wage / target  # e.g. $21 / 0.28 = $75/hr

    # Empirical throughput anchor:
    # Use the 25th percentile of OPEN-hour FOH sales across all (dow, hour) slots.
    # This captures what a typical SLOW hour looks like — i.e. what one barista
    # comfortably handles solo. We then say: add a 2nd person when sales exceed
    # ~2x that, a 3rd at 3x, etc.
    open_hour_sales: List[float] = []
    for (dow, hr), p in projection.items():
        hours_cfg = config.HOURS_BY_DOW.get(dow)
        if not hours_cfg:
            continue
        if hours_cfg.open_time.hour <= hr < hours_cfg.close_time.hour:
            open_hour_sales.append(p.foh_sales)

    if open_hour_sales and len(open_hour_sales) >= 8:
        open_hour_sales.sort()
        # 25th percentile = "slow open hour" = solo throughput proxy
        idx = int(0.25 * (len(open_hour_sales) - 1))
        slow_hour_anchor = open_hour_sales[idx]
        # Don't anchor below the labor floor (otherwise we overstaff dead hours)
        throughput_per_person = max(slow_hour_anchor, labor_floor_per_person)
    else:
        # Not enough data — default to a sensible cafe heuristic
        throughput_per_person = max(120.0, labor_floor_per_person)

    # Add the Nth person when cumulative demand exceeds (N-1) people's capacity
    # plus a 25% buffer (avoid adding right at the breaking point).
    BUFFER = 1.25

    thresholds = [(0.0, 1)]
    for n in range(2, 5):  # support up to 4 people
        threshold = (n - 1) * throughput_per_person * BUFFER
        # Also enforce labor floor: the Nth person's slice must cover their wage
        threshold = max(threshold, (n - 1) * labor_floor_per_person)
        thresholds.append((round(threshold, 2), n))

    log.info(
        "Derived FOH thresholds (avg wage $%.2f, target %.0f%%, throughput $%.0f/p): %s",
        avg_foh_wage, target * 100, throughput_per_person, thresholds,
    )
    return thresholds


def derive_boh_thresholds(
    projection: Dict[Tuple[int, int], HourlyProjection],
    avg_boh_wage: float,
) -> List[Tuple[float, int]]:
    """
    BOH thresholds are DAILY (not hourly): based on total daily savory sales.
    Each cook is 8hrs * wage; we want savory sales >= cooks * 8 * wage / target.
    """
    target = config.LABOR_TARGET_PCT or 0.28
    per_cook_daily_min_sales = (config.BOH_SHIFT_HOURS * avg_boh_wage) / target
    SAFETY = 1.20

    thresholds = [(0.0, 1)]
    cumulative = per_cook_daily_min_sales * SAFETY
    for n in range(2, config.BOH_MAX_COOKS + 1):
        thresholds.append((round(cumulative, 2), n))
        cumulative += per_cook_daily_min_sales * SAFETY * 0.9

    log.info(
        "Derived BOH daily thresholds (avg wage $%.2f): %s",
        avg_boh_wage, thresholds,
    )
    return thresholds


def required_foh_for_hour(
    projected_foh_sales: float, thresholds: List[Tuple[float, int]]
) -> int:
    """How many FOH people do we need at this projected hourly sales level?"""
    needed = 1
    for min_sales, count in thresholds:
        if projected_foh_sales >= min_sales:
            needed = count
    return needed


def required_boh_for_day(
    projected_daily_boh_sales: float,
    thresholds: List[Tuple[float, int]],
    day_of_week: int,
) -> int:
    """How many cooks for a given day's projected savory sales?"""
    needed = 1
    for min_sales, count in thresholds:
        if projected_daily_boh_sales >= min_sales:
            needed = count
    floor = (
        config.BOH_MIN_COOKS_WEEKEND if day_of_week in (4, 5)
        else config.BOH_MIN_COOKS_WEEKDAY
    )
    return max(needed, floor)
