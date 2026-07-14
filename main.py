"""
Cafe Knotted Scheduler — main entry point.

Workflow:
  1. Auth to Toast + 7shifts
  2. Pull 8 weeks of hourly sales (split FOH/BOH) from Toast
  3. Pull average wages from 7shifts
  4. Auto-detect outliers, exclude manually-ignored dates
  5. Project demand by (day_of_week, hour)
  6. Derive staffing thresholds from target labor %
  7. Schedule next week's shifts
  8. Cost the schedule with CA OT
  9. Write CSV + Markdown summary

Usage:
    python main.py                           # schedule next Monday's week
    python main.py --week-start 2026-06-01   # schedule a specific week
    python main.py --use-mock                # run with fake data (no API calls)

All knobs live in config.py.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import config


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.DEBUG if verbose else logging.INFO,
        datefmt="%H:%M:%S",
    )


def _next_monday(from_date: date) -> date:
    days_ahead = (7 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + timedelta(days=days_ahead)


def run(week_start: date, use_mock: bool, verbose: bool) -> None:
    _setup_logging(verbose)
    log = logging.getLogger("knotted")

    # 1. Pull data
    if use_mock:
        from mock_data import generate_mock_hourly_sales, MOCK_FOH_WAGE, MOCK_BOH_WAGE
        log.info("Using MOCK data (no API calls)")
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(weeks=config.LOOKBACK_WEEKS)
        hourly_sales = generate_mock_hourly_sales(start_date, end_date)
        avg_foh_wage = MOCK_FOH_WAGE
        avg_boh_wage = MOCK_BOH_WAGE
    else:
        from toast_client import ToastClient
        from sevenshifts_client import SevenShiftsClient
        toast = ToastClient()
        seven = SevenShiftsClient()
        end_date = date.today() - timedelta(days=1)
        start_date = end_date - timedelta(weeks=config.LOOKBACK_WEEKS)
        log.info("Pulling Toast sales %s -> %s", start_date, end_date)
        hourly_sales = toast.fetch_hourly_sales(start_date, end_date)
        log.info("Pulling 7shifts wages")
        avg_foh_wage, avg_boh_wage = seven.get_average_wages()

    # 2. Analyze
    from sales_analyzer import (
        project_hourly_demand,
        derive_foh_thresholds,
        derive_boh_thresholds,
    )
    projection, outlier_report = project_hourly_demand(hourly_sales)
    foh_thresholds = derive_foh_thresholds(projection, avg_foh_wage)
    boh_thresholds = derive_boh_thresholds(projection, avg_boh_wage)

    # 3. Schedule
    from scheduler import schedule_week
    log.info("Building schedule for week of %s", week_start)
    shifts = schedule_week(week_start, projection, foh_thresholds, boh_thresholds)

    # 4. Cost
    from labor_calculator import summarize
    daily, weekly, ot_warnings = summarize(shifts, projection, week_start, avg_foh_wage, avg_boh_wage)

    # 5. Output
    _write_outputs(
        shifts=shifts,
        daily=daily,
        weekly=weekly,
        ot_warnings=ot_warnings,
        outlier_report=outlier_report,
        foh_thresholds=foh_thresholds,
        boh_thresholds=boh_thresholds,
        avg_foh_wage=avg_foh_wage,
        avg_boh_wage=avg_boh_wage,
        week_start=week_start,
    )


def _write_outputs(
    *, shifts, daily, weekly, ot_warnings, outlier_report,
    foh_thresholds, boh_thresholds,
    avg_foh_wage, avg_boh_wage, week_start,
):
    out_dir = Path(config.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = week_start.isoformat()

    # --- Schedule CSV ---
    schedule_path = out_dir / f"schedule_{tag}.csv"
    with schedule_path.open("w", newline="") as f:
        fieldnames = ["date", "day_of_week", "role", "start", "end",
                      "break_start", "break_end", "total_hours", "paid_hours"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for s in sorted(shifts, key=lambda x: (x.date, x.start)):
            w.writerow(s.to_dict())

    # --- Summary Markdown ---
    summary_path = out_dir / f"summary_{tag}.md"
    lines = []
    lines.append(f"# Cafe Knotted Schedule — Week of {week_start.strftime('%a %b %d, %Y')}\n")
    lines.append("## Inputs\n")
    lines.append(f"- Avg FOH wage: ${avg_foh_wage:.2f}/hr")
    lines.append(f"- Avg BOH wage: ${avg_boh_wage:.2f}/hr")
    lines.append(f"- Target prime cost: {config.TARGET_PRIME_COST_PCT*100:.0f}%")
    lines.append(f"- Assumed COGS: {config.ASSUMED_COGS_PCT*100:.0f}%")
    lines.append(f"- **Implied labor target: {config.LABOR_TARGET_PCT*100:.1f}%**")
    lines.append(f"- Tolerance band: ±{config.LABOR_WEEK_TOLERANCE*100:.0f}%\n")

    lines.append("## Data exclusions\n")
    if outlier_report.manually_excluded:
        lines.append("**Manually excluded:**")
        for d in outlier_report.manually_excluded:
            lines.append(f"- {d} ({d.strftime('%a')})")
        lines.append("")
    if outlier_report.auto_excluded:
        lines.append("**Auto-flagged outliers** (z-score > "
                     f"{config.OUTLIER_ZSCORE_CUTOFF}):")
        for d, total, z in outlier_report.auto_excluded:
            lines.append(f"- {d} ({d.strftime('%a')}): ${total:,.0f} total, z={z:+.2f}")
        lines.append("")
    if not outlier_report.manually_excluded and not outlier_report.auto_excluded:
        lines.append("_None_\n")

    lines.append("## Derived staffing thresholds\n")
    lines.append("**FOH** (per-hour projected pastry/beverage sales → # of people):")
    for sales, n in foh_thresholds:
        lines.append(f"- ≥ ${sales:,.0f}/hr → {n} person{'s' if n>1 else ''}")
    lines.append("")
    lines.append("**BOH** (per-day projected savory sales → # of cooks):")
    for sales, n in boh_thresholds:
        lines.append(f"- ≥ ${sales:,.0f}/day → {n} cook{'s' if n>1 else ''}")
    lines.append("")

    # --- Daily table ---
    lines.append("## Daily breakdown\n")
    lines.append("| Day | Date | Proj. Sales | FOH hrs | BOH hrs | FOH $ | BOH $ | Total $ | Labor % |")
    lines.append("|-----|------|------------:|--------:|--------:|------:|------:|--------:|--------:|")
    for d in daily:
        flag = ""
        if d.labor_pct > config.LABOR_TARGET_PCT + 0.05:
            flag = " 🔴"
        elif d.labor_pct > config.LABOR_TARGET_PCT + 0.02:
            flag = " 🟡"
        lines.append(
            f"| {d.day_of_week} | {d.date} | ${d.projected_sales:,.0f} | "
            f"{d.foh_hours:.1f} | {d.boh_hours:.1f} | "
            f"${d.foh_cost:,.0f} | ${d.boh_cost:,.0f} | "
            f"${d.total_labor_cost:,.0f} | {d.labor_pct*100:.1f}%{flag} |"
        )
    lines.append("")

    # --- Weekly summary ---
    lines.append("## Weekly summary\n")
    diff = weekly.labor_pct - config.LABOR_TARGET_PCT
    if abs(diff) <= config.LABOR_WEEK_TOLERANCE:
        verdict = "✅ Within tolerance"
    elif diff > 0:
        verdict = f"⚠️ Over target by {diff*100:.1f}pp"
    else:
        verdict = f"💰 Under target by {-diff*100:.1f}pp (room to add coverage)"

    lines.append(f"- Projected sales: **${weekly.projected_sales:,.0f}**")
    lines.append(f"- Total scheduled paid hours: **{weekly.total_hours:.1f}**")
    lines.append(f"- FOH labor cost: ${weekly.foh_cost:,.0f}")
    lines.append(f"- BOH labor cost: ${weekly.boh_cost:,.0f}")
    lines.append(f"- **Total labor cost: ${weekly.total_labor_cost:,.0f}**")
    lines.append(f"- **Labor as % of sales: {weekly.labor_pct*100:.2f}%**")
    lines.append(f"- vs. target ({config.LABOR_TARGET_PCT*100:.1f}%): {verdict}\n")

    if ot_warnings:
        lines.append("## Daily-OT alerts\n")
        lines.append("These shifts exceed 8 paid hrs in a day and trigger CA daily OT")
        lines.append("(billed at 1.5x for hours 8-12, 2x past 12). Consider trimming.\n")
        for w in ot_warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.append("## Notes\n")
    lines.append("- Weekly OT (>40hr/wk for an individual employee) is NOT modeled.")
    lines.append("  When assigning these shifts to actual staff in 7shifts, verify no")
    lines.append("  one is scheduled across enough shifts to cross 40hrs.")
    lines.append("- Daily-OT hours ARE costed correctly (1.5x past 8, 2x past 12).\n")

    lines.append("## Files\n")
    lines.append(f"- Full schedule CSV: `{schedule_path.name}`\n")

    summary_path.write_text("\n".join(lines))

    print(f"\n✓ Schedule written to {schedule_path}")
    print(f"✓ Summary written to {summary_path}\n")


def parse_args():
    p = argparse.ArgumentParser(description="Cafe Knotted scheduling model")
    p.add_argument("--week-start", help="YYYY-MM-DD; defaults to next Monday")
    p.add_argument("--use-mock", action="store_true",
                   help="Run with synthetic data (no Toast/7shifts calls)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.week_start:
        ws = date.fromisoformat(args.week_start)
        if ws.weekday() != 0:
            print(f"Warning: {ws} is a {ws.strftime('%A')}, not Monday. Using anyway.")
    else:
        ws = _next_monday(date.today())
    run(week_start=ws, use_mock=args.use_mock, verbose=args.verbose)
