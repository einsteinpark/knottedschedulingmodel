"""
Build the hourly demand projection from Toast CSV exports.

Toast's "summary" reports each give us a DIFFERENT slice of the same data:
  - Sales_by_day.csv         : daily totals (date -> $)
  - Time_of_day__totals_.csv : hourly totals across ALL days summed together
  - Day_of_week__totals_.csv : day-of-week totals across all weeks summed
  - Sales_category_summary.csv : category breakdown across all data summed

We combine them as follows to produce a per-(dow, hour) projection:

  1. hour_share(h) = hourly_total[h] / sum(hourly_total)
     => fraction of a typical day's sales that happens in hour h
  2. dow_factor(dow) = avg_daily_sales[dow] / overall_avg_daily_sales
     => Saturday is 2.2x a Monday, etc.
  3. typical_daily_sales = mean(daily_totals after outliers excluded)
  4. projected_sales(dow, h) = typical_daily_sales * dow_factor(dow) * hour_share(h)
  5. foh_share, boh_share = computed from category totals
  6. projected_foh(dow, h) = projected_sales(dow, h) * foh_share
  7. projected_boh(dow, h) = projected_sales(dow, h) * boh_share

This is an approximation: it assumes the FOH/BOH ratio is constant across
hours and days. That's reasonable for a cafe (donuts in the morning vs.
sandwiches at lunch would violate it; we can refine later if needed).
"""

from __future__ import annotations

import csv
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from dataclasses import dataclass

# Reuse the existing data classes from the sales_analyzer module
from sales_analyzer import HourlyProjection, OutlierReport

import config


# Map Toast "Sales Category" names -> our FOH/BOH/other buckets
TOAST_CATEGORY_BUCKETS = {
    "Beverage - Coffee":         "foh",
    "Beverage - Other":          "foh",
    "Food - Donuts":             "foh",
    "Retail":                    "foh",
    "Food - Other":              "boh",   # confirmed: savory items
    "No Sales Category Assigned":"other",
    "Fees":                      "other",
}


@dataclass
class TastFiles:
    """Container for the paths Toast gives us in a sales export."""
    sales_by_day: Path
    time_of_day: Path
    day_of_week: Path
    sales_category: Path


def _read_csv_dicts(path: Path) -> List[dict]:
    with path.open() as f:
        return [r for r in csv.DictReader(f)]


def _to_float(s: str) -> float:
    if s is None or s == "":
        return 0.0
    return float(s.replace(",", "").replace("$", "").strip())


def _parse_yyyymmdd(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y%m%d").date()


def _load_daily(uploads_dir: Path) -> Tuple[Dict[date, float], Dict[date, int]]:
    """Read Sales_by_day.csv -> ({date: net_sales}, {date: orders}), honoring
    MANUAL_IGNORE_DATES."""
    ignored = set()
    for s in getattr(config, "MANUAL_IGNORE_DATES", []):
        try:
            ignored.add(date.fromisoformat(s))
        except (ValueError, TypeError):
            continue
    sales: Dict[date, float] = {}
    orders: Dict[date, int] = {}
    with (uploads_dir / "Sales_by_day.csv").open() as f:
        for r in csv.DictReader(f):
            try:
                d = _parse_yyyymmdd(r["yyyyMMdd"])
            except (KeyError, ValueError):
                continue
            if d in ignored:
                continue
            sales[d] = _to_float(r.get("Net sales", "0"))
            try:
                orders[d] = int(r.get("Total orders") or 0)
            except (ValueError, TypeError):
                orders[d] = 0
    return sales, orders


def _weighted(vals_recent_first: List[float], weights: List[float]) -> float:
    """Weighted mean. weights are aligned most-recent-first; truncated and
    renormalized to however many values are present."""
    if not vals_recent_first:
        return 0.0
    w = weights[:len(vals_recent_first)]
    if not w:
        w = [1.0] * len(vals_recent_first)
    denom = sum(w) or 1.0
    return sum(v * wi for v, wi in zip(vals_recent_first, w)) / denom


def weighted_dow_baseline(
    uploads_dir: Path,
) -> Tuple[Dict[int, float], Dict[int, float]]:
    """
    Per-day-of-week baseline using the most recent config.BASELINE_WEEKS
    occurrences of each weekday, weighted by config.BASELINE_DOW_WEIGHTS
    (most-recent first). Returns ({dow: sales}, {dow: orders}).

    This is the single source of the day-level baseline used by both the
    hourly-metrics projection and the recency-factor denominator, so the two
    stay consistent.
    """
    sales, orders = _load_daily(uploads_dir)
    n_weeks = int(getattr(config, "BASELINE_WEEKS", 4))
    weights = list(getattr(config, "BASELINE_DOW_WEIGHTS", [4, 3, 2, 1]))

    # Only completed history before the current week contributes to the baseline.
    cutoff = getattr(config, "BASELINE_HISTORY_CUTOFF", None)
    if cutoff:
        try:
            cutoff_d = date.fromisoformat(cutoff)
            sales = {d: v for d, v in sales.items() if d < cutoff_d}
            orders = {d: v for d, v in orders.items() if d < cutoff_d}
        except ValueError:
            pass

    by_dow_s: Dict[int, List[Tuple[date, float]]] = {}
    by_dow_o: Dict[int, List[Tuple[date, float]]] = {}
    for d, v in sales.items():
        by_dow_s.setdefault(d.weekday(), []).append((d, v))
    for d, v in orders.items():
        by_dow_o.setdefault(d.weekday(), []).append((d, float(v)))

    sales_by_dow: Dict[int, float] = {}
    orders_by_dow: Dict[int, float] = {}
    for dow in range(7):
        recent_s = [v for _, v in sorted(by_dow_s.get(dow, []), reverse=True)[:n_weeks]]
        recent_o = [v for _, v in sorted(by_dow_o.get(dow, []), reverse=True)[:n_weeks]]
        sales_by_dow[dow] = _weighted(recent_s, weights)
        orders_by_dow[dow] = _weighted(recent_o, weights)

    # Fill any empty DOW with overall mean so projections always populate
    nonzero = [v for v in sales_by_dow.values() if v > 0]
    overall = (sum(nonzero) / len(nonzero)) if nonzero else 0.0
    for dow in range(7):
        if sales_by_dow[dow] <= 0:
            sales_by_dow[dow] = overall
    return sales_by_dow, orders_by_dow


def current_week_actuals(uploads_dir: Path) -> Dict[date, Dict[str, float]]:
    """Realized current-week days from actuals.csv (single source the operator
    updates as each day finalizes). Returns {date: {net, orders, bs, cw}}.

    This is separate from the historical baseline data (Sales_by_day.csv /
    Item_hourly.csv): it holds only the in-progress week's finalized actuals and
    feeds the projection-vs-actual scorecard and the auto-calibration."""
    out: Dict[date, Dict[str, float]] = {}
    path = uploads_dir / "actuals.csv"
    if not path.exists():
        return out
    with path.open() as f:
        for r in csv.DictReader(f):
            try:
                d = date.fromisoformat(r["date"].strip())
            except (KeyError, ValueError):
                continue
            out[d] = {
                "net": _to_float(r.get("net_sales", "0")),
                "orders": _to_float(r.get("orders", "0")),
                "bs": _to_float(r.get("bs", "0")),
                "cw": _to_float(r.get("cw", "0")),
            }
    return out


def actual_sales_by_date(uploads_dir: Path) -> Dict[date, float]:
    """{date: net_sales}. Prefers actuals.csv (current-week realized days); falls
    back to Sales_by_day.csv. Ignores MANUAL_IGNORE so actuals always show."""
    out: Dict[date, float] = {}
    with (uploads_dir / "Sales_by_day.csv").open() as f:
        for r in csv.DictReader(f):
            try:
                out[_parse_yyyymmdd(r["yyyyMMdd"])] = _to_float(r.get("Net sales", "0"))
            except (KeyError, ValueError):
                continue
    # actuals.csv takes precedence for any overlapping dates
    for d, rec in current_week_actuals(uploads_dir).items():
        out[d] = rec["net"]
    return out


def actual_labor_by_date(uploads_dir: Path) -> Dict[date, Dict[str, float]]:
    """Actualized labor per day from actual_labor.csv.

    Supports two formats:
      - Legacy: date, scheduled_hours          (7shifts scheduled hours only)
      - Enriched: date, foh_hours, foh_cost, boh_hours, boh_cost, scheduled_hours
        (Toast clock-in, split FOH=Barista/Cashier vs BOH=Production Cook, costed)

    Returns {date: {foh_cost, boh_cost, foh_hours, boh_hours, total_hours,
    has_split}}. `has_split` is True only when FOH/BOH dollar figures are present,
    which is what the dashboard needs to show actual-vs-projected labor."""
    out: Dict[date, Dict[str, float]] = {}
    path = uploads_dir / "actual_labor.csv"
    if not path.exists():
        return out

    def _num(row, key):
        v = row.get(key)
        if v is None or str(v).strip() == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None

    with path.open() as f:
        for r in csv.DictReader(f):
            try:
                d = date.fromisoformat(r["date"].strip())
            except (KeyError, ValueError, AttributeError):
                continue
            foh_cost = _num(r, "foh_cost")
            boh_cost = _num(r, "boh_cost")
            out[d] = {
                "foh_hours": _num(r, "foh_hours"),
                "boh_hours": _num(r, "boh_hours"),
                "foh_cost": foh_cost,
                "boh_cost": boh_cost,
                "total_hours": _num(r, "scheduled_hours"),
                "has_split": foh_cost is not None or boh_cost is not None,
            }
    return out


def build_projection_from_csvs(
    files: TastFiles,
) -> Tuple[Dict[Tuple[int, int], HourlyProjection], OutlierReport, Dict[str, float]]:
    """
    Returns:
        - projection by (day_of_week, hour)
        - outlier report
        - metadata dict (foh_share, boh_share, days_analyzed, etc)
    """
    # ---------- 1) Daily totals + outlier detection ----------
    daily_rows = _read_csv_dicts(files.sales_by_day)
    daily_by_date: Dict[date, float] = {}
    for r in daily_rows:
        d = _parse_yyyymmdd(r["yyyyMMdd"])
        daily_by_date[d] = _to_float(r["Net sales"])

    # Apply manual ignore list first
    manually_excluded = set()
    for s in config.MANUAL_IGNORE_DATES:
        try:
            manually_excluded.add(date.fromisoformat(s))
        except ValueError:
            pass

    # Per-DOW z-score outlier detection
    by_dow: Dict[int, List[Tuple[date, float]]] = {}
    for d, total in daily_by_date.items():
        if d in manually_excluded:
            continue
        by_dow.setdefault(d.weekday(), []).append((d, total))

    auto_excluded: List[Tuple[date, float, float]] = []
    excluded = set(manually_excluded)
    for dow, items in by_dow.items():
        if len(items) < 3:
            continue
        totals = [t for _, t in items]
        mean = statistics.mean(totals)
        sd = statistics.pstdev(totals) or 1.0
        for d, t in items:
            z = (t - mean) / sd
            if abs(z) > config.OUTLIER_ZSCORE_CUTOFF:
                auto_excluded.append((d, t, z))
                excluded.add(d)

    kept_daily = {d: t for d, t in daily_by_date.items() if d not in excluded}

    # ---------- 2) Day-of-week factors (computed on kept days) ----------
    kept_by_dow: Dict[int, List[float]] = {}
    for d, total in kept_daily.items():
        kept_by_dow.setdefault(d.weekday(), []).append(total)
    avg_by_dow: Dict[int, float] = {
        dow: statistics.mean(totals) for dow, totals in kept_by_dow.items()
    }
    # If any DOW is missing entirely, fall back to overall avg
    overall_avg = statistics.mean(kept_daily.values()) if kept_daily else 0.0
    for dow in range(7):
        avg_by_dow.setdefault(dow, overall_avg)

    # ---------- 3) Hour-of-day shape ----------
    hour_rows = _read_csv_dicts(files.time_of_day)
    hour_totals: Dict[int, float] = {}
    for r in hour_rows:
        h = int(r["Hour of day"])
        hour_totals[h] = _to_float(r["Net sales"])
    total_hourly = sum(hour_totals.values()) or 1.0
    hour_share = {h: v / total_hourly for h, v in hour_totals.items()}

    # ---------- 4) FOH / BOH category split ----------
    cat_rows = _read_csv_dicts(files.sales_category)
    cat_buckets = {"foh": 0.0, "boh": 0.0, "other": 0.0}
    for r in cat_rows:
        name = r["Sales category"]
        if name.lower().startswith("total"):
            continue
        bucket = TOAST_CATEGORY_BUCKETS.get(name, "other")
        cat_buckets[bucket] += _to_float(r["Net sales"])
    cat_total = sum(cat_buckets.values()) or 1.0
    # Distribute "other" proportionally to foh/boh so projections add up
    foh_raw = cat_buckets["foh"]
    boh_raw = cat_buckets["boh"]
    fb_total = foh_raw + boh_raw or 1.0
    foh_share = foh_raw / fb_total
    boh_share = boh_raw / fb_total

    # ---------- 5) Build the projection ----------
    projection: Dict[Tuple[int, int], HourlyProjection] = {}
    for dow in range(7):
        daily_proj = avg_by_dow[dow]
        for h, share in hour_share.items():
            total = daily_proj * share
            projection[(dow, h)] = HourlyProjection(
                day_of_week=dow,
                hour=h,
                foh_sales=total * foh_share,
                boh_sales=total * boh_share,
                total_sales=total,
            )

    metadata = {
        "foh_share":          foh_share,
        "boh_share":          boh_share,
        "days_analyzed":      len(kept_daily),
        "days_total":         len(daily_by_date),
        "date_min":           min(daily_by_date.keys()) if daily_by_date else None,
        "date_max":           max(daily_by_date.keys()) if daily_by_date else None,
        "overall_avg_daily":  overall_avg,
        "category_foh_dollars": cat_buckets["foh"],
        "category_boh_dollars": cat_buckets["boh"],
        "category_other_dollars": cat_buckets["other"],
        "avg_by_dow":         avg_by_dow,
    }
    report = OutlierReport(
        auto_excluded=sorted(auto_excluded, key=lambda x: x[0]),
        manually_excluded=sorted(manually_excluded),
    )
    return projection, report, metadata
