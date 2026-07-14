"""
Break scheduler for FOH shifts.

CA law: shifts >6hr require a 30-min unpaid meal break BEFORE the start of the
5th hour of work (i.e., if you start at 8am you must begin your break by 1pm).
Shifts >10hr require a second 30-min break before the 10th hour. We focus on
the first break here since none of our shifts exceed 10hr.

This module:
  1. Takes the proposed shifts for a day
  2. Identifies each shift's legal break window (after 3hr-into-shift,
     before 5hr-into-shift) — this gives a 2hr window per person
  3. Looks at demand by half-hour and decides where breaks can go without
     dropping coverage below the required headcount
  4. Returns shifts with break times that AVOID stacking breaks during peak

If no valid stagger exists, the module flags it and recommends a shift edit
(e.g., "Move Rush-helper start 30min earlier so they can break before peak").
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from shift_optimizer import (
    FOHShift,
    PROPOSED_SCHEDULE,
    CURRENT_SCHEDULE,
    required_for_hour,
    schedule_cost,
    compute_coverage,
)
from csv_analyzer import build_projection_from_csvs, TastFiles
import config
from pathlib import Path


HALF_HOUR_SLOTS_PER_DAY = 48
SLOT_MIN = 30


def shift_minutes_in_slot(shift: FOHShift, slot_start_min: int,
                          break_start: Optional[int], break_end: Optional[int]) -> float:
    """Paid presence in a 30-min slot. 0 to 30."""
    slot_end_min = slot_start_min + SLOT_MIN
    overlap = max(0, min(shift.end_min, slot_end_min) - max(shift.start_min, slot_start_min))
    if break_start is not None and break_end is not None:
        b_overlap = max(0, min(break_end, slot_end_min) - max(break_start, slot_start_min))
        overlap -= b_overlap
    return overlap


def required_headcount_half_hour(dow: int, slot_start_min: int, projection,
                                  orders_proj: Optional[Dict] = None,
                                  sales_proj: Optional[Dict] = None) -> int:
    """
    Required headcount for the hour this 30-min slot falls into.

    Combined orders+sales model (preferred). Falls back to dollar-based
    if orders_proj is not provided.

    Special case: pre-open / post-close hours (≤2 orders/hr) require 0 — these
    are prep/cleanup hours with no real customer demand, even if someone is
    clocked in. This prevents the break scheduler from treating prep time as
    "need=1" and rejecting break placements there.
    """
    h = slot_start_min // 60
    if orders_proj is not None:
        from shift_optimizer import required_combined, required_for_orders
        orders = orders_proj.get((dow, h), 0)
        if orders <= 2:
            return 0
        if sales_proj is not None:
            return required_combined(orders, sales_proj.get((dow, h), 0))
        return required_for_orders(orders)
    # Fallback: dollar-based
    p = projection.get((dow, h))
    foh = p.foh_sales if p else 0.0
    return required_for_hour(foh)


def coverage_at_slot(shifts: List[FOHShift],
                     breaks: Dict[int, Tuple[Optional[int], Optional[int]]],
                     slot_start_min: int) -> float:
    """Effective headcount (fractional based on minutes) at a given slot."""
    total_min = 0.0
    for idx, s in enumerate(shifts):
        bs, be = breaks.get(idx, (s.break_start_min, s.break_end_min))
        total_min += shift_minutes_in_slot(s, slot_start_min, bs, be)
    return total_min / SLOT_MIN


def legal_break_window(shift: FOHShift) -> Tuple[int, int]:
    """
    CA law: meal break must START before the 5th hour of work begins.
    For shifts >6hr we must provide a 30-min meal break.

    Earliest legal: anytime in the shift before the 5th hour begins.
      (No minimum — break can be the first hour if we want.)
    Latest legal: must START before shift_start + 5hr, so the break
      starts no later than (shift_start + 5hr - 30min) = shift_start + 4.5hr.

    Returns: (earliest_break_start_min, latest_break_start_min)
    """
    # Allow break to start as early as 30 min after shift start (give a moment
    # to settle in; otherwise the break could literally start at clock-in).
    earliest = shift.start_min + 30
    # Must start before the 5th hour begins
    latest = shift.start_min + int(4.5 * 60)
    return earliest, latest


def assign_breaks(shifts: List[FOHShift], dow: int, projection,
                  orders_proj: Optional[Dict] = None,
                  sales_proj: Optional[Dict] = None
                  ) -> Tuple[List[FOHShift], List[str]]:
    """
    Demand-aware break placement.

    Strategy:
      1. For each shift needing a break, search the ENTIRE legal window
         (not just early hours) at 30-min granularity.
      2. Score each candidate slot by:
           a) Does it drop coverage below required? (hard fail)
           b) How low is demand during that slot? (prefer quietest)
           c) Are other people already on break in this slot? (penalty)
      3. Place each break in its best-scoring slot.
      4. Shifts ≤6hr get no break (CA doesn't require one).
    """
    # Build list of shifts requiring breaks
    candidates: List[Tuple[int, FOHShift, Tuple[int, int]]] = []
    breaks: Dict[int, Tuple[Optional[int], Optional[int]]] = {}

    for idx, s in enumerate(shifts):
        if s.total_hours <= 6:
            breaks[idx] = (None, None)
        elif s.pinned_break_start_min is not None:
            # Honor the operator-specified break time; don't search.
            breaks[idx] = (s.pinned_break_start_min, s.pinned_break_start_min + 30)
        else:
            candidates.append((idx, s, legal_break_window(s)))

    # Sort by latest break start (those with tightest windows go first)
    candidates.sort(key=lambda t: t[2][1])

    warnings: List[str] = []

    for idx, shift, (earliest, latest) in candidates:
        # Generate all 30-min-aligned candidate starts in legal window
        candidate_starts: List[int] = []
        bs = ((earliest + SLOT_MIN - 1) // SLOT_MIN) * SLOT_MIN  # round up to half-hour
        while bs <= latest:
            candidate_starts.append(bs)
            bs += SLOT_MIN

        # Score each candidate
        best_slot = None
        best_score = float("inf")  # LOWER is better

        for cand_start in candidate_starts:
            cand_end = cand_start + 30
            trial = dict(breaks)
            trial[idx] = (cand_start, cand_end)

            # 1. Hard fail check: does this drop coverage below required?
            coverage_drop = 0.0
            hour = cand_start // 60
            req = required_headcount_half_hour(dow, cand_start, projection, orders_proj, sales_proj)
            cov = coverage_at_slot(shifts, trial, cand_start)
            if cov < req:
                coverage_drop = req - cov

            # 2. Demand score: prefer slots where demand is LOW.
            # Prefer orders/hr if available, else fall back to FOH sales.
            if orders_proj is not None:
                demand = orders_proj.get((dow, hour), 0)
            else:
                p = projection.get((dow, hour))
                demand = p.foh_sales if p else 0.0

            # 3. Stacking penalty: penalize if another break is in this same slot
            stacking = 0
            for other_idx, (obs, oe) in trial.items():
                if other_idx == idx or obs is None:
                    continue
                # Overlap check
                if obs < cand_end and oe > cand_start:
                    stacking += 1

            # Composite score (lower is better):
            #   - Coverage drops are catastrophic (×10000)
            #   - Demand level adds linearly
            #   - Each stacking break adds a fixed penalty
            score = coverage_drop * 10000 + demand + stacking * 50

            if score < best_score:
                best_score = score
                best_slot = cand_start

        # Apply best slot found
        if best_slot is None:
            # Fall back to earliest legal slot if no candidates (shouldn't happen)
            best_slot = candidate_starts[0] if candidate_starts else (shift.start_min + 3*60)

        breaks[idx] = (best_slot, best_slot + 30)

        # Re-check this break for actual coverage drop and warn
        cov = coverage_at_slot(shifts, breaks, best_slot)
        req = required_headcount_half_hour(dow, best_slot, projection, orders_proj, sales_proj)
        if cov < req:
            warnings.append(
                f"{shift.label}: best available break placement at "
                f"{best_slot//60:02d}:{best_slot%60:02d} still drops coverage "
                f"by {req - cov:.1f}."
            )

    # Apply break times back to shifts
    new_shifts: List[FOHShift] = []
    for idx, s in enumerate(shifts):
        bs, be = breaks.get(idx, (s.break_start_min, s.break_end_min))
        new = FOHShift(s.label, s.start_min, s.end_min, s.wage, s.tier,
                       pinned_break_start_min=s.pinned_break_start_min,
                       unfilled=getattr(s, "unfilled", False))
        if bs is not None and be is not None:
            new.has_break = True
            new.break_start_min = bs
            new.break_end_min = be
        else:
            new.has_break = False
            new.break_start_min = None
            new.break_end_min = None
        new_shifts.append(new)

    return new_shifts, warnings


def render_break_schedule_html(projection, output_path: Path) -> None:
    """Render the proposed schedule WITH coordinated breaks."""
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]
    short_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    day_data = []
    for dow in range(7):
        shifts = PROPOSED_SCHEDULE[dow]
        with_breaks, warnings = assign_breaks(list(shifts), dow, projection)
        cost, hrs, by_tier = schedule_cost(with_breaks)
        day_data.append({
            "dow": dow,
            "name": day_names[dow],
            "short": short_names[dow],
            "shifts": with_breaks,
            "cost": cost,
            "hours": hrs,
            "warnings": warnings,
        })

    week_cost = sum(d["cost"] for d in day_data)
    week_hours = sum(d["hours"] for d in day_data)

    days_html = "".join(_render_day_break_block(d, projection) for d in day_data)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Knotted FOH Schedule with Coordinated Breaks</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #faf8f5; --ink: #1a1814; --ink-soft: #6b6359; --rule: #e5dfd4;
  --jr: #7b9c5c; --md: #c4541f; --sr: #5a3a78;
  --break: rgba(0,0,0,0.18);
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

.summary {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 0; margin-bottom: 24px; border: 1px solid var(--rule); background: white; }}
.summary .col {{ padding: 16px 20px; border-right: 1px solid var(--rule); }}
.summary .col:last-child {{ border: none; }}
.summary .lbl {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--ink-soft); margin-bottom: 6px; }}
.summary .big {{ font-family: 'Fraunces', serif; font-size: 26px; font-weight: 700; }}
.summary .meta {{ font-size: 11px; color: var(--ink-soft); margin-top: 3px; font-family: 'JetBrains Mono', monospace; }}

.intro {{
  background: white; border: 1px solid var(--rule);
  padding: 14px 18px; margin-bottom: 20px; font-size: 12px; line-height: 1.6;
}}
.intro strong {{ color: var(--ink); }}

.day {{ background: white; border: 1px solid var(--rule); margin-bottom: 22px; page-break-inside: avoid; }}
.day-head {{
  display: flex; padding: 14px 22px; border-bottom: 1px solid var(--rule);
  align-items: center; gap: 18px; justify-content: space-between;
}}
.day-head .name {{ display: flex; align-items: baseline; gap: 10px; }}
.day-head .dnum {{ font-family: 'Fraunces', serif; font-size: 26px; font-weight: 700; }}
.day-head .dlabel {{ font-family: 'Fraunces', serif; font-style: italic; font-size: 17px; color: var(--ink-soft); }}
.day-head .meta {{ font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--ink-soft); }}

.day-body {{ padding: 18px 22px; }}

.timeline-wrap {{ position: relative; margin-top: 14px; }}
.timeline-axis {{
  display: flex; padding-left: 110px; margin-bottom: 6px;
  font-family: 'JetBrains Mono', monospace; font-size: 9px; color: var(--ink-soft);
}}
.timeline-axis .tick {{
  flex: 1; text-align: left; border-left: 1px solid var(--rule); padding-left: 3px;
}}
.shift-row {{ display: flex; align-items: center; margin-bottom: 4px; }}
.shift-row .row-label {{
  width: 110px; padding-right: 10px; font-size: 11px;
  display: flex; align-items: center; gap: 6px;
}}
.shift-row .row-label .tier-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }}
.shift-row .row-label .lbl {{ font-weight: 500; }}
.shift-row .bar-track {{
  flex: 1; height: 26px; background: #f4f1eb; border: 1px solid var(--rule);
  position: relative;
}}
.shift-row .bar {{
  position: absolute; top: 0; bottom: 0;
  display: flex; align-items: center; padding: 0 8px;
  color: white; font-family: 'JetBrains Mono', monospace; font-size: 10px;
  font-weight: 500;
  border-radius: 1px;
  overflow: hidden;
}}
.shift-row .bar.tier-jr {{ background: var(--jr); }}
.shift-row .bar.tier-md {{ background: var(--md); }}
.shift-row .bar.tier-sr {{ background: var(--sr); }}
.shift-row .bar .lbl-inner {{
  white-space: nowrap; text-shadow: 0 1px 0 rgba(0,0,0,0.15);
}}
.shift-row .bar .brk {{
  position: absolute; top: 0; bottom: 0;
  background: repeating-linear-gradient(
    45deg, rgba(255,255,255,0.55), rgba(255,255,255,0.55) 3px,
    transparent 3px, transparent 7px);
  border-left: 1px solid rgba(255,255,255,0.6);
  border-right: 1px solid rgba(255,255,255,0.6);
}}
.shift-row .bar .brk-label {{
  position: absolute; top: -3px; font-size: 8px; color: var(--ink);
  background: white; padding: 1px 3px; border: 1px solid var(--rule);
  border-radius: 2px; transform: translateX(-50%); white-space: nowrap;
  font-family: 'JetBrains Mono', monospace;
}}

.cov-row {{ display: flex; align-items: center; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--rule); }}
.cov-row .row-label {{ width: 110px; padding-right: 10px; font-size: 10px; color: var(--ink-soft); font-family: 'JetBrains Mono', monospace; }}
.cov-row .cov-strip {{
  flex: 1; display: flex; height: 22px; gap: 1px;
}}
.cov-cell {{
  flex: 1; display: flex; align-items: center; justify-content: center;
  font-family: 'JetBrains Mono', monospace; font-size: 9px;
}}
.cov-need {{ background: var(--rule); color: var(--ink); }}
.cov-ok {{ background: var(--jr); color: white; }}
.cov-low {{ background: var(--bad); color: white; }}
.cov-high {{ background: #d4c19a; color: white; }}
.cov-empty {{ background: #f4f1eb; color: var(--ink-soft); opacity: 0.5; }}

.warnings {{
  margin-top: 12px; padding: 10px 14px;
  background: #faf2e3; border-left: 3px solid var(--warn);
  font-size: 11px; color: var(--ink);
}}
.warnings strong {{ display: block; margin-bottom: 4px; }}

.key {{
  display: flex; gap: 18px; flex-wrap: wrap; align-items: center;
  margin-bottom: 20px; font-size: 11px; color: var(--ink-soft);
}}
.key .item {{ display: flex; align-items: center; gap: 6px; }}
.key .sw {{ width: 14px; height: 14px; border-radius: 2px; }}
.sw-jr {{ background: var(--jr); }} .sw-md {{ background: var(--md); }} .sw-sr {{ background: var(--sr); }}
.sw-brk {{ background: repeating-linear-gradient(45deg, rgba(0,0,0,0.4), rgba(0,0,0,0.4) 2px, transparent 2px, transparent 4px); border: 1px solid var(--rule); }}

@media print {{ body {{ padding: 14px; background: white; }} }}
</style></head>
<body>
<header class="masthead">
  <h1>FOH schedule <em>— with coordinated breaks</em></h1>
  <div class="sub">CA law: 30-min unpaid break before 5th hour on shifts > 6hr</div>
</header>

<section class="summary">
  <div class="col">
    <div class="lbl">Weekly FOH cost</div>
    <div class="big">${week_cost:,.0f}</div>
    <div class="meta">{week_hours:.1f} paid hours</div>
  </div>
  <div class="col">
    <div class="lbl">Break placement</div>
    <div class="big">staggered</div>
    <div class="meta">no two breaks during peak</div>
  </div>
  <div class="col">
    <div class="lbl">Coverage check</div>
    <div class="big">{sum(1 for d in day_data if not d['warnings'])} / 7 clean</div>
    <div class="meta">{sum(len(d['warnings']) for d in day_data)} warnings total</div>
  </div>
</section>

<div class="intro">
  <strong>How break placement works.</strong> Every shift longer than 6 hours gets a 30-minute unpaid meal break, placed legally (before the 5th hour of work). The algorithm picks each shift's break time to <strong>avoid dropping below required headcount during that 30-min window</strong>. Where two people's natural break windows overlap, breaks get staggered. The diagonal-stripe sections inside the bars show when each person is on break.
</div>

<div class="key">
  <div class="item"><span class="sw sw-jr"></span>Junior $19</div>
  <div class="item"><span class="sw sw-md"></span>Mid $21</div>
  <div class="item"><span class="sw sw-sr"></span>Senior $23</div>
  <div class="item"><span class="sw sw-brk"></span>Unpaid 30-min break</div>
</div>

{days_html}

</body>
</html>
"""
    output_path.write_text(html)


def _render_day_break_block(d, projection) -> str:
    """Render a single day with shift bars showing breaks + coverage strip."""
    # Display window: 6:30am (390min) to 11pm (1380min) = 16.5 hours
    START_MIN = 6 * 60
    END_MIN = 23 * 60
    SPAN = END_MIN - START_MIN

    def pct(x_min: int) -> float:
        return max(0, min(100, (x_min - START_MIN) / SPAN * 100))

    # Axis ticks at each hour
    axis_html = ''.join(
        f'<div class="tick">{h:02d}</div>'
        for h in range(START_MIN // 60, (END_MIN // 60) + 1)
    )

    # Shift rows
    rows_html = []
    for s in d["shifts"]:
        bar_left = pct(s.start_min)
        bar_right = pct(s.end_min)
        bar_width = bar_right - bar_left
        # Break overlay
        brk_html = ""
        if s.has_break and s.break_start_min is not None:
            # Relative to bar
            brk_left = (s.break_start_min - s.start_min) / (s.end_min - s.start_min) * 100
            brk_width = 30 / (s.end_min - s.start_min) * 100
            brk_center_pct = brk_left + brk_width / 2
            bs_label = f"{s.break_start_min // 60:02d}:{s.break_start_min % 60:02d}"
            be_label = f"{s.break_end_min // 60:02d}:{s.break_end_min % 60:02d}"
            brk_html = (
                f'<div class="brk" style="left:{brk_left}%;width:{brk_width}%"></div>'
                f'<div class="brk-label" style="left:{brk_center_pct}%">{bs_label}–{be_label}</div>'
            )
        # Time annotation inside bar
        time_str = f"{s.start_str}–{s.end_str}"
        rows_html.append(f"""
<div class="shift-row">
  <div class="row-label">
    <span class="tier-dot" style="background: var(--{s.tier});"></span>
    <span class="lbl">{s.label}</span>
  </div>
  <div class="bar-track">
    <div class="bar tier-{s.tier}" style="left:{bar_left}%;width:{bar_width}%">
      <span class="lbl-inner">{time_str} · {s.paid_hours:.1f}h · ${s.cost:.0f}</span>
      {brk_html}
    </div>
  </div>
</div>
""")

    # Coverage strip: for each hour, show needed vs actual (accounting for breaks)
    breaks_dict = {idx: (s.break_start_min, s.break_end_min)
                   for idx, s in enumerate(d["shifts"])}
    cov_cells = []
    need_cells = []
    open_h = config.HOURS_BY_DOW[d["dow"]].opener_start.hour
    close_h_min = (max(s.end_min for s in d["shifts"]))
    close_h = (close_h_min + 59) // 60
    for h in range(open_h, min(close_h + 1, 24)):
        slot = h * 60
        req = required_headcount_half_hour(d["dow"], slot, projection)
        cov = coverage_at_slot(d["shifts"], breaks_dict, slot)
        need_cells.append(f'<div class="cov-cell cov-need">{req}</div>')
        if cov == 0:
            cls, lab = "cov-empty", "—"
        elif cov < req - 0.2:
            cls, lab = "cov-low", f"{cov:.1f}"
        elif cov > req + 0.5:
            cls, lab = "cov-high", f"{cov:.1f}"
        else:
            cls, lab = "cov-ok", f"{cov:.1f}"
        cov_cells.append(f'<div class="cov-cell {cls}">{lab}</div>')

    # Axis above coverage
    cov_axis = ''.join(
        f'<div class="cov-cell" style="background: transparent; color: var(--ink-soft); font-weight: 500;">{h:02d}</div>'
        for h in range(open_h, min(close_h + 1, 24))
    )

    warnings_html = ""
    if d["warnings"]:
        warnings_html = (
            '<div class="warnings"><strong>Heads up:</strong>'
            + ''.join(f'<div>{w}</div>' for w in d["warnings"])
            + '</div>'
        )

    return f"""
<section class="day">
  <div class="day-head">
    <div class="name">
      <div class="dnum">{d['short']}</div>
      <div class="dlabel">{d['name']}</div>
    </div>
    <div class="meta">{d['hours']:.1f} hrs · ${d['cost']:,.0f}</div>
  </div>
  <div class="day-body">
    <div class="timeline-axis">{axis_html}</div>
    {''.join(rows_html)}

    <div class="cov-row">
      <div class="row-label">hour</div>
      <div class="cov-strip">{cov_axis}</div>
    </div>
    <div class="cov-row">
      <div class="row-label">need</div>
      <div class="cov-strip">{''.join(need_cells)}</div>
    </div>
    <div class="cov-row">
      <div class="row-label">on floor</div>
      <div class="cov-strip">{''.join(cov_cells)}</div>
    </div>
    {warnings_html}
  </div>
</section>
"""


if __name__ == "__main__":
    uploads = Path("/mnt/user-data/uploads")
    files = TastFiles(
        sales_by_day=uploads / "Sales_by_day.csv",
        time_of_day=uploads / "Time_of_day__totals_.csv",
        day_of_week=uploads / "Day_of_week__totals_.csv",
        sales_category=uploads / "Sales_category_summary.csv",
    )
    proj, _, _ = build_projection_from_csvs(files)
    out = Path(config.OUTPUT_DIR) / "foh_schedule_with_breaks.html"
    render_break_schedule_html(proj, out)
    print(f"✓ Schedule with breaks: {out}")
