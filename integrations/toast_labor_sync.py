"""
Toast Labor API -> actualized labor per day (FOH/BOH split, costed).

Pulls clock-in TIME ENTRIES for a date range and splits them by job:
    Barista / Cashier  -> FOH
    Production Cook     -> BOH
Everything else (managers, salaried roles) is skipped — only hourly FOH/BOH
counts here. Each entry is costed by its Toast hourly wage (overtime at 1.5x),
falling back to config role wages if an entry has no wage.

Writes data/actual_labor.csv (enriched format the dashboard reads):
    date, foh_hours, foh_cost, boh_hours, boh_cost, total_hours

CREDENTIALS COME FROM ENVIRONMENT VARIABLES ONLY (shared with toast_sync).
Requires the LABOR scope on the Toast API credential. Never print wages/tokens.

This is best-effort: run_weekly wraps the call so a missing scope or API hiccup
degrades to "no actual labor yet" (dashboard shows 'pending') instead of failing
the daily run.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List

import config
from .toast_sync import _get_token, _http_json, _cfg


def _classify_job(title: str):
    """Map a Toast job title to 'foh' | 'boh' | None (skip)."""
    t = (title or "").lower()
    if "barista" in t or "cashier" in t or "front of house" in t:
        return "foh"
    if "cook" in t or "kitchen" in t or "production" in t or "back of house" in t:
        return "boh"
    return None


def _fetch_jobs(host: str, headers: dict) -> Dict[str, str]:
    """{jobGuid: title} from /labor/v1/jobs."""
    jobs: Dict[str, str] = {}
    data = _http_json(f"https://{host}/labor/v1/jobs", headers=headers)
    for j in (data or []):
        g = j.get("guid")
        if g:
            jobs[g] = j.get("title") or j.get("name") or ""
    return jobs


def _fetch_time_entries(host: str, headers: dict, start: date, end: date) -> List[dict]:
    """Time entries per business date (mirrors the ordersBulk businessDate pattern)."""
    out: List[dict] = []
    d = start
    while d <= end:
        url = f"https://{host}/labor/v1/timeEntries?businessDate={d.strftime('%Y%m%d')}"
        batch = _http_json(url, headers=headers)
        if isinstance(batch, list):
            out.extend(batch)
        d += timedelta(days=1)
    return out


def _entry_hours(te: dict) -> tuple:
    """(regular_hours, overtime_hours) for a time entry, with an in/out fallback."""
    reg = te.get("regularHours")
    ot = te.get("overtimeHours")
    try:
        reg = float(reg) if reg is not None else 0.0
        ot = float(ot) if ot is not None else 0.0
    except (TypeError, ValueError):
        reg = ot = 0.0
    if reg + ot <= 0:
        # Fall back to clocked in/out duration (only if fully clocked out).
        try:
            a = datetime.fromisoformat(te["inDate"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(te["outDate"].replace("Z", "+00:00"))
            reg = max(0.0, (b - a).total_seconds() / 3600.0)
            ot = 0.0
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
    return reg, ot


def _aggregate(entries: List[dict], jobs: Dict[str, str],
               foh_default: float, boh_default: float) -> Dict[date, Dict[str, float]]:
    """Pure aggregation (unit-testable): time entries -> per-day FOH/BOH hours+cost."""
    by_day: Dict[date, Dict[str, float]] = defaultdict(
        lambda: {"foh_hours": 0.0, "foh_cost": 0.0,
                 "boh_hours": 0.0, "boh_cost": 0.0, "total_hours": 0.0})
    for te in entries:
        if te.get("deleted"):
            continue
        bd = te.get("businessDate")
        try:
            if bd:
                d = datetime.strptime(str(bd), "%Y%m%d").date()
            else:
                d = datetime.fromisoformat(te["inDate"].replace("Z", "+00:00")).date()
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        reg, ot = _entry_hours(te)
        hrs = reg + ot
        if hrs <= 0:
            continue
        jref = te.get("jobReference") or {}
        jguid = jref.get("guid") if isinstance(jref, dict) else None
        dept = _classify_job(jobs.get(jguid, ""))
        wage = te.get("hourlyWage")
        try:
            wage = float(wage) if wage else None
        except (TypeError, ValueError):
            wage = None
        rec = by_day[d]
        rec["total_hours"] += hrs
        if dept == "foh":
            w = wage if wage else foh_default
            rec["foh_hours"] += hrs
            rec["foh_cost"] += reg * w + ot * w * 1.5
        elif dept == "boh":
            w = wage if wage else boh_default
            rec["boh_hours"] += hrs
            rec["boh_cost"] += reg * w + ot * w * 1.5
    return dict(by_day)


def labor_by_day(start: date, end: date) -> Dict[date, Dict[str, float]]:
    host = _cfg("TOAST_HOSTNAME", required=True)
    guid = _cfg("TOAST_RESTAURANT_GUID", required=True)
    token = _get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Toast-Restaurant-External-ID": guid,
    }
    jobs = _fetch_jobs(host, headers)
    entries = _fetch_time_entries(host, headers, start, end)
    foh_default = float(getattr(config, "AVG_FOH_WAGE", 20.0))
    boh_default = float(getattr(config, "AVG_BOH_WAGE", 21.0))
    return _aggregate(entries, jobs, foh_default, boh_default)


def write_actual_labor(data_dir: Path, start: date, end: date) -> dict:
    """Pull Toast clock-in labor for [start, end] and write actual_labor.csv."""
    if start > end:
        return {"days": 0, "note": "no completed days in range"}
    by_day = labor_by_day(start, end)
    path = data_dir / "actual_labor.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "foh_hours", "foh_cost", "boh_hours", "boh_cost", "total_hours"])
        for d in sorted(by_day):
            r = by_day[d]
            w.writerow([d.isoformat(),
                        f'{r["foh_hours"]:.1f}', f'{r["foh_cost"]:.2f}',
                        f'{r["boh_hours"]:.1f}', f'{r["boh_cost"]:.2f}',
                        f'{r["total_hours"]:.1f}'])
    return {
        "days": len(by_day),
        "foh_cost": round(sum(r["foh_cost"] for r in by_day.values()), 2),
        "boh_cost": round(sum(r["boh_cost"] for r in by_day.values()), 2),
        "total_hours": round(sum(r["total_hours"] for r in by_day.values()), 1),
    }
