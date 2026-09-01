#!/usr/bin/env python3
"""
Weekly orchestrator — the entry point the Sunday-night job runs.

Pipeline:
  1. Pull recent days from Toast -> refresh Sales_by_day.csv, Item_hourly.csv,
     and the current week's actuals.csv  (skip with --skip-toast to use the CSVs
     already on disk — useful for local testing without credentials).
  2. (optional) Pull last week's scheduled labor from 7shifts.
  3. (optional) Pull Instagram insights and PRINT a suggested lift (never auto-
     applied — a human confirms before it touches the projection).
  4. Re-render the dashboard (HTML, and PDF if wkhtmltopdf is present).
  5. Write a run log.

Credentials are read from environment variables by the integration modules.
Never pass secrets on the command line or commit them. See AUTOMATION.md.

Usage:
  python run_weekly.py                      # full run (needs Toast creds)
  python run_weekly.py --skip-toast         # re-render from existing CSVs
  python run_weekly.py --with-7shifts --with-social
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT_HTML = ROOT / "weekly_dashboard.html"
OUT_PDF = ROOT / "weekly_dashboard.pdf"
LOG = ROOT / "run_log.json"


def current_monday(today: date = None) -> date:
    today = today or date.today()
    return today - timedelta(days=today.weekday())


def run(args) -> dict:
    summary = {"ran_at": datetime.now().isoformat(timespec="seconds")}
    today = date.today()
    yesterday = today - timedelta(days=1)
    start = yesterday - timedelta(days=args.lookback_days)
    week_start = current_monday(today)

    # 1) Toast -> CSVs
    if not args.skip_toast:
        from integrations import toast_sync
        summary["toast"] = toast_sync.sync(DATA, start, yesterday, week_start)
        print(f"[toast] {summary['toast']['orders']} orders, "
              f"net ${summary['toast']['net_total']:,.2f} across "
              f"{len(summary['toast']['days'])} days")
        # 1b) Toast clock-in -> actualized labor (FOH/BOH split, costed).
        # Best-effort: needs the LABOR scope; a failure must not sink the run.
        try:
            from integrations import toast_labor_sync
            summary["toast_labor"] = toast_labor_sync.write_actual_labor(
                DATA, week_start, yesterday)
            print(f"[toast-labor] {summary['toast_labor']}")
        except Exception as e:
            print(f"[toast-labor] skipped: {e}")

        # 1c) Rolling 8-week blended wage per position from Toast clock-in ->
        # data/derived_wages.json, which config loads on the next run (1-run lag).
        try:
            from integrations import toast_labor_sync as _tls
            wage_start = yesterday - timedelta(days=55)  # ~8 weeks
            w = _tls.derive_position_wages(wage_start, yesterday)
            if w.get("foh_wage") or w.get("boh_wage"):
                (DATA / "derived_wages.json").write_text(json.dumps({
                    "foh_wage": w.get("foh_wage"),
                    "boh_wage": w.get("boh_wage"),
                    "window": [wage_start.isoformat(), yesterday.isoformat()],
                    "foh_hours": w.get("foh_hours"),
                    "boh_hours": w.get("boh_hours"),
                }, indent=2))
                summary["derived_wages"] = w
                print(f"[wages] 8-wk blended FOH ${w.get('foh_wage')} / "
                      f"BOH ${w.get('boh_wage')}")
        except Exception as e:
            print(f"[wages] skipped: {e}")
    else:
        print("[toast] skipped (using existing CSVs)")

    # 2) 7shifts -> SCHEDULED labor (optional). Scheduling only — actual labor
    #    comes from Toast clock-in above; kept in a separate file.
    if args.with_7shifts:
        from integrations import sevenshifts_sync
        last_mon = week_start - timedelta(days=7)
        summary["sevenshifts"] = sevenshifts_sync.write_scheduled_labor(
            DATA, last_mon, last_mon + timedelta(days=6))
        print(f"[7shifts] {summary['sevenshifts']}")

    # 3) Social -> suggested lift (printed only, never auto-applied)
    if args.with_social:
        try:
            from integrations import social
            metrics = social.fetch_instagram_metrics()
            base = float(args.ig_baseline_reach)
            sig = social.engagement_to_lift_signal(metrics, base)
            summary["instagram"] = sig
            print(f"[instagram] suggested lift {sig['suggested_lift_pct']:+}%  "
                  f"(reach {sig['total_reach']:,}, {sig['reach_vs_baseline']}x baseline) "
                  f"— review before applying")
        except Exception as e:  # social is best-effort
            print(f"[instagram] skipped: {e}")

    # 4) Render
    from weekly_dashboard import render_dashboard
    render_dashboard(DATA, OUT_HTML)
    print(f"[render] {OUT_HTML}")
    try:
        subprocess.run(
            ["wkhtmltopdf", "--enable-local-file-access", "--print-media-type",
             "--page-size", "Letter", "--orientation", "Landscape",
             str(OUT_HTML), str(OUT_PDF)],
            check=False, capture_output=True, timeout=180)
        if OUT_PDF.exists():
            print(f"[render] {OUT_PDF}")
    except Exception as e:
        print(f"[render] pdf skipped: {e}")

    summary["outputs"] = [str(OUT_HTML)] + ([str(OUT_PDF)] if OUT_PDF.exists() else [])
    LOG.write_text(json.dumps(summary, indent=2))
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--lookback-days", type=int, default=35,
                   help="How many days back to pull from Toast (baseline window).")
    p.add_argument("--skip-toast", action="store_true",
                   help="Re-render from existing CSVs without calling Toast.")
    p.add_argument("--with-7shifts", action="store_true")
    p.add_argument("--with-social", action="store_true")
    p.add_argument("--ig-baseline-reach", default="20000",
                   help="Typical weekly IG reach, for the lift suggestion.")
    args = p.parse_args()
    try:
        run(args)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
