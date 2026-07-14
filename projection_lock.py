"""
Projection lock manager.

Purpose: track how well the forward projection predicted reality.

Lifecycle of a week's projection:
  - While a week is still in the FUTURE (the "forward week"), its projection is
    mutable — every render overwrites it with the latest tuned numbers.
  - The moment that week becomes the CURRENT week (week_start <= the anchor
    Monday), its projection FREEZES. From then on it's the locked record we
    score actuals against, and it is never overwritten.

So "lock in when the forward week becomes the current week" happens
automatically: the forward projection keeps getting refreshed until the week
arrives, then it freezes on the first render of the new current week.

Persistence: locked_projections.json in the project root (next to the .py
files), so it travels with the GitHub repo and accumulates week over week.

Structure:
{
  "2026-06-22": {
    "frozen": true,
    "locked_at": "2026-06-22",
    "weekly": 49847.0,
    "days": {"2026-06-22": 5480.0, ..., "2026-06-28": 9645.0}
  },
  ...
}
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Optional

# Anchor Monday = the Monday of the current week. Imported lazily to avoid a
# circular import with forward_projection at module load.
def _anchor_monday() -> date:
    from forward_projection import CURRENT_WEEK_START
    return CURRENT_WEEK_START


# One-time seed for the current week (Jun 22-28, 2026). These are the figures
# that were locked in last session when this week was still the forward week,
# under the original 8-week-flat + uniform +63% model. They are the honest
# record of what we committed to, and stay frozen regardless of later model
# changes. (Sum = $49,847.)
SEED: Dict[str, Dict] = {
    "2026-06-22": {
        "frozen": True,
        "locked_at": "2026-06-22",
        "weekly": 49847.0,
        "days": {
            "2026-06-22": 5480.0,
            "2026-06-23": 4940.0,
            "2026-06-24": 5118.0,
            "2026-06-25": 5189.0,
            "2026-06-26": 8478.0,
            "2026-06-27": 10997.0,
            "2026-06-28": 9645.0,
        },
    },
}


def _lock_path(uploads_dir: Path) -> Path:
    # uploads_dir is .../knotted_scheduler/data ; store the lock one level up.
    return uploads_dir.parent / "locked_projections.json"


def load_locks(uploads_dir: Path) -> Dict[str, Dict]:
    p = _lock_path(uploads_dir)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_locks(uploads_dir: Path, data: Dict[str, Dict]) -> None:
    _lock_path(uploads_dir).write_text(json.dumps(data, indent=2, sort_keys=True))


def _record_week(
    locks: Dict[str, Dict],
    week_start: date,
    per_day: Dict[date, float],
    anchor: date,
) -> None:
    """Write a week's projection, honoring the freeze rule."""
    key = week_start.isoformat()
    frozen = week_start <= anchor  # current week or earlier => frozen
    if frozen and key in locks and locks[key].get("frozen"):
        return  # already frozen — never overwrite
    locks[key] = {
        "frozen": frozen,
        "locked_at": anchor.isoformat() if frozen else None,
        "weekly": round(sum(per_day.values()), 2),
        "days": {d.isoformat(): round(v, 2) for d, v in sorted(per_day.items())},
    }


def maintain_locks(uploads_dir: Path) -> Dict[str, Dict]:
    """
    Seed + refresh + freeze. Call once per render. Returns the current lock map.
    """
    from forward_projection import (
        build_forward_projection, CURRENT_WEEK_CONFIG, FORWARD_WEEK_CONFIG,
    )

    anchor = _anchor_monday()
    locks = load_locks(uploads_dir)

    # 1) Seed any missing seed weeks first, so the freeze step won't clobber them
    for key, val in SEED.items():
        locks.setdefault(key, val)

    # 2) Record current + forward week projections from the live model
    for cfg in (CURRENT_WEEK_CONFIG, FORWARD_WEEK_CONFIG):
        projection, _, _ = build_forward_projection(uploads_dir, cfg)
        per_day: Dict[date, float] = {}
        for d in cfg.week_dates:
            per_day[d] = sum(
                projection[(d, h)].adjusted_sales for h in range(6, 24)
            )
        _record_week(locks, cfg.week_dates[0], per_day, anchor)

    save_locks(uploads_dir, locks)
    return locks


def get_week_lock(uploads_dir: Path, week_start: date) -> Optional[Dict]:
    return load_locks(uploads_dir).get(week_start.isoformat())
