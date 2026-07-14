"""
7shifts API -> actual scheduled labor (supplementary).

The dashboard's core output is the *recommended* schedule from projected demand.
This pulls what was actually scheduled in 7shifts so you can compare planned vs
actual labor hours/cost per day. Optional — the dashboard renders fine without it.

Credentials from environment only (set in your secret store, never in chat):

    SEVENSHIFTS_API_KEY
    SEVENSHIFTS_COMPANY_ID
    SEVENSHIFTS_LOCATION_ID    (Arts District)

Same pattern as your labor-audit 7shifts pull; if you already have a working
client there, call it instead of `fetch_shifts` below.
"""
from __future__ import annotations

import os
import json
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List

import csv

_BASE = "https://api.7shifts.com/v2"


def _cfg(name: str, required: bool = True) -> str:
    val = os.environ.get(name)
    if required and not val:
        raise RuntimeError(f"Missing env var {name} (set it in your secret store).")
    return val


def _get(path: str, params: dict) -> dict:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{_BASE}{path}?{qs}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {_cfg('SEVENSHIFTS_API_KEY')}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"7shifts {url} -> {e.code}: {e.read().decode()[:300]}")


def fetch_shifts(start: date, end: date) -> List[dict]:
    """Shifts overlapping [start, end]. Paginated via cursor."""
    out, cursor = [], None
    while True:
        params = {
            "company_id": _cfg("SEVENSHIFTS_COMPANY_ID"),
            "location_id": _cfg("SEVENSHIFTS_LOCATION_ID"),
            "start[gte]": f"{start.isoformat()}T00:00:00",
            "start[lte]": f"{end.isoformat()}T23:59:59",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        resp = _get("/shifts", params)
        out.extend(resp.get("data", []))
        cursor = (resp.get("meta", {}) or {}).get("cursor", {}).get("next")
        if not cursor:
            break
    return out


def scheduled_hours_by_day(start: date, end: date) -> Dict[date, float]:
    """{date: total scheduled hours} (paid hours, breaks not modeled here)."""
    by_day: Dict[date, float] = defaultdict(float)
    for s in fetch_shifts(start, end):
        try:
            a = datetime.fromisoformat(s["start"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(s["end"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if s.get("deleted") or s.get("draft"):
            continue
        by_day[a.date()] += max(0.0, (b - a).total_seconds() / 3600.0)
    return dict(by_day)


def write_actual_labor(data_dir: Path, start: date, end: date) -> dict:
    hours = scheduled_hours_by_day(start, end)
    path = data_dir / "actual_labor.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "scheduled_hours"])
        for d in sorted(hours):
            w.writerow([d.isoformat(), f"{hours[d]:.1f}"])
    return {"days": len(hours), "total_hours": round(sum(hours.values()), 1)}
