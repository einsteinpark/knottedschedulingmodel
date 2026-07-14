"""
CSV-mode entry point. Run the full scheduling model from Toast CSV exports.

Usage:
    python run_csv.py --uploads-dir /mnt/user-data/uploads --week-start 2026-05-25
"""

from __future__ import annotations

import argparse
import csv
import logging
from datetime import date, timedelta
from pathlib import Path

import config
from csv_analyzer import build_projection_from_csvs, TastFiles
from sales_analyzer import derive_foh_thresholds, derive_boh_thresholds
from scheduler import schedule_week
from labor_calculator import summarize


def _next_monday(from_date: date) -> date:
    days_ahead = (7 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + timedelta(days=days_ahead)


def run(uploads_dir: Path, week_start: date) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("knotted_csv")

    files = TastFiles(
        sales_by_day=uploads_dir / "Sales_by_day.csv",
        time_of_day=uploads_dir / "Time_of_day__totals_.csv",
        day_of_week=uploads_dir / "Day_of_week__totals_.csv",
        sales_category=uploads_dir / "Sales_category_summary.csv",
    )

    log.info("Building projection from CSVs...")
    projection, outliers, meta = build_projection_from_csvs(files)
    log.info(
        "Analyzed %d days (excluded %d), FOH share %.1f%%, BOH share %.1f%%",
        meta["days_analyzed"],
        meta["days_total"] - meta["days_analyzed"],
        meta["foh_share"] * 100,
        meta["boh_share"] * 100,
    )

    foh_thresholds = derive_foh_thresholds(projection, config.AVG_FOH_WAGE)
    boh_thresholds = derive_boh_thresholds(projection, config.AVG_BOH_WAGE)

    log.info("Scheduling week of %s", week_start)
    shifts = schedule_week(week_start, projection, foh_thresholds, boh_thresholds)

    daily, weekly, ot_warnings = summarize(
        shifts, projection, week_start,
        config.AVG_FOH_WAGE, config.AVG_BOH_WAGE,
    )

    _write_outputs(
        shifts=shifts, daily=daily, weekly=weekly,
        ot_warnings=ot_warnings, outlier_report=outliers,
        foh_thresholds=foh_thresholds, boh_thresholds=boh_thresholds,
        meta=meta, week_start=week_start,
    )


def _write_outputs(*, shifts, daily, weekly, ot_warnings, outlier_report,
                   foh_thresholds, boh_thresholds, meta, week_start):
    out_dir = Path(config.OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = week_start.isoformat()

    # --- Schedule CSV ---
    schedule_path = out_dir / f"schedule_{tag}.csv"
    with schedule_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "date", "day_of_week", "role", "start", "end",
            "break_start", "break_end", "total_hours", "paid_hours",
        ])
        w.writeheader()
        for s in sorted(shifts, key=lambda x: (x.date, x.start)):
            w.writerow(s.to_dict())

    # --- Summary ---
    summary_path = out_dir / f"summary_{tag}.md"
    L = []
    L.append(f"# Cafe Knotted Schedule — Week of {week_start.strftime('%a %b %d, %Y')}\n")
    L.append("## Data analyzed\n")
    L.append(f"- Days in source data: **{meta['days_total']}**")
    L.append(f"- Days used for projection: **{meta['days_analyzed']}**")
    L.append(f"- Overall average daily sales: **${meta['overall_avg_daily']:,.0f}**")
    L.append(f"- FOH share (pastry/beverage/retail): **{meta['foh_share']*100:.1f}%** (${meta['category_foh_dollars']:,.0f})")
    L.append(f"- BOH share (savory food): **{meta['boh_share']*100:.1f}%** (${meta['category_boh_dollars']:,.0f})\n")

    L.append("## Day-of-week pattern (average daily sales)\n")
    L.append("| Day | Avg Sales | vs. Mon |")
    L.append("|-----|----------:|--------:|")
    mon_avg = meta["avg_by_dow"].get(0, 0) or 1
    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for dow in range(7):
        avg = meta["avg_by_dow"].get(dow, 0)
        ratio = avg / mon_avg
        L.append(f"| {day_names[dow]} | ${avg:,.0f} | {ratio:.2f}x |")
    L.append("")

    L.append("## Inputs\n")
    L.append(f"- Avg hourly FOH wage: **${config.AVG_FOH_WAGE:.2f}/hr**")
    L.append(f"- Avg hourly BOH wage: **${config.AVG_BOH_WAGE:.2f}/hr**")
    L.append(f"- FOH manager salary: **${config.FOH_MANAGER_ANNUAL_SALARY:,.0f}/yr** (overhead, not in schedule)")
    L.append(f"- BOH manager salary: **${config.BOH_MANAGER_ANNUAL_SALARY:,.0f}/yr** (covers 5 cook shifts/wk)")
    L.append(f"- Target prime cost: {config.TARGET_PRIME_COST_PCT*100:.0f}%")
    L.append(f"- Assumed COGS: {config.ASSUMED_COGS_PCT*100:.0f}%")
    L.append(f"- **Implied labor target: {config.LABOR_TARGET_PCT*100:.1f}%**\n")

    # Manager economics callout
    mgr_weekly = (config.FOH_MANAGER_ANNUAL_SALARY + config.BOH_MANAGER_ANNUAL_SALARY) / 52.0
    proj_weekly = sum(d.projected_sales for d in daily)
    mgr_pct = mgr_weekly / proj_weekly * 100 if proj_weekly else 0
    L.append("## Manager fixed cost\n")
    L.append(f"- Combined manager salaries: **${mgr_weekly:,.0f}/week** (${mgr_weekly*52:,.0f}/yr)")
    L.append(f"- At projected weekly sales of ${proj_weekly:,.0f}, managers alone consume **{mgr_pct:.1f}%** of sales")
    L.append(f"- Remaining for hourly labor to hit {config.LABOR_TARGET_PCT*100:.0f}% target: **{config.LABOR_TARGET_PCT*100 - mgr_pct:.1f}pp** (≈ ${proj_weekly * config.LABOR_TARGET_PCT - mgr_weekly:,.0f}/wk)\n")

    L.append("## Data exclusions\n")
    if outlier_report.auto_excluded:
        L.append(f"**Auto-flagged outliers** (z-score > {config.OUTLIER_ZSCORE_CUTOFF}):")
        for d, total, z in outlier_report.auto_excluded:
            L.append(f"- {d} ({d.strftime('%a')}): ${total:,.0f}, z={z:+.2f}")
        L.append("")
    else:
        L.append("_No auto-detected outliers._\n")

    L.append("## Derived staffing thresholds\n")
    L.append("**FOH** (projected hourly pastry+beverage+retail sales → # of people):")
    for s, n in foh_thresholds:
        L.append(f"- ≥ ${s:,.0f}/hr → {n} person{'s' if n>1 else ''}")
    L.append("")
    L.append(f"**BOH:** fixed at {config.BOH_COOKS_PER_DAY} cooks per day. Manager (salaried) ")
    L.append(f"fills cook #1 on {len(config.BOH_MANAGER_DAYS_OF_WEEK)} of the 7 days.\n")

    L.append("## Daily breakdown\n")
    L.append("| Day | Date | Proj. Sales | FOH hrs | BOH hrs | FOH $ | BOH $ | Mgr $ | Total $ | Labor % |")
    L.append("|-----|------|------------:|--------:|--------:|------:|------:|------:|--------:|--------:|")
    for d in daily:
        flag = ""
        if d.labor_pct > config.LABOR_TARGET_PCT + 0.05:
            flag = " 🔴"
        elif d.labor_pct > config.LABOR_TARGET_PCT + 0.02:
            flag = " 🟡"
        L.append(
            f"| {d.day_of_week} | {d.date} | ${d.projected_sales:,.0f} | "
            f"{d.foh_hours:.1f} | {d.boh_hours:.1f} | "
            f"${d.foh_cost:,.0f} | ${d.boh_cost:,.0f} | "
            f"${d.manager_cost:,.0f} | "
            f"${d.total_labor_cost:,.0f} | {d.labor_pct*100:.1f}%{flag} |"
        )
    L.append("")

    L.append("## Weekly summary\n")
    diff = weekly.labor_pct - config.LABOR_TARGET_PCT
    if abs(diff) <= config.LABOR_WEEK_TOLERANCE:
        verdict = "✅ Within tolerance"
    elif diff > 0:
        verdict = f"⚠️ Over target by {diff*100:.1f}pp"
    else:
        verdict = f"💰 Under target by {-diff*100:.1f}pp (room to add coverage)"

    hourly_only = weekly.foh_cost + weekly.boh_cost
    L.append(f"- Projected sales: **${weekly.projected_sales:,.0f}**")
    L.append(f"- Hourly paid hours scheduled: **{weekly.total_hours:.1f}**")
    L.append(f"- Hourly labor cost: ${hourly_only:,.0f} ({weekly.labor_pct_hourly_only*100:.1f}% of sales)")
    L.append(f"  - FOH hourly: ${weekly.foh_cost:,.0f}")
    L.append(f"  - BOH hourly: ${weekly.boh_cost:,.0f}")
    L.append(f"- Manager salaries (weekly): ${weekly.foh_manager_cost + weekly.boh_manager_cost:,.0f}")
    L.append(f"  - FOH manager: ${weekly.foh_manager_cost:,.0f}")
    L.append(f"  - BOH manager: ${weekly.boh_manager_cost:,.0f}")
    L.append(f"- **Total labor cost: ${weekly.total_labor_cost:,.0f}**")
    L.append(f"- **Total labor as % of sales: {weekly.labor_pct*100:.2f}%**")
    L.append(f"- vs. target ({config.LABOR_TARGET_PCT*100:.1f}%): {verdict}\n")

    if ot_warnings:
        L.append("## Daily-OT alerts\n")
        L.append("These shifts trigger CA daily OT (>8hr/day):\n")
        for w in ot_warnings:
            L.append(f"- {w}")
        L.append("")

    summary_path.write_text("\n".join(L))
    print(f"\n✓ Schedule:  {schedule_path}")
    print(f"✓ Summary:   {summary_path}\n")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--uploads-dir", default="/mnt/user-data/uploads")
    p.add_argument("--week-start", default=None)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    ws = (date.fromisoformat(args.week_start) if args.week_start
          else _next_monday(date.today()))
    run(Path(args.uploads_dir), ws)
