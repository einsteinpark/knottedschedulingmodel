"""
FOH shift optimizer for Cafe Knotted.

Takes the current shift pattern and demand projection, and produces a
proposed alternative that:

  1. Better covers the morning rush (no more solo opener from 8a-12p)
  2. Trims evening over-coverage when demand drops
  3. Respects CA break rules (30-min unpaid before 5th hour if shift > 6hr)
  4. Honors the staggered close (dish closer +30min, station closer +60min)
  5. Keeps total weekly FOH cost similar or lower

The output:
  - A side-by-side "current vs proposed" comparison per day
  - Coverage chart (headcount per hour vs demand-required headcount)
  - Wage-by-tier breakdown ($19 / $21 / $23) so you can see where the spend goes
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, time, timedelta, datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

import config
from csv_analyzer import build_projection_from_csvs, TastFiles


# Wage tiers for FOH staff. Blended to a flat $23 for all FOH (operator, 2026-08);
# tier labels are retained for display but every FOH shift costs the same rate.
WAGE_JR = 23.00
WAGE_MD = 23.00
WAGE_SR = 23.00


@dataclass
class FOHShift:
    """One FOH shift on a single day."""
    label: str                # display name (e.g., "Opener", "Mid-1")
    start_min: int            # minutes from midnight
    end_min: int
    wage: float               # $/hr tier
    tier: str                 # 'jr' | 'md' | 'sr'
    # Optional: pin the break to a specific start time (minutes from midnight).
    # When set, the break_scheduler will respect this and not move the break.
    # Used for operator-specified break times that override demand-based logic.
    pinned_break_start_min: Optional[int] = None
    # True = position planned but not yet staffed (rendered yellow / "TBD").
    unfilled: bool = False
    has_break: bool = field(init=False)
    break_start_min: Optional[int] = field(init=False, default=None)
    break_end_min: Optional[int] = field(init=False, default=None)

    def __post_init__(self):
        # 30-min unpaid break before 5th hour if shift > 6 hr
        duration_min = self.end_min - self.start_min
        if duration_min > 6 * 60:
            self.has_break = True
            if self.pinned_break_start_min is not None:
                self.break_start_min = self.pinned_break_start_min
                self.break_end_min = self.pinned_break_start_min + 30
            else:
                # Default: place break ~4.5h into shift (before 5th hour starts)
                bs = self.start_min + int(4.5 * 60)
                self.break_start_min = bs
                self.break_end_min = bs + 30
        else:
            self.has_break = False

    @property
    def total_hours(self) -> float:
        return (self.end_min - self.start_min) / 60.0

    @property
    def paid_hours(self) -> float:
        return self.total_hours - (0.5 if self.has_break else 0)

    @property
    def cost(self) -> float:
        return self.paid_hours * self.wage

    @property
    def start_str(self) -> str:
        h, m = divmod(self.start_min, 60)
        return f"{h:02d}:{m:02d}"

    @property
    def end_str(self) -> str:
        h, m = divmod(self.end_min, 60)
        return f"{h:02d}:{m:02d}"

    def minutes_in_hour(self, hour: int) -> float:
        """Paid minutes contributed to a given clock hour."""
        hr_start = hour * 60
        hr_end = (hour + 1) * 60
        overlap = max(0, min(self.end_min, hr_end) - max(self.start_min, hr_start))
        if self.has_break and self.break_start_min is not None:
            b_overlap = max(0, min(self.break_end_min, hr_end) -
                            max(self.break_start_min, hr_start))
            overlap -= b_overlap
        return overlap


def _t(h: int, m: int = 0) -> int:
    """Convert clock time to minutes-from-midnight."""
    return h * 60 + m


# ---------------------------------------------------------------------------
# CURRENT schedule (what you're running today)
# ---------------------------------------------------------------------------

CURRENT_SCHEDULE: Dict[int, List[FOHShift]] = {
    # Mon-Thu: 3 shifts
    0: [
        FOHShift("Opener",     _t(6, 30),  _t(12, 30), WAGE_JR, 'jr'),
        FOHShift("Mid",        _t(11, 30), _t(20, 0),  WAGE_JR, 'jr'),
        FOHShift("Closer",     _t(12, 30), _t(21, 0),  WAGE_SR, 'sr'),
    ],
    1: None,  # filled in below
    2: None,
    3: None,
    # Fri: 5 shifts
    4: [
        FOHShift("Opener",        _t(6, 30),  _t(12, 30), WAGE_JR, 'jr'),
        FOHShift("Mid-AM",        _t(10, 0),  _t(16, 0),  WAGE_JR, 'jr'),
        FOHShift("Mid-PM",        _t(12, 30), _t(21, 0),  WAGE_JR, 'jr'),
        FOHShift("First Closer",   _t(14, 30), _t(23, 0),  WAGE_MD, 'md'),
        FOHShift("Second Closer",_t(14, 30), _t(23, 0),  WAGE_SR, 'sr'),
    ],
    # Sat: 6 shifts
    5: [
        FOHShift("Opener",        _t(6, 30),  _t(15, 0),  WAGE_JR, 'jr'),
        FOHShift("Mid-1",         _t(8, 0),   _t(16, 30), WAGE_JR, 'jr'),
        FOHShift("Mid-2",         _t(10, 0),  _t(16, 0),  WAGE_JR, 'jr'),
        FOHShift("Mid-3",         _t(12, 30), _t(21, 0),  WAGE_MD, 'md'),
        FOHShift("First Closer",   _t(14, 30), _t(23, 0),  WAGE_JR, 'jr'),
        FOHShift("Second Closer",_t(14, 30), _t(23, 0),  WAGE_SR, 'sr'),
    ],
    # Sun: 5 shifts
    6: [
        FOHShift("Opener",        _t(6, 30),  _t(15, 0),  WAGE_JR, 'jr'),
        FOHShift("Mid-1",         _t(8, 0),   _t(16, 0),  WAGE_JR, 'jr'),
        FOHShift("Mid-2",         _t(10, 30), _t(19, 0),  WAGE_MD, 'md'),
        FOHShift("Mid-3",         _t(12, 30), _t(21, 0),  WAGE_MD, 'md'),
        FOHShift("PM-helper",     _t(15, 0),  _t(21, 0),  WAGE_JR, 'jr'),
    ],
}
# M-Thu all share the same pattern
for d in (1, 2, 3):
    CURRENT_SCHEDULE[d] = [
        FOHShift(s.label, s.start_min, s.end_min, s.wage, s.tier)
        for s in CURRENT_SCHEDULE[0]
    ]


# ---------------------------------------------------------------------------
# PROPOSED schedule (optimized)
#
# Design principles:
#   - M-Thu: 4 shifts (was 3). Add an early "rush helper" 9:30a-2:30p (5hr,
#     no break needed). Pull the existing "Mid" earlier to 10am instead of
#     11:30am. Shorten the senior closer by 1 hour (in at 1:30pm instead
#     of 12:30pm) since 5-7pm only needs 2 people not 3.
#   - Fri: 5 shifts but redistribute. Add an early mid 9am-3pm.
#   - Sat/Sun: similar pattern — earlier 2nd-person arrival.
#   - Staggered closes preserved: dish closer leaves at close+30min,
#     station closer at close+60min.
# ---------------------------------------------------------------------------

# Close times by DOW (from config)
CLOSE_BY_DOW = {
    0: _t(20, 0),  1: _t(20, 0),  2: _t(20, 0),  3: _t(20, 0),
    4: _t(22, 0),  5: _t(22, 0),
    6: _t(20, 0),
}

# Ordinal words for auto-labeling openers/closers.
_ORDINALS = ["First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth"]


def relabel_shifts(shifts: List[FOHShift], close_min: int) -> List[FOHShift]:
    """Auto-label FOH shifts by the operator's naming convention:

      - starts before 8:00am              -> Opener (First/Second Opener if >1)
      - ends after the day's close time    -> First Closer, Second Closer, ...
      - everyone else                      -> Mid-1, Mid-2, ...

    Openers are ordered by start time; mids by start time; closers by END time
    (earliest departure after close = First Closer — the staggered-close order).
    Mutates the shift labels in place and returns the list.
    """
    OPEN_CUTOFF = 8 * 60

    def is_closer(s):
        return s.end_min > close_min

    def is_opener(s):
        return s.start_min < OPEN_CUTOFF and not is_closer(s)

    openers = sorted([s for s in shifts if is_opener(s)], key=lambda s: (s.start_min, s.end_min))
    closers = sorted([s for s in shifts if is_closer(s)], key=lambda s: (s.end_min, s.start_min))
    mids = sorted([s for s in shifts if not is_opener(s) and not is_closer(s)],
                  key=lambda s: (s.start_min, s.end_min))

    for i, s in enumerate(openers):
        s.label = "Opener" if len(openers) == 1 else f"{_ORDINALS[i]} Opener"
    for i, s in enumerate(mids):
        s.label = f"Mid-{i + 1}"
    for i, s in enumerate(closers):
        s.label = "Closer" if len(closers) == 1 else f"{_ORDINALS[i]} Closer"
    return shifts


# Raw shift TIMES per day (labels are auto-assigned by relabel_shifts below, so
# the placeholder labels here are cosmetic). Wage tiers are PROVISIONAL pending
# the operator pay-rate review: jr $19 / md $21 / sr $23.
PROPOSED_SCHEDULE: Dict[int, List[FOHShift]] = {
    # Mon-Thu (operator spec 2026-08): opener, one long mid, two staggered closers.
    0: [
        FOHShift("Opener",        _t(6, 30),  _t(12, 30), WAGE_JR, 'jr'),   # 6:30a-12:30p
        FOHShift("Mid-1",         _t(8, 0),   _t(16, 30), WAGE_JR, 'jr'),   # 8a-4:30p
        FOHShift("First Closer",  _t(14, 30), _t(20, 30), WAGE_MD, 'md'),   # 2:30p-8:30p
        FOHShift("Second Closer", _t(12, 30), _t(21, 0),  WAGE_SR, 'sr'),   # 12:30p-9p
    ],
    1: None, 2: None, 3: None,
    # Fri (operator spec 2026-08).
    4: [
        FOHShift("Opener",        _t(6, 30),  _t(12, 0),  WAGE_JR, 'jr'),   # 6:30a-12p
        FOHShift("Mid-1",         _t(8, 0),   _t(14, 0),  WAGE_JR, 'jr'),   # 8a-2p
        FOHShift("Mid-2",         _t(9, 0),   _t(15, 0),  WAGE_JR, 'jr'),   # 9a-3p
        FOHShift("Mid-3",         _t(10, 0),  _t(18, 30), WAGE_MD, 'md'),   # 10a-6:30p
        FOHShift("Mid-4",         _t(11, 0),  _t(19, 30), WAGE_MD, 'md'),   # 11a-7:30p
        FOHShift("First Closer",  _t(16, 30), _t(22, 30), WAGE_MD, 'md'),   # 4:30p-10:30p
        FOHShift("Second Closer", _t(15, 0),  _t(23, 0),  WAGE_SR, 'sr'),   # 3p-11p
    ],
    # Sat (operator spec 2026-08).
    5: [
        FOHShift("Opener",        _t(6, 30),  _t(12, 0),  WAGE_JR, 'jr'),   # 6:30a-12p
        FOHShift("Mid-1",         _t(8, 0),   _t(14, 0),  WAGE_JR, 'jr'),   # 8a-2p
        FOHShift("Mid-2",         _t(8, 30),  _t(17, 0),  WAGE_MD, 'md'),   # 8:30a-5p
        FOHShift("Mid-3",         _t(9, 0),   _t(15, 0),  WAGE_JR, 'jr'),   # 9a-3p
        FOHShift("Mid-4",         _t(10, 0),  _t(18, 30), WAGE_MD, 'md'),   # 10a-6:30p
        FOHShift("Mid-5",         _t(14, 0),  _t(20, 0),  WAGE_MD, 'md'),   # 2p-8p
        FOHShift("First Closer",  _t(16, 30), _t(22, 30), WAGE_MD, 'md'),   # 4:30p-10:30p
        FOHShift("Second Closer", _t(15, 0),  _t(23, 0),  WAGE_SR, 'sr'),   # 3p-11p
    ],
    # Sun (operator spec 2026-08).
    6: [
        FOHShift("Opener",        _t(6, 30),  _t(12, 0),  WAGE_JR, 'jr'),   # 6:30a-12p
        FOHShift("Mid-1",         _t(8, 0),   _t(14, 0),  WAGE_JR, 'jr'),   # 8a-2p
        FOHShift("Mid-2",         _t(8, 30),  _t(17, 0),  WAGE_MD, 'md'),   # 8:30a-5p
        FOHShift("Mid-3",         _t(9, 0),   _t(17, 30), WAGE_MD, 'md'),   # 9a-5:30p
        FOHShift("Mid-4",         _t(11, 0),  _t(19, 30), WAGE_MD, 'md'),   # 11a-7:30p
        FOHShift("First Closer",  _t(14, 30), _t(20, 30), WAGE_MD, 'md'),   # 2:30p-8:30p
        FOHShift("Second Closer", _t(14, 0),  _t(21, 0),  WAGE_SR, 'sr'),   # 2p-9p
    ],
}
# Auto-label each distinct day by the start/end convention, then replicate
# Mon-Thu across Tue-Thu.
for _d in (0, 4, 5, 6):
    relabel_shifts(PROPOSED_SCHEDULE[_d], CLOSE_BY_DOW[_d])
for d in (1, 2, 3):
    PROPOSED_SCHEDULE[d] = [
        FOHShift(s.label, s.start_min, s.end_min, s.wage, s.tier)
        for s in PROPOSED_SCHEDULE[0]
    ]


# ---------------------------------------------------------------------------
# Current-week-only extra coverage — CLEARED. The day-helpers are removed per
# operator request; the new baseline roster (Second Opener + Rush-helper) now
# carries that midday coverage directly.
# ---------------------------------------------------------------------------
CURRENT_WEEK_EXTRA_SHIFTS: Dict[int, List[FOHShift]] = {}

# Current-week-only base-roster overrides — CLEARED (the prior Thu override
# retimed the old Rush-helper, which no longer exists in the new baseline).
CURRENT_WEEK_SHIFT_OVERRIDES: Dict[int, List[FOHShift]] = {}


# ---------------------------------------------------------------------------
# Forward-week extra coverage — CLEARED. These planned day-helpers (mostly
# 8a-2p) are now redundant with the new baseline Second Opener (8a-2p), so
# leaving them would double-count coverage. The new baseline applies to all tabs.
# ---------------------------------------------------------------------------
FORWARD_WEEK_EXTRA_SHIFTS: Dict[int, List[FOHShift]] = {}


def _copy_shift(s: "FOHShift") -> "FOHShift":
    return FOHShift(s.label, s.start_min, s.end_min, s.wage, s.tier,
                    pinned_break_start_min=s.pinned_break_start_min,
                    unfilled=s.unfilled)


def day_schedule(
    dow: int,
    extra_shifts: Optional[Dict[int, List["FOHShift"]]] = None,
    base_overrides: Optional[Dict[int, List["FOHShift"]]] = None,
) -> List["FOHShift"]:
    """Fresh copies of the schedule for a DOW.

    base_overrides[dow], if present, REPLACES the proposed base roster for that
    day (current-week retimes). extra_shifts[dow] are then merged on top. Fresh
    copies avoid mutating the shared module-level FOHShift objects when breaks
    get (re)assigned."""
    base_src = (base_overrides or {}).get(dow) or PROPOSED_SCHEDULE[dow]
    out = [_copy_shift(s) for s in base_src]
    if extra_shifts:
        out += [_copy_shift(s) for s in extra_shifts.get(dow, [])]
    return out


def compute_coverage(shifts: List[FOHShift]) -> Dict[int, float]:
    """Returns headcount on the floor for each hour (averaging over the hour)."""
    cov: Dict[int, float] = defaultdict(float)
    for s in shifts:
        for h in range(24):
            mins = s.minutes_in_hour(h)
            if mins > 0:
                cov[h] += mins / 60.0
    return cov


def required_for_hour(foh_sales: float) -> int:
    """
    DEPRECATED — kept for backward compatibility with existing call sites.
    Use required_for_orders() instead, which is more accurate.

    This dollar-based version was too conservative on transaction-heavy hours.
    """
    # Same thresholds as before for any existing callers
    if foh_sales >= 568: return 4
    if foh_sales >= 379: return 3
    if foh_sales >= 189: return 2
    return 1


def required_for_orders(orders_per_hour: float) -> int:
    """
    Orders/hr → required FOH headcount.

    Throughput model (from operator input): 10 orders/hr per person, flat
    (no diminishing returns).
      - 1 person:  ≤10 orders/hr
      - 2 people:  11-20 orders/hr
      - 3 people:  21-30 orders/hr
      - 4 people:  31+ orders/hr

    Rounds to int before tier comparison — the displayed values are integers
    and tier semantics ("21-30") should match what the user sees, not be
    triggered by sub-1 fractional differences in historical averages.
    """
    o = int(round(orders_per_hour))
    if o >= 31: return 4
    if o >= 21: return 3
    if o >= 11: return 2
    return 1


def required_for_sales(total_sales_per_hour: float, other_sales_per_hour: float = None) -> int:
    """
    Sales/hr → minimum FOH headcount (ceiling override).

    Rule rationale: each FOH body costs ~$20/hr blended. To keep FOH labor at
    ≤15% of sales, each tier needs enough total-sales support to amortize.
    BS/CW are quick-grab items (pull-and-hand-off) that don't drive the same
    FOH load as drinks and assembled items — so the staffing tier is driven
    by the NON-FEATURED ("Other") sales, with a total-sales floor to ensure
    each tier is economically justified.

    Tiers (all require BOTH conditions to fire):
      - Other ≥ $275/hr AND total ≥ $400/hr → 3 people
      - Other ≥ $375/hr AND total ≥ $525/hr → 4 people
      - Other ≥ $475/hr AND total ≥ $750/hr → 5 people

    If only total_sales is given (e.g. callers without item breakdown), fall
    back to a simpler total-sales tier.
    """
    if other_sales_per_hour is not None:
        # 5 people: Other ≥ $475 AND total ≥ $750
        if other_sales_per_hour >= 475 and total_sales_per_hour >= 750:
            return 5
        # 4 people: Other ≥ $375 AND total ≥ $525
        if other_sales_per_hour >= 375 and total_sales_per_hour >= 525:
            return 4
        # 3 people: Other ≥ $275 AND total ≥ $400
        if other_sales_per_hour >= 275 and total_sales_per_hour >= 400:
            return 3
        return 0

    # Backwards-compat: legacy total-sales-only tier
    if total_sales_per_hour >= 525:
        return 4
    return 0


def required_combined(orders_per_hour: float, total_sales_per_hour: float,
                      other_sales_per_hour: float = None) -> int:
    """
    Final FOH need = max(orders-based, sales-ceiling-override).

    Sales only kicks in when it would *bump up* the orders-based need.
    """
    return max(required_for_orders(orders_per_hour),
               required_for_sales(total_sales_per_hour, other_sales_per_hour))


def schedule_cost(shifts: List[FOHShift]) -> Tuple[float, float, Dict[str, float]]:
    """Returns (total_cost, total_paid_hours, by_tier_cost)."""
    total = sum(s.cost for s in shifts)
    hours = sum(s.paid_hours for s in shifts)
    by_tier: Dict[str, float] = defaultdict(float)
    for s in shifts:
        by_tier[s.tier] += s.cost
    return total, hours, dict(by_tier)


def render_comparison_html(
    projection,
    output_path: Path,
):
    """Render side-by-side comparison HTML."""
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    short_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Per-day analysis
    rows = []
    week_cur_cost = week_prop_cost = 0.0
    week_cur_hrs = week_prop_hrs = 0.0
    week_cur_tier = defaultdict(float)
    week_prop_tier = defaultdict(float)

    for dow in range(7):
        cur = CURRENT_SCHEDULE[dow]
        prop = PROPOSED_SCHEDULE[dow]
        cur_cost, cur_hrs, cur_tier = schedule_cost(cur)
        prop_cost, prop_hrs, prop_tier = schedule_cost(prop)
        cur_cov = compute_coverage(cur)
        prop_cov = compute_coverage(prop)

        week_cur_cost += cur_cost
        week_prop_cost += prop_cost
        week_cur_hrs += cur_hrs
        week_prop_hrs += prop_hrs
        for t, v in cur_tier.items():  week_cur_tier[t] += v
        for t, v in prop_tier.items(): week_prop_tier[t] += v

        rows.append({
            "dow": dow,
            "name": day_names[dow],
            "short": short_names[dow],
            "cur": cur, "prop": prop,
            "cur_cost": cur_cost, "prop_cost": prop_cost,
            "cur_hrs": cur_hrs, "prop_hrs": prop_hrs,
            "cur_cov": cur_cov, "prop_cov": prop_cov,
        })

    # Build the body
    days_html = []
    for r in rows:
        days_html.append(_render_day_block(r, projection))

    # Tier breakdown
    cur_tier_str = " · ".join(f"${week_cur_tier.get(t,0):,.0f} {t}" for t in ('jr','md','sr'))
    prop_tier_str = " · ".join(f"${week_prop_tier.get(t,0):,.0f} {t}" for t in ('jr','md','sr'))
    delta_cost = week_prop_cost - week_cur_cost
    delta_hrs = week_prop_hrs - week_cur_hrs
    delta_arrow = "▼" if delta_cost < 0 else ("▲" if delta_cost > 0 else "—")
    delta_class = "down" if delta_cost < -10 else ("up" if delta_cost > 10 else "flat")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Knotted FOH Schedule — Current vs Proposed</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #faf8f5; --ink: #1a1814; --ink-soft: #6b6359; --rule: #e5dfd4;
  --jr: #7b9c5c; --md: #c4541f; --sr: #5a3a78;
  --good: #4a6b2c; --warn: #b88718; --bad: #9d3812;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg); color: var(--ink);
  padding: 32px 32px 64px; max-width: 1500px; margin: 0 auto;
  font-size: 12px; line-height: 1.5;
}}
header.masthead {{
  display: flex; align-items: baseline; justify-content: space-between;
  padding-bottom: 16px; margin-bottom: 24px; border-bottom: 2px solid var(--ink);
}}
.masthead h1 {{ font-family: 'Fraunces', serif; font-weight: 700; font-size: 32px; letter-spacing: -0.02em; }}
.masthead h1 em {{ font-style: italic; font-weight: 400; color: var(--ink-soft); }}
.masthead .sub {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--ink-soft); text-transform: uppercase; letter-spacing: 0.08em; }}

.summary {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0; margin-bottom: 28px; border: 1px solid var(--rule); background: white; }}
.summary .col {{ padding: 18px 22px; border-right: 1px solid var(--rule); }}
.summary .col:last-child {{ border-right: none; }}
.summary .lbl {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--ink-soft); margin-bottom: 6px; }}
.summary .big {{ font-family: 'Fraunces', serif; font-size: 28px; font-weight: 700; letter-spacing: -0.01em; }}
.summary .meta {{ font-size: 11px; color: var(--ink-soft); margin-top: 4px; font-family: 'JetBrains Mono', monospace; }}
.delta.down {{ color: var(--good); }}
.delta.up   {{ color: var(--bad); }}
.delta.flat {{ color: var(--ink-soft); }}

.day {{ background: white; border: 1px solid var(--rule); margin-bottom: 22px; page-break-inside: avoid; }}
.day-head {{ display: flex; padding: 14px 22px; border-bottom: 1px solid var(--rule); align-items: center; gap: 18px; justify-content: space-between; }}
.day-head .name {{ display: flex; align-items: baseline; gap: 10px; }}
.day-head .dnum {{ font-family: 'Fraunces', serif; font-size: 26px; font-weight: 700; }}
.day-head .dlabel {{ font-family: 'Fraunces', serif; font-style: italic; font-size: 17px; color: var(--ink-soft); }}
.day-head .delta-pill {{
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  padding: 4px 10px; border-radius: 3px;
}}
.delta-pill.down {{ background: #e2eed5; color: var(--good); }}
.delta-pill.up   {{ background: #f5d8c8; color: var(--bad); }}
.delta-pill.flat {{ background: #f1ece1; color: var(--ink-soft); }}

.day-body {{ display: grid; grid-template-columns: 1fr 1fr; }}
.side {{ padding: 18px 22px; border-right: 1px solid var(--rule); }}
.side:last-child {{ border-right: none; background: #faf6ed; }}
.side h3 {{ font-family: 'Fraunces', serif; font-style: italic; font-weight: 400; font-size: 14px; color: var(--ink-soft); margin-bottom: 10px; }}
.side h3 .marker {{ font-family: 'JetBrains Mono', monospace; font-style: normal; font-size: 10px; background: var(--ink); color: white; padding: 2px 6px; border-radius: 2px; margin-right: 6px; letter-spacing: 0.05em; }}

.shift-list {{ list-style: none; }}
.shift-list li {{
  display: grid; grid-template-columns: 32px 1fr 80px 70px;
  align-items: center; gap: 8px; padding: 5px 0;
  border-bottom: 1px dotted var(--rule);
  font-family: 'JetBrains Mono', monospace; font-size: 11px;
}}
.shift-list li:last-child {{ border-bottom: none; }}
.tier-dot {{ width: 10px; height: 10px; border-radius: 50%; display: inline-block; }}
.tier-jr {{ background: var(--jr); }}
.tier-md {{ background: var(--md); }}
.tier-sr {{ background: var(--sr); }}
.shift-list .lbl {{ font-family: 'Inter', sans-serif; font-size: 11px; }}
.shift-list .time {{ color: var(--ink-soft); }}
.shift-list .cost {{ text-align: right; }}

.totals {{ margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--rule); font-family: 'JetBrains Mono', monospace; font-size: 11px; }}
.totals .row {{ display: flex; justify-content: space-between; margin-bottom: 3px; }}
.totals .row.bold {{ font-weight: 600; font-size: 12px; padding-top: 4px; }}

/* Coverage chart */
.coverage {{ padding: 14px 22px 18px; background: #faf6ed; border-top: 1px solid var(--rule); }}
.coverage h3 {{ font-family: 'Fraunces', serif; font-style: italic; font-weight: 400; font-size: 13px; color: var(--ink-soft); margin-bottom: 10px; }}
.cov-grid {{ display: grid; grid-template-columns: 80px 1fr; gap: 8px; align-items: center; margin-bottom: 5px; }}
.cov-label {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--ink-soft); }}
.cov-bar {{ display: flex; align-items: stretch; height: 22px; gap: 1px; }}
.cov-cell {{ flex: 1; display: flex; align-items: center; justify-content: center; font-family: 'JetBrains Mono', monospace; font-size: 9px; }}
.cov-need {{ background: var(--rule); color: var(--ink); }}
.cov-have-ok {{ background: var(--jr); color: white; }}
.cov-have-low {{ background: var(--md); color: white; }}
.cov-have-high {{ background: #c9b489; color: white; }}
.cov-have-empty {{ background: #f4f1eb; color: var(--ink-soft); }}
.cov-ticks {{ display: grid; grid-template-columns: 80px 1fr; gap: 8px; margin-bottom: 4px; }}
.cov-tick-row {{ display: flex; gap: 1px; }}
.cov-tick-cell {{ flex: 1; text-align: center; font-family: 'JetBrains Mono', monospace; font-size: 9px; color: var(--ink-soft); }}

.key {{ display: flex; gap: 18px; flex-wrap: wrap; align-items: center; margin-bottom: 20px; font-size: 11px; color: var(--ink-soft); }}
.key .item {{ display: flex; align-items: center; gap: 6px; }}
.key .sw {{ width: 14px; height: 14px; border-radius: 50%; }}
.sw-jr {{ background: var(--jr); }} .sw-md {{ background: var(--md); }} .sw-sr {{ background: var(--sr); }}

.footnote {{ margin-top: 20px; padding: 14px 18px; background: white; border: 1px solid var(--rule); font-size: 11px; line-height: 1.6; color: var(--ink-soft); }}
.footnote strong {{ color: var(--ink); }}

@media print {{ body {{ padding: 14px; background: white; }} }}
</style></head>
<body>
<header class="masthead">
  <h1>FOH schedule <em>— current vs proposed</em></h1>
  <div class="sub">All shifts &middot; Junior $19 &middot; Mid $21 &middot; Senior $23</div>
</header>

<section class="summary">
  <div class="col">
    <div class="lbl">Weekly cost — current</div>
    <div class="big">${week_cur_cost:,.0f}</div>
    <div class="meta">{week_cur_hrs:.1f} paid hrs &middot; {cur_tier_str}</div>
  </div>
  <div class="col">
    <div class="lbl">Weekly cost — proposed</div>
    <div class="big">${week_prop_cost:,.0f}</div>
    <div class="meta">{week_prop_hrs:.1f} paid hrs &middot; {prop_tier_str}</div>
  </div>
  <div class="col">
    <div class="lbl">Net change</div>
    <div class="big delta {delta_class}">{delta_arrow} ${abs(delta_cost):,.0f}</div>
    <div class="meta">{delta_hrs:+.1f} hrs/week</div>
  </div>
</section>

<div class="key">
  <div class="item"><span class="sw sw-jr"></span>Junior ($19/hr)</div>
  <div class="item"><span class="sw sw-md"></span>Mid ($21/hr)</div>
  <div class="item"><span class="sw sw-sr"></span>Senior ($23/hr)</div>
</div>

{''.join(days_html)}

<div class="footnote">
  <p><strong>How to read the coverage strip.</strong> Top row of each strip shows how many people the data says you'd want for each hour (1, 2, 3, or 4). Bottom row shows how many you actually have scheduled. Green = matched or over. Orange = under-staffed. Tan = over-staffed (3 hands when 2 was enough).</p>
  <p><strong>Why the proposed schedule starts the 2nd person earlier.</strong> Your morning demand crosses the "2-person needed" threshold around 10am on weekdays and 8-9am on weekends. The current schedule has the 2nd person arriving at 11:30am Mon-Thu, which leaves the opener handling rising demand alone for ~3 hours. Proposed: 2nd person arrives at 9:30am M-Thu, 8:30-9am weekends.</p>
  <p><strong>What got trimmed to pay for it.</strong> The senior closer's start time shifted later (1pm Mon-Thu instead of 12:30pm), and the dish closer's end time shifted earlier where possible — the model says you don't need 3 people 5-7pm on a typical weekday.</p>
</div>

</body>
</html>
"""
    output_path.write_text(html)


def _render_day_block(r, projection):
    """Render one day's current/proposed side-by-side + coverage strip."""

    def shifts_html(shifts: List[FOHShift]) -> str:
        items = []
        for s in shifts:
            br = f" (break {_min2str(s.break_start_min)}–{_min2str(s.break_end_min)})" if s.has_break else ""
            items.append(
                f'<li>'
                f'<span class="tier-dot tier-{s.tier}"></span>'
                f'<span class="lbl">{s.label}</span>'
                f'<span class="time">{s.start_str}–{s.end_str}</span>'
                f'<span class="cost">${s.cost:.0f} · {s.paid_hours:.1f}h</span>'
                f'</li>'
            )
        return f'<ul class="shift-list">{"".join(items)}</ul>'

    def totals_html(label_a, shifts) -> str:
        cost, hrs, tier = schedule_cost(shifts)
        tier_breakdown = " · ".join(
            f"${tier.get(t,0):.0f} {t}" for t in ('jr','md','sr') if tier.get(t, 0) > 0
        )
        return (f'<div class="totals">'
                f'<div class="row bold"><span>{label_a}</span><span>${cost:,.0f} &middot; {hrs:.1f}h</span></div>'
                f'<div class="row"><span>{tier_breakdown}</span></div>'
                f'</div>')

    cov_html = _render_coverage(r, projection)

    delta = r["prop_cost"] - r["cur_cost"]
    if abs(delta) < 5:
        pill_cls, pill_text = "flat", f"≈ ${abs(delta):.0f}"
    elif delta < 0:
        pill_cls, pill_text = "down", f"− ${abs(delta):,.0f}"
    else:
        pill_cls, pill_text = "up", f"+ ${abs(delta):,.0f}"

    return f"""
<section class="day">
  <div class="day-head">
    <div class="name">
      <div class="dnum">{r['short']}</div>
      <div class="dlabel">{r['name']}</div>
    </div>
    <div class="delta-pill {pill_cls}">{pill_text} vs. current</div>
  </div>
  <div class="day-body">
    <div class="side">
      <h3><span class="marker">NOW</span>Current</h3>
      {shifts_html(r['cur'])}
      {totals_html("Total", r['cur'])}
    </div>
    <div class="side">
      <h3><span class="marker">NEW</span>Proposed</h3>
      {shifts_html(r['prop'])}
      {totals_html("Total", r['prop'])}
    </div>
  </div>
  <div class="coverage">
    {cov_html}
  </div>
</section>
"""


def _min2str(m: Optional[int]) -> str:
    if m is None: return ""
    h, mm = divmod(m, 60)
    return f"{h:02d}:{mm:02d}"


def _render_coverage(r, projection) -> str:
    """
    Build a per-hour coverage chart for this day:
      Row 1: hour labels
      Row 2: required headcount based on demand
      Row 3: actual headcount in current schedule
      Row 4: actual headcount in proposed schedule
    """
    dow = r["dow"]
    open_h = config.HOURS_BY_DOW[dow].opener_start.hour
    close_h = (CLOSE_BY_DOW[dow] + 60) // 60  # include +60min closer overhang
    hours = list(range(open_h, close_h + 1))

    # Required headcount per hour (from demand)
    req = []
    for h in hours:
        p = projection.get((dow, h))
        foh = p.foh_sales if p else 0.0
        req.append(required_for_hour(foh))

    def cov_cell(have: float, need: int) -> str:
        if have == 0:
            cls = "cov-have-empty"
            display = "—"
        else:
            display = f"{have:.1f}"
            if have < need - 0.25:
                cls = "cov-have-low"
            elif have > need + 0.5:
                cls = "cov-have-high"
            else:
                cls = "cov-have-ok"
        return f'<div class="cov-cell {cls}">{display}</div>'

    cur_cov = r["cur_cov"]
    prop_cov = r["prop_cov"]

    tick_row = ''.join(f'<div class="cov-tick-cell">{h:02d}</div>' for h in hours)
    need_row = ''.join(f'<div class="cov-cell cov-need">{n}</div>' for n in req)
    cur_row  = ''.join(cov_cell(cur_cov.get(h, 0), req[i]) for i, h in enumerate(hours))
    prop_row = ''.join(cov_cell(prop_cov.get(h, 0), req[i]) for i, h in enumerate(hours))

    return f"""
<h3>Coverage by hour — need vs. scheduled</h3>
<div class="cov-ticks">
  <div class="cov-label"></div>
  <div class="cov-tick-row">{tick_row}</div>
</div>
<div class="cov-grid">
  <div class="cov-label">Demand says</div>
  <div class="cov-bar">{need_row}</div>
</div>
<div class="cov-grid">
  <div class="cov-label">Current</div>
  <div class="cov-bar">{cur_row}</div>
</div>
<div class="cov-grid">
  <div class="cov-label">Proposed</div>
  <div class="cov-bar">{prop_row}</div>
</div>
"""


if __name__ == "__main__":
    uploads = Path("/mnt/user-data/uploads")
    files = TastFiles(
        sales_by_day=uploads / "Sales_by_day.csv",
        time_of_day=uploads / "Time_of_day__totals_.csv",
        day_of_week=uploads / "Day_of_week__totals_.csv",
        sales_category=uploads / "Sales_category_summary.csv",
    )
    projection, _, _ = build_projection_from_csvs(files)
    out = Path(config.OUTPUT_DIR) / "foh_schedule_comparison.html"
    render_comparison_html(projection, out)
    print(f"✓ Comparison: {out}")
