"""
Sheet 2: Forward projection dashboard for May 25-31, 2026.

Renders the projected upcoming week with:
  - Summary at top: factors driving the projection, potential issues + fixes,
    labor trim status
  - Daily breakdown with adjusted hourly orders/sales
  - Pinch-point highlighting on hours where adjusted demand creates new
    coverage gaps

Reuses CSS and styling from weekly_dashboard.py.
"""

from __future__ import annotations
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import config
from forward_projection import (
    build_week_summary, PROJECTED_DATES, PROJECTED_WEEK_START,
    HourProjection, PinchPoint, WeekConfig,
    CURRENT_WEEK_CONFIG, FORWARD_WEEK_CONFIG,
)
from shift_optimizer import (
    FOHShift, PROPOSED_SCHEDULE, schedule_cost,
)
from break_scheduler import assign_breaks
from weekly_dashboard import (
    BOHShift, build_hourly_metrics, foh_need_for_hour, foh_scheduled_for_hour,
    boh_shifts_for_day, sequential_color, sequential_color_yellow, headcount_color,
    DISPLAY_HOURS,
)


DAY_NAMES_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday",
                  "Friday", "Saturday", "Sunday"]
DAY_NAMES_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _role_class_for_label(label: str) -> str:
    return "closer" if label.lower().startswith("closer") else "front"


def _min_to_str(m: int) -> str:
    return f"{m//60:02d}:{m%60:02d}"


def _pct_left(minute: int, hour_start: int = 6, hour_end: int = 24) -> float:
    span = (hour_end - hour_start) * 60
    return (minute - hour_start * 60) / span * 100


def render_sheet_2(uploads_dir: Path, week_config: Optional[WeekConfig] = None) -> str:
    """Render the forward-projection sheet HTML body (no <html>/<head>)."""
    if week_config is None:
        week_config = FORWARD_WEEK_CONFIG
    week_dates = week_config.week_dates
    summary, projection = build_week_summary(uploads_dir, week_config)
    orders_proj, sales_proj = build_hourly_metrics(uploads_dir)

    # Per-(dow,hour,item) historical avg qty — TWO views:
    #  - item_hourly_avg: 8-week historical baseline (for "vs avg" comparison)
    #  - item_hourly_last: last week only (the projection anchor — captures
    #    the Nate.Eatz lift in the actual data)
    from item_hourly import load_item_hourly
    from datetime import date as _date
    item_hourly_avg = load_item_hourly(uploads_dir)
    item_hourly_last = load_item_hourly(
        uploads_dir,
        date_from=_date(2026, 6, 15),
        date_to=_date(2026, 6, 21),
    )

    # Compute totals
    total_sales = summary.projected_total_sales
    week_foh = 0.0
    week_boh = 0.0
    week_foh_hrs = 0.0
    week_boh_hrs = 0.0
    week_foh_shift_count = 0
    week_boh_shift_count = 0
    day_blocks: List[str] = []
    per_day_labor: List[dict] = []   # per-day projected FOH/BOH $ + sales, for the labor outlook

    # Quick lookup for pinch points by (date, hour)
    pinch_lookup: Dict[Tuple[date, int], PinchPoint] = {
        (p.date, p.hour): p for p in summary.pinch_points
    }

    for d in week_dates:
        dow = d.weekday()
        adj_orders = {(dow, h): projection[(d, h)].adjusted_orders for h in range(6, 24)}
        adj_sales = {(dow, h): projection[(d, h)].adjusted_sales for h in range(6, 24)}
        daily_proj = {dow: sum(adj_sales.values())}
        from shift_optimizer import day_schedule
        shifts, _ = assign_breaks(
            day_schedule(dow, week_config.extra_foh_shifts, week_config.base_foh_overrides), dow,
            daily_proj, adj_orders, adj_sales,
        )
        boh = boh_shifts_for_day(dow)
        if week_config.label == "current week":
            from weekly_dashboard import current_week_extra_boh
            boh = boh + current_week_extra_boh(dow)
        # Overnight donut prep (pre-DF): current week and forward are the real
        # near-term operational weeks (still making donuts in-house), so both
        # carry it. Costed + listed, not charted.
        from weekly_dashboard import donut_prep_shifts
        donut = donut_prep_shifts(dow)
        donut_cost = sum(s.cost for s in donut)
        donut_hrs = sum(s.paid_hours for s in donut)
        foh_cost, foh_hrs, _ = schedule_cost(shifts)
        boh_cost = sum(s.cost for s in boh) + donut_cost
        boh_hrs = sum(s.paid_hours for s in boh if not s.is_manager) + donut_hrs
        week_foh += foh_cost
        week_boh += boh_cost
        week_foh_hrs += foh_hrs
        week_boh_hrs += boh_hrs
        week_foh_shift_count += len(shifts)
        week_boh_shift_count += len(boh) + len(donut)

        day_sales = sum(adj_sales.values())
        baseline_sales = sum(projection[(d, h)].historical_sales for h in range(6, 24))
        per_day_labor.append({
            "date": d, "dow": dow, "proj_sales": day_sales,
            "proj_foh": foh_cost, "proj_boh": boh_cost,
        })
        day_blocks.append(_render_day_block(
            d, dow, shifts, boh, adj_orders, adj_sales,
            day_sales, baseline_sales, projection,
            pinch_lookup, foh_cost, boh_cost,
            item_hourly_avg, item_hourly_last,
            uploads_dir, week_config, donut,
        ))

    mgr_weekly = (config.FOH_MANAGER_ANNUAL_SALARY + config.BOH_MANAGER_ANNUAL_SALARY) / 52.0
    week_labor = week_foh + week_boh + mgr_weekly
    week_pct = week_labor / total_sales * 100 if total_sales else 0

    foh_pct = week_foh / total_sales * 100 if total_sales else 0
    boh_pct = week_boh / total_sales * 100 if total_sales else 0
    mgr_pct = mgr_weekly / total_sales * 100 if total_sales else 0

    target_pct = config.LABOR_TARGET_PCT * 100
    diff = week_pct - target_pct
    target_labor = total_sales * config.LABOR_TARGET_PCT
    trim_amount = week_labor - target_labor
    HOURS_ASSUMED_WAGE = 20.0
    trim_hours = trim_amount / HOURS_ASSUMED_WAGE
    tol_pp = config.LABOR_WEEK_TOLERANCE * 100
    if diff > tol_pp:
        trim_cls = "trim-over"; trim_label = "Over goal — trim needed"; trim_arrow = "↓"
    elif diff < -tol_pp:
        trim_cls = "trim-under"; trim_label = "Under goal — room to add"; trim_arrow = "↑"
    elif trim_amount > 0:
        trim_cls = "trim-ok"; trim_label = "On target — slightly over, within tolerance"; trim_arrow = "→"
    elif trim_amount < 0:
        trim_cls = "trim-ok"; trim_label = "On target — slightly under, within tolerance"; trim_arrow = "→"
    else:
        trim_cls = "trim-ok"; trim_label = "At goal"; trim_arrow = "→"

    if abs(diff) <= tol_pp:
        verdict_cls = "ok"; verdict = f"on target ({diff:+.1f}pp)"
    elif diff > 0:
        verdict_cls = "over"; verdict = f"over by {diff:.1f}pp"
    else:
        verdict_cls = "under"; verdict = f"under by {-diff:.1f}pp"

    sales_delta = total_sales - summary.historical_baseline_sales
    sales_delta_pct = sales_delta / summary.historical_baseline_sales * 100 if summary.historical_baseline_sales else 0

    # ---- Build daily breakdown table (recency × baseline → projected) ----
    DAY_NAMES_SHORT_FULL = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    daily_rows_html = ""
    for d in week_dates:
        dow = d.weekday()
        # Use the recency factor actually applied to this date (includes any
        # current-week cool-off), not the uniform pre-cool-off factor.
        recency = projection[(d, 6)].recency_factor if (d, 6) in projection \
            else summary.recency_factors.get(dow, 1.0)
        baseline_d = sum(projection[(d, h)].historical_sales for h in range(6, 24))
        projected_d = sum(projection[(d, h)].adjusted_sales for h in range(6, 24))
        net_pct = (projected_d / baseline_d - 1) * 100 if baseline_d else 0
        net_cls = "delta-up" if net_pct >= 0 else "delta-down"
        d_str = d.strftime("%-m/%-d")
        daily_rows_html += (
            f'<tr>'
            f'<td class="day-cell">{DAY_NAMES_SHORT_FULL[dow]} {d_str}</td>'
            f'<td class="num">{(recency-1)*100:+.1f}%</td>'
            f'<td class="num">${baseline_d:,.0f}</td>'
            f'<td class="num strong">${projected_d:,.0f}</td>'
            f'<td class="num {net_cls}">{net_pct:+.0f}%</td>'
            f'</tr>'
        )
    daily_table_html = f"""<table class="daily-summary">
  <thead><tr>
    <th>Day</th><th>Recency</th><th>Baseline</th><th>Projected</th><th>Net</th>
  </tr></thead>
  <tbody>
{daily_rows_html}
  </tbody>
</table>"""

    # ---- Build factors HTML (what went into the projection) ----
    # Items starting with "#SUB:" are sub-bullets nested under the previous
    # main item. Walk the list and group accordingly.
    factor_groups = []  # list of (main_html, [sub_html, ...])
    for f in summary.factors_used:
        if f.startswith("#SUB:"):
            sub_text = f[5:].strip()
            if factor_groups:
                factor_groups[-1][1].append(sub_text)
        else:
            factor_groups.append((f, []))

    factor_lines = []
    for main, subs in factor_groups:
        if subs:
            sub_html = "".join(f'<li>{s}</li>' for s in subs)
            factor_lines.append(
                f'<li class="factor-with-subs">{main}<ul class="factor-subs">{sub_html}</ul></li>'
            )
        else:
            factor_lines.append(f'<li>{main}</li>')
    factors_html = "\n".join(factor_lines)

    # ---- Build issues HTML — condensed to per-day summary ----
    # Group pinch points by date. For each day, summarize: number of gap hours,
    # peak gap (highest delta), and a single consolidated suggestion.
    if summary.pinch_points:
        by_date: Dict[date, List] = {}
        for p in summary.pinch_points:
            by_date.setdefault(p.date, []).append(p)
        issues_items = []
        for d in week_dates:
            pps = by_date.get(d, [])
            dn = DAY_NAMES_SHORT[d.weekday()]
            d_str = d.strftime("%-m/%-d")
            if not pps:
                issues_items.append(
                    f'<li class="no-issues-day"><span class="when">{dn} {d_str}</span>'
                    f' &middot; <span class="ok-text">No coverage gaps</span></li>'
                )
                continue
            # Peak gap hour
            peak = max(pps, key=lambda p: p.delta)
            n_gaps = len(pps)
            # Range of hours
            hours = sorted(p.hour for p in pps)
            if len(hours) > 1:
                hour_range = f"{hours[0]:02d}:00–{hours[-1]:02d}:00"
            else:
                hour_range = f"{hours[0]:02d}:00"
            # Find most common suggestion (or use peak's)
            from collections import Counter
            sug_counts = Counter(p.suggestion for p in pps if p.suggestion)
            top_sug = sug_counts.most_common(1)[0][0] if sug_counts else peak.suggestion
            sev_class = "sev-issue" if peak.severity == "issue" else "sev-warning"
            issues_items.append(
                f'<li class="{sev_class}"><span class="when">{dn} {d_str}</span>'
                f' &middot; <strong>{n_gaps}</strong> gap hour{"s" if n_gaps != 1 else ""} ({hour_range})'
                f' &middot; peak need <strong>{peak.need}</strong> at {peak.hour:02d}:00 (scheduled {peak.scheduled:.0f})'
                f' &middot; <span class="fix">{top_sug}</span></li>'
            )
        issues_html = "\n".join(issues_items)
    else:
        issues_html = '<li class="no-issues">No new coverage gaps from projection — existing schedule should hold.</li>'

    # ---- Week range string ----
    week_range = (
        f"{summary.week_start.strftime('%a %b %-d')} "
        f"– {summary.week_end.strftime('%a %b %-d, %Y')}"
    )

    days_html = "\n".join(day_blocks)

    # Title varies by week config
    is_current = week_config.label == "current week"
    masthead_title = "Current Week" if is_current else "Forward Projection"
    masthead_sub = "Projected current week" if is_current else "Projected upcoming week"

    # ---- Projection accuracy table (current week only) ----
    # Locked projection (frozen when this week rolled from forward -> current)
    # vs actualized Toast sales, with deltas. Drives the "tune until no delta"
    # loop: the goal is to shrink Δ on the forward projection before it locks.
    accuracy_section_html = ""
    if is_current:
        try:
            from projection_lock import get_week_lock
            from csv_analyzer import actual_sales_by_date
            lock = get_week_lock(uploads_dir, week_config.week_dates[0])
            actuals = actual_sales_by_date(uploads_dir)
            locked_days = (lock or {}).get("days", {})

            rows = ""
            wtd_proj = wtd_act = 0.0
            n_actual = 0
            for d in week_dates:
                key = d.isoformat()
                proj = locked_days.get(key)
                act = actuals.get(d)
                d_label = f"{DAY_NAMES_SHORT[d.weekday()]} {d.strftime('%-m/%-d')}"
                proj_cell = f"${proj:,.0f}" if proj is not None else "—"
                if act is not None and proj is not None:
                    delta = act - proj
                    dpct = (delta / proj * 100) if proj else 0
                    dcls = "delta-up" if delta >= 0 else "delta-down"
                    act_cell = f"${act:,.0f}"
                    delta_cell = f'<td class="num {dcls}">{delta:+,.0f}</td>' \
                                 f'<td class="num {dcls}">{dpct:+.1f}%</td>'
                    wtd_proj += proj
                    wtd_act += act
                    n_actual += 1
                else:
                    act_cell = '<span class="pending">pending</span>'
                    delta_cell = '<td class="num pending">—</td><td class="num pending">—</td>'
                rows += (
                    f'<tr><td class="day-cell">{d_label}</td>'
                    f'<td class="num">{proj_cell}</td>'
                    f'<td class="num strong">{act_cell}</td>'
                    f'{delta_cell}</tr>'
                )

            # Week-to-date + full-week footer rows
            if n_actual:
                wtd_delta = wtd_act - wtd_proj
                wtd_pct = (wtd_delta / wtd_proj * 100) if wtd_proj else 0
                wtd_cls = "delta-up" if wtd_delta >= 0 else "delta-down"
                wtd_row = (
                    f'<tr class="wtd-row"><td class="day-cell">Week-to-date ({n_actual}d)</td>'
                    f'<td class="num">${wtd_proj:,.0f}</td>'
                    f'<td class="num strong">${wtd_act:,.0f}</td>'
                    f'<td class="num {wtd_cls}">{wtd_delta:+,.0f}</td>'
                    f'<td class="num {wtd_cls}">{wtd_pct:+.1f}%</td></tr>'
                )
            else:
                wtd_row = (
                    '<tr class="wtd-row"><td class="day-cell">Week-to-date</td>'
                    '<td class="num pending" colspan="4">no actuals yet — fill in as Toast data lands</td></tr>'
                )
            locked_weekly = (lock or {}).get("weekly")
            full_proj = f"${locked_weekly:,.0f}" if locked_weekly else "—"
            full_row = (
                f'<tr class="full-row"><td class="day-cell">Full week (locked)</td>'
                f'<td class="num">{full_proj}</td>'
                f'<td class="num" colspan="3"></td></tr>'
            )

            lock_note = ""
            if lock and not lock.get("frozen"):
                lock_note = ('<div class="acc-note">⚠ Not yet frozen — this week is still '
                             'in the forward window; numbers update each render.</div>')
            else:
                lock_note = ('<div class="acc-note">Locked projection frozen at roll-over '
                             '(forward → current). The schedule below reflects the live '
                             '4-week-weighted model and may differ.</div>')

            # Auto-calibration status note
            from forward_projection import auto_calibration_factor as _acf
            from csv_analyzer import current_week_actuals
            _cal, _comp = _acf(projection, current_week_actuals(uploads_dir), week_dates)
            if _cal != 1.0 and _comp:
                arrow = "up" if _cal >= 1 else "down"
                lock_note += (
                    f'<div class="acc-note">Auto-calibration: '
                    f'<strong class="delta-{arrow}">{(_cal-1)*100:+.1f}%</strong> applied to '
                    f'not-yet-completed days, learned from the {len(_comp)} finalized day(s) '
                    f'(damped 50%, capped ±15%). Updates automatically as you add each day to '
                    f'<code>actuals.csv</code>.</div>'
                )

            # ---- Breakfast Sandwich projected vs actual ----
            from item_hourly import load_item_hourly
            from forward_projection import bscw_item_lifts
            from csv_analyzer import current_week_actuals
            _avg = load_item_hourly(uploads_dir)
            _bs_lift, _ = bscw_item_lifts(uploads_dir, week_config.lift_multiplier)
            _cw_actuals = current_week_actuals(uploads_dir)

            def _bs_proj(d):
                dow = d.weekday()
                return sum(_avg.get((dow, h, "Breakfast Sandwich"), 0.0)
                           for h in range(6, 24)) * _bs_lift

            bs_rows = ""
            bs_wtd_proj = bs_wtd_act = 0.0
            bs_n = 0
            for d in week_dates:
                d_label = f"{DAY_NAMES_SHORT[d.weekday()]} {d.strftime('%-m/%-d')}"
                bp = _bs_proj(d)
                ba = _cw_actuals.get(d, {}).get("bs")
                if ba is not None and d in _cw_actuals:
                    delta = ba - bp
                    dpct = (delta / bp * 100) if bp else 0
                    dcls = "delta-up" if delta >= 0 else "delta-down"
                    cells = (f'<td class="num strong">{ba:.0f}</td>'
                             f'<td class="num {dcls}">{delta:+.0f}</td>'
                             f'<td class="num {dcls}">{dpct:+.0f}%</td>')
                    bs_wtd_proj += bp
                    bs_wtd_act += ba
                    bs_n += 1
                else:
                    cells = ('<td class="num strong"><span class="pending">pending</span></td>'
                             '<td class="num pending">—</td><td class="num pending">—</td>')
                bs_rows += (f'<tr><td class="day-cell">{d_label}</td>'
                            f'<td class="num">{bp:.0f}</td>{cells}</tr>')
            if bs_n:
                bd = bs_wtd_act - bs_wtd_proj
                bpct = (bd / bs_wtd_proj * 100) if bs_wtd_proj else 0
                bcls = "delta-up" if bd >= 0 else "delta-down"
                bs_wtd_row = (
                    f'<tr class="wtd-row"><td class="day-cell">Week-to-date ({bs_n}d)</td>'
                    f'<td class="num">{bs_wtd_proj:.0f}</td>'
                    f'<td class="num strong">{bs_wtd_act:.0f}</td>'
                    f'<td class="num {bcls}">{bd:+.0f}</td>'
                    f'<td class="num {bcls}">{bpct:+.0f}%</td></tr>'
                )
            else:
                bs_wtd_row = ('<tr class="wtd-row"><td class="day-cell">Week-to-date</td>'
                              '<td class="num pending" colspan="4">no actuals yet</td></tr>')

            accuracy_section_html = f"""
<section class="proj-accuracy">
  <div class="acc-title">Projection vs Actual — Locked Forecast Scorecard</div>
  <table class="daily-summary accuracy">
    <thead><tr>
      <th>Day</th><th>Projected</th><th>Actual</th><th>Δ $</th><th>Δ %</th>
    </tr></thead>
    <tbody>
{rows}
{wtd_row}
{full_row}
    </tbody>
  </table>
  <div class="acc-subtitle">Breakfast Sandwich — projected (live model) vs actual</div>
  <table class="daily-summary accuracy bs-accuracy">
    <thead><tr>
      <th>Day</th><th>BS Proj</th><th>BS Actual</th><th>Δ</th><th>Δ %</th>
    </tr></thead>
    <tbody>
{bs_rows}
{bs_wtd_row}
    </tbody>
  </table>
  {lock_note}
</section>"""
        except Exception as _e:
            accuracy_section_html = ""

    # ---- Labor & blended outlook (current week only) ----
    labor_section_html = ""
    if is_current:
        try:
            labor_section_html = _render_labor_outlook(
                week_dates, per_day_labor, uploads_dir, total_sales, week_labor)
        except Exception:
            labor_section_html = ""

    return f"""
<header class="masthead">
  <h1>Knotted AD <em>— {masthead_title}</em></h1>
  <div class="sub">{masthead_sub} &middot; {week_range}</div>
</header>
{accuracy_section_html}
{labor_section_html}
<section class="projection-summary">
  <div class="proj-block proj-factors">
    <div class="proj-block-title">Projected Weekly Sales — Driver factors</div>
    <div class="proj-sales-row">
      <div class="proj-sales-amount">${total_sales:,.0f}</div>
      <div class="proj-sales-vs-base">
        Baseline ({getattr(config, 'BASELINE_WEEKS', 8)}-wk avg): ${summary.historical_baseline_sales:,.0f}
        &nbsp; · &nbsp;
        <span class="{'delta-down' if sales_delta < 0 else 'delta-up'}">
          {sales_delta:+,.0f} ({sales_delta_pct:+.1f}%)
        </span>
      </div>
    </div>
    {daily_table_html}
    <ul class="proj-factor-list">
{factors_html}
    </ul>
  </div>

  <div class="proj-block proj-issues">
    <div class="proj-block-title">Potential issues &amp; suggested fixes</div>
    <ul class="proj-issue-list">
{issues_html}
    </ul>
  </div>
</section>

<section class="goal-banner {trim_cls}">
  <div class="goal-left">
    <div class="goal-arrow">{trim_arrow}</div>
    <div>
      <div class="goal-label">{trim_label}</div>
      <div class="goal-detail">Projected {week_pct:.1f}% &middot; Target {target_pct:.0f}% &middot; Delta {abs(diff):.1f} pp</div>
    </div>
  </div>
  <div class="goal-right">
    <div class="goal-amount">${abs(trim_amount):,.0f}<span class="goal-unit">/wk</span></div>
    <div class="goal-annual">≈ {abs(trim_hours):.0f} labor hours/wk (at $20/hr assumed blended)</div>
  </div>
</section>

<section class="summary">
  <div class="col">
    <div class="k">Projected Weekly Sales</div>
    <div class="v">${total_sales:,.0f}</div>
    <div class="meta">{week_range}</div>
  </div>
  <div class="col">
    <div class="k">FOH Hourly</div>
    <div class="v">${week_foh:,.0f} <span class="v-pct">{foh_pct:.1f}%</span></div>
    <div class="meta">{week_foh_hrs:.1f} paid hrs · {week_foh_shift_count} shifts</div>
  </div>
  <div class="col">
    <div class="k">BOH Hourly</div>
    <div class="v">${week_boh:,.0f} <span class="v-pct">{boh_pct:.1f}%</span></div>
    <div class="meta">{week_boh_hrs:.1f} paid hrs · {week_boh_shift_count} shifts</div>
  </div>
  <div class="col">
    <div class="k">Manager Salaries</div>
    <div class="v">${mgr_weekly:,.0f} <span class="v-pct">{mgr_pct:.1f}%</span></div>
    <div class="meta">{int(config.MANAGER_SALARY_ALLOCATION*100)}% of ${(config.FOH_MANAGER_ANNUAL_SALARY_FULL + config.BOH_MANAGER_ANNUAL_SALARY_FULL)/1000:g}K/yr · {int(config.MANAGER_SALARY_ALLOCATION*100)}% to WCC</div>
  </div>
  <div class="col">
    <div class="k">Total Labor</div>
    <div class="v"><span class="pct {verdict_cls}">{week_pct:.1f}%</span> <span class="v-sub">${week_labor:,.0f}</span></div>
    <div class="meta">{verdict} of {target_pct:.0f}%</div>
  </div>
</section>

<div class="alloc-note">
  <strong>Note:</strong> Manager salaries are split 50 / 50 between Cafe Knotted AD and WCC.
  Only the {int(config.MANAGER_SALARY_ALLOCATION*100)}% allocated to this location is counted
  in the labor figures above. The other 50% sits on WCC's books.
</div>

{days_html}
"""


def _render_labor_outlook(
    week_dates: List[date],
    per_day_labor: List[dict],
    uploads_dir: Path,
    total_proj_sales: float,
    week_labor_incl_mgr: float,
) -> str:
    """Current-week labor + blended outlook.

    Per day: projected FOH$ / BOH$ / total hourly labor$, the actualized FOH$/BOH$
    (from Toast clock-in once available; 'pending' until then), labor$ delta, and an
    up-to-date labor% (vs actual sales on realized days, projected on the rest).
    Then a blended outlook: pure projection vs actual-so-far + projected-remainder,
    for both revenue and total-labor%.
    Manager salary is excluded here (it's salaried, shown separately in the summary).
    """
    from csv_analyzer import actual_sales_by_date, actual_labor_by_date
    act_sales = actual_sales_by_date(uploads_dir)
    act_labor = actual_labor_by_date(uploads_dir)
    pd_by_date = {p["date"]: p for p in per_day_labor}
    target_pct = config.LABOR_TARGET_PCT * 100

    def money(x: float) -> str:
        return f"${x:,.0f}"

    rows = ""
    proj_foh_sum = proj_boh_sum = proj_labor_sum = proj_sales_sum = 0.0
    blended_sales = blended_labor = 0.0
    any_actual_labor = False

    for d in week_dates:
        p = pd_by_date.get(d, {})
        proj_foh = p.get("proj_foh", 0.0)
        proj_boh = p.get("proj_boh", 0.0)
        proj_sales = p.get("proj_sales", 0.0)
        proj_labor = proj_foh + proj_boh
        proj_foh_sum += proj_foh
        proj_boh_sum += proj_boh
        proj_labor_sum += proj_labor
        proj_sales_sum += proj_sales

        d_label = f"{DAY_NAMES_SHORT[d.weekday()]} {d.month}/{d.day}"
        a_sales = act_sales.get(d)
        completed = a_sales is not None
        al = act_labor.get(d) or {}
        a_foh = al.get("foh_cost")
        a_boh = al.get("boh_cost")
        has_al = bool(al.get("has_split")) and completed

        blended_sales += a_sales if completed else proj_sales
        if has_al:
            a_labor = (a_foh or 0.0) + (a_boh or 0.0)
            blended_labor += a_labor
            any_actual_labor = True
        else:
            blended_labor += proj_labor

        foh_act_cell = money(a_foh) if (has_al and a_foh is not None) else '<span class="pending">pending</span>'
        boh_act_cell = money(a_boh) if (has_al and a_boh is not None) else '<span class="pending">pending</span>'

        if has_al:
            a_labor = (a_foh or 0.0) + (a_boh or 0.0)
            dl = a_labor - proj_labor
            dlp = (dl / proj_labor * 100) if proj_labor else 0.0
            dcls = "delta-up" if dl <= 0 else "delta-down"  # under projection = green
            delta_cells = f'<td class="num {dcls}">{dl:+,.0f}</td><td class="num {dcls}">{dlp:+.1f}%</td>'
            labor_pct = (a_labor / a_sales * 100) if a_sales else 0.0
        else:
            delta_cells = '<td class="num pending">—</td><td class="num pending">—</td>'
            denom = a_sales if (completed and a_sales) else proj_sales
            labor_pct = (proj_labor / denom * 100) if denom else 0.0

        pct_cls = "delta-up" if labor_pct <= target_pct else "delta-down"
        realized_tag = "" if completed else ' <span class="proj-tag">proj</span>'
        rows += (
            f'<tr><td class="day-cell">{d_label}{realized_tag}</td>'
            f'<td class="num">{money(proj_foh)}</td>'
            f'<td class="num strong">{foh_act_cell}</td>'
            f'<td class="num">{money(proj_boh)}</td>'
            f'<td class="num strong">{boh_act_cell}</td>'
            f'<td class="num">{money(proj_labor)}</td>'
            f'{delta_cells}'
            f'<td class="num {pct_cls}">{labor_pct:.1f}%</td></tr>'
        )

    proj_pct = (proj_labor_sum / proj_sales_sum * 100) if proj_sales_sum else 0.0
    blended_pct = (blended_labor / blended_sales * 100) if blended_sales else 0.0
    rev_delta = blended_sales - proj_sales_sum
    rev_delta_pct = (rev_delta / proj_sales_sum * 100) if proj_sales_sum else 0.0
    pct_delta = blended_pct - proj_pct

    footer = (
        f'<tr class="wtd-row"><td class="day-cell">Projected week</td>'
        f'<td class="num">{money(proj_foh_sum)}</td><td class="num"></td>'
        f'<td class="num">{money(proj_boh_sum)}</td><td class="num"></td>'
        f'<td class="num">{money(proj_labor_sum)}</td>'
        f'<td class="num"></td><td class="num"></td>'
        f'<td class="num">{proj_pct:.1f}%</td></tr>'
    )
    # Rolling week = actual labor on closed days + projected labor on the rest.
    # Below the projected week (negative delta) is favorable.
    roll_delta = blended_labor - proj_labor_sum
    roll_delta_pct = (roll_delta / proj_labor_sum * 100) if proj_labor_sum else 0.0
    roll_cls = "delta-up" if roll_delta <= 0 else "delta-down"  # under projection = green
    rolling_footer = (
        f'<tr class="wtd-row rolling-row"><td class="day-cell">Rolling week (act + proj)</td>'
        f'<td class="num"></td><td class="num"></td>'
        f'<td class="num"></td><td class="num"></td>'
        f'<td class="num strong">{money(blended_labor)}</td>'
        f'<td class="num {roll_cls}">{roll_delta:+,.0f}</td>'
        f'<td class="num {roll_cls}">{roll_delta_pct:+.1f}%</td>'
        f'<td class="num">{blended_pct:.1f}%</td></tr>'
    )

    rev_dcls = "delta-up" if rev_delta >= 0 else "delta-down"
    pct_dcls = "delta-up" if pct_delta <= 0 else "delta-down"  # lower labor% = green
    note = (
        "Actual FOH/BOH $ populate from Toast clock-in (Barista/Cashier → FOH, "
        "Production Cook → BOH) once the Labor scope is enabled; until then labor "
        "reflects the projected/scheduled cost. Manager salary is excluded here."
    )

    return f"""
<section class="labor-outlook">
  <div class="acc-title">Current Week — Labor: Projected vs Actual (hourly FOH + BOH)</div>
  <div class="labor-scroll">
  <table class="daily-summary accuracy labor-table">
    <thead><tr>
      <th>Day</th>
      <th>Proj FOH $</th><th>Act FOH $</th>
      <th>Proj BOH $</th><th>Act BOH $</th>
      <th>Proj Labor $</th><th>Δ $</th><th>Δ %</th>
      <th>Labor %</th>
    </tr></thead>
    <tbody>
{rows}
{footer}
{rolling_footer}
    </tbody>
  </table>
  </div>

  <div class="blended-outlook">
    <div class="blended-card">
      <div class="bl-k">Revenue — up-to-date</div>
      <div class="bl-v">{money(blended_sales)}</div>
      <div class="bl-meta">Projected {money(proj_sales_sum)} ·
        <span class="{rev_dcls}">{rev_delta:+,.0f} ({rev_delta_pct:+.1f}%)</span></div>
    </div>
    <div class="blended-card">
      <div class="bl-k">Total labor % — up-to-date</div>
      <div class="bl-v">{blended_pct:.1f}%</div>
      <div class="bl-meta">Projected {proj_pct:.1f}% ·
        <span class="{pct_dcls}">{pct_delta:+.1f} pp</span> · target {target_pct:.0f}%</div>
    </div>
  </div>
  <div class="acc-note">Rolling week = actual labor on closed days + projected labor on the
  remaining days; <strong>below the Projected week (green) means you're tracking under plan</strong>.
  The blended cards apply the same actual-so-far + projected-rest logic to revenue and labor %.
  {note}</div>
</section>"""


def _render_day_block(
    d: date, dow: int,
    shifts: List[FOHShift], boh: List[BOHShift],
    adj_orders: Dict, adj_sales: Dict,
    day_sales: float, baseline_sales: float,
    projection: Dict, pinch_lookup: Dict,
    foh_cost: float, boh_cost: float,
    item_hourly_avg: Dict,
    item_hourly_last: Dict,
    uploads_dir: Path,
    week_config: WeekConfig,
    donut: List[BOHShift] = None,
) -> str:
    name = DAY_NAMES_FULL[dow]
    short = DAY_NAMES_SHORT[dow]
    d_str = d.strftime("%-m/%-d")

    # Daily metadata for the right-side stats
    target_pct = config.LABOR_TARGET_PCT * 100
    daily_labor_pct = (foh_cost + boh_cost) / day_sales * 100 if day_sales else 0
    pct_cls = "ok" if abs(daily_labor_pct - target_pct) <= 2 else ("over" if daily_labor_pct > target_pct else "under")

    sales_delta = day_sales - baseline_sales
    sales_delta_pct = (sales_delta / baseline_sales * 100) if baseline_sales else 0
    delta_cls = "delta-up" if sales_delta >= 0 else "delta-down"

    # Render FOH bars (same as Sheet 1)
    def render_foh_bar(s: FOHShift) -> str:
        bl = _pct_left(s.start_min)
        bw = _pct_left(s.end_min) - bl
        brk = ""
        if s.has_break and s.break_start_min is not None:
            bs_rel = (s.break_start_min - s.start_min) / (s.end_min - s.start_min) * 100
            bw_rel = 30 / (s.end_min - s.start_min) * 100
            brk_h, brk_m = divmod(s.break_start_min, 60)
            brk_label = f"{brk_h:02d}:{brk_m:02d}"
            brk = (
                f'<div class="brk" style="left:{bs_rel}%;width:{bw_rel}%">'
                f'<span class="brk-label">{brk_label}</span>'
                f'</div>'
            )
        role = _role_class_for_label(s.label)
        unfilled = getattr(s, "unfilled", False)
        bar_cls = "b-unfilled" if unfilled else f"b-{role}"
        dot_cls = "dot-unfilled" if unfilled else f"dot-{role}"
        tbd = '<span class="tbd-tag">TBD</span>' if unfilled else ''
        return (
            f'<div class="shift-row">'
            f'<div class="lbl"><span class="dot {dot_cls}"></span>{s.label}{tbd}</div>'
            f'<div class="track">'
            f'<div class="bar {bar_cls}" style="left:{bl}%;width:{bw}%">'
            f'{s.start_str}–{s.end_str}{brk}</div>'
            f'</div></div>'
        )

    def render_boh_bar(b: BOHShift) -> str:
        bl = _pct_left(b.start_min)
        bw = _pct_left(b.end_min) - bl
        brk = ""
        if not b.is_manager and b.break_start_min and b.break_end_min:
            bs_rel = (b.break_start_min - b.start_min) / (b.end_min - b.start_min) * 100
            bw_rel = (b.break_end_min - b.break_start_min) / (b.end_min - b.start_min) * 100
            brk_h, brk_m = divmod(b.break_start_min, 60)
            brk_label = f"{brk_h:02d}:{brk_m:02d}"
            brk = (
                f'<div class="brk" style="left:{bs_rel}%;width:{bw_rel}%">'
                f'<span class="brk-label">{brk_label}</span>'
                f'</div>'
            )
        unfilled = getattr(b, "unfilled", False)
        if unfilled:
            cls = "b-unfilled"; dot_cls = "dot-unfilled"
        elif b.is_manager:
            cls = "b-boh-mgr"; dot_cls = "dot-boh-mgr"
        else:
            cls = "b-boh"; dot_cls = "dot-boh"
        tbd = '<span class="tbd-tag">TBD</span>' if unfilled else ''
        label = f"{b.label}"
        return (
            f'<div class="shift-row">'
            f'<div class="lbl"><span class="dot {dot_cls}"></span>{label}{tbd}</div>'
            f'<div class="track">'
            f'<div class="bar {cls}" style="left:{bl}%;width:{bw}%">'
            f'{b.start_str}–{b.end_str}{brk}</div>'
            f'</div></div>'
        )

    foh_bars = "\n".join(render_foh_bar(s) for s in shifts)
    boh_bars = "\n".join(render_boh_bar(b) for b in boh)

    # Donut prep (pre-DF): one row per shift, listed not charted (10pm-6:30am).
    donut_list = ""
    if donut:
        from weekly_dashboard import _overnight_hm
        rows = []
        for b in donut:
            rows.append(
                f'<div class="shift-row donut-row">'
                f'<div class="lbl"><span class="dot dot-donut"></span>{b.label}</div>'
                f'<div class="track"><span class="donut-note">'
                f'{_overnight_hm(b.start_min)}&ndash;{_overnight_hm(b.end_min)} &middot; '
                f'{b.paid_hours:g}h &middot; ${b.cost:,.0f} &middot; overnight, off-chart'
                f'</span></div>'
                f'</div>'
            )
        donut_list = "\n".join(rows)

    # Heatmap cells
    max_orders_global = max(adj_orders.values()) if adj_orders else 1.0
    max_sales_global = max(adj_sales.values()) if adj_sales else 1.0

    # Per-item max for color scaling (compute projected BS/CW for this day's hours)
    # Uniform-lift methodology: BS/CW projection = 8-week DOW baseline × the
    # same recency lift factor used for overall revenue (avoids the Mon-Wed
    # under-projection problem and keeps BS/CW consistent with revenue projection).
    # BS/CW projection = all-data DOW/hour baseline × ITEM-SPECIFIC recency lift.
    # The breakfast sandwich is the viral driver and surged far more than overall
    # revenue, so it gets its own lift rather than the blended revenue lift.
    from forward_projection import bscw_item_lifts, weekend_bscw_scale
    bs_lift, cw_lift = bscw_item_lifts(uploads_dir, week_config.lift_multiplier)
    # Weekend BS/CW bump to Nate.Eatz-week target (Fri/Sat/Sun); 1.0 otherwise.
    _wk_bs_scale, _wk_cw_scale = weekend_bscw_scale(uploads_dir, dow, bs_lift, cw_lift)
    def _proj_bscw(dow, h):
        bs_base = item_hourly_avg.get((dow, h, "Breakfast Sandwich"), 0.0)
        cw_base = item_hourly_avg.get((dow, h, "Chicken Caesar Wrap"), 0.0)
        return bs_base * bs_lift * _wk_bs_scale, cw_base * cw_lift * _wk_cw_scale

    proj_bs_by_h: Dict[int, float] = {}
    proj_cw_by_h: Dict[int, float] = {}
    for h in DISPLAY_HOURS:
        bs_p, cw_p = _proj_bscw(dow, h)
        proj_bs_by_h[h] = bs_p
        proj_cw_by_h[h] = cw_p
    # Shared max across BS and CW rows for comparable color intensity
    shared_item_max = max(
        max(proj_bs_by_h.values()) if proj_bs_by_h else 1.0,
        max(proj_cw_by_h.values()) if proj_cw_by_h else 1.0,
    )
    max_bs = shared_item_max
    max_cw = shared_item_max

    # Per-day totals: projected (last-week-anchored) vs 8-wk DOW avg
    bs_proj_day = sum(_proj_bscw(dow, h)[0] for h in range(6, 24))
    cw_proj_day = sum(_proj_bscw(dow, h)[1] for h in range(6, 24))
    bs_avg_day = sum(item_hourly_avg.get((dow, h, "Breakfast Sandwich"), 0.0)
                     for h in range(6, 24))
    cw_avg_day = sum(item_hourly_avg.get((dow, h, "Chicken Caesar Wrap"), 0.0)
                     for h in range(6, 24))

    # Pre-compute BS/CW sales per hour (for the two new heatmap rows).
    # BS/CW sales = projected BS qty × $12.50 + projected CW qty × $14.50.
    # Non-BS/CW sales = adjusted hourly sales - BS/CW sales (clipped to >=0).
    from forward_projection import NATE_BS_PRICE, NATE_CW_PRICE
    bscw_sales_by_h: Dict[int, float] = {}
    other_sales_by_h: Dict[int, float] = {}
    for h in DISPLAY_HOURS:
        bs_p, cw_p = _proj_bscw(dow, h)
        bscw = bs_p * NATE_BS_PRICE + cw_p * NATE_CW_PRICE
        total = adj_sales.get((dow, h), 0)
        bscw = min(bscw, total)
        other = max(0, total - bscw)
        bscw_sales_by_h[h] = bscw
        other_sales_by_h[h] = other
    # Shared max across BS+CW sales and Other sales rows for comparable color
    shared_sales_max = max(
        max(bscw_sales_by_h.values()) if bscw_sales_by_h else 1.0,
        max(other_sales_by_h.values()) if other_sales_by_h else 1.0,
    )

    # Daily totals for the two new rows (sum across full operating hours)
    bscw_sales_day = 0.0
    other_sales_day = 0.0
    for h in range(6, 24):
        bs_p, cw_p = _proj_bscw(dow, h)
        bscw_h = bs_p * NATE_BS_PRICE + cw_p * NATE_CW_PRICE
        total_h = adj_sales.get((dow, h), 0)
        bscw_h = min(bscw_h, total_h)
        bscw_sales_day += bscw_h
        other_sales_day += max(0, total_h - bscw_h)

    axis_cells = ""
    orders_cells = ""
    sales_cells = ""
    bscw_sales_cells = ""
    other_sales_cells = ""
    need_cells = ""
    sched_cells = ""
    bs_cells = ""
    cw_cells = ""

    # Build other_sales_proj for need rule (per-(dow,h) keyed to match adj_sales)
    other_sales_proj: Dict[Tuple[int, int], float] = {
        (dow, h): other_sales_by_h.get(h, 0) for h in DISPLAY_HOURS
    }

    for h in DISPLAY_HOURS:
        axis_cells += f'<div class="heat-cell axis">{h:02d}</div>'

        o = adj_orders.get((dow, h), 0)
        s = adj_sales.get((dow, h), 0)
        need = foh_need_for_hour(dow, h, adj_orders, adj_sales, other_sales_proj)
        sched_avg, sched_min = foh_scheduled_for_hour(shifts, h)

        # Pinch point highlight: thicker border + red tint on cell if (d, h) in pinch_lookup
        is_pinch = (d, h) in pinch_lookup
        pinch_class = " pinch" if is_pinch else ""

        if o > 0:
            bg = sequential_color(o, max_orders_global)
            orders_cells += f'<div class="heat-cell{pinch_class}" style="background:{bg};color:#1a1814">{o:.0f}</div>'
        else:
            orders_cells += f'<div class="heat-cell{pinch_class}" style="background:#f5f1ea;color:#9b9387">–</div>'

        if s > 0:
            bg = sequential_color(s, max_sales_global)
            sales_cells += f'<div class="heat-cell{pinch_class}" style="background:{bg};color:#1a1814">${s:.0f}</div>'
        else:
            sales_cells += f'<div class="heat-cell{pinch_class}" style="background:#f5f1ea;color:#9b9387">$0</div>'

        # BS/CW sales /hr and non-BS/CW sales /hr — the breakdown (yellow scale)
        bscw_s = bscw_sales_by_h.get(h, 0)
        if bscw_s > 0:
            bg = sequential_color_yellow(bscw_s, shared_sales_max)
            bscw_sales_cells += f'<div class="heat-cell" style="background:{bg};color:#1a1814">${bscw_s:.0f}</div>'
        else:
            bscw_sales_cells += f'<div class="heat-cell" style="background:#faf7e8;color:#9b9387">—</div>'

        other_s = other_sales_by_h.get(h, 0)
        if other_s > 0:
            bg = sequential_color_yellow(other_s, shared_sales_max)
            other_sales_cells += f'<div class="heat-cell" style="background:{bg};color:#1a1814">${other_s:.0f}</div>'
        else:
            other_sales_cells += f'<div class="heat-cell" style="background:#faf7e8;color:#9b9387">$0</div>'

        bg_need = headcount_color(need)
        need_cells += f'<div class="heat-cell{pinch_class}" style="background:{bg_need};color:#1a1814">{need}</div>'

        # FOH sched: show avg rounded to nearest 0.5 so partial coverage is visible.
        # Color by floor of rounded value (worst-case bound).
        import math
        rounded = round(sched_avg * 2) / 2
        bg_sched = headcount_color(math.floor(rounded))
        # If avg < need, make text red to flag the shortfall
        text_color = "#7a2818" if (rounded < need) else "#1a1814"
        if rounded == 0:
            sched_display = "—"
        elif rounded == int(rounded):
            sched_display = f'{int(rounded)}'
        else:
            sched_display = f'{rounded:.1f}'
        sched_cells += f'<div class="heat-cell{pinch_class}" style="background:{bg_sched};color:{text_color}">{sched_display}</div>'

        # BS/CW per-hour projected qty cells (use same sequential_color as orders/sales)
        bs_q = proj_bs_by_h.get(h, 0.0)
        if bs_q >= 0.5:
            bg = sequential_color(bs_q, max_bs)
            bs_cells += f'<div class="heat-cell" style="background:{bg};color:#1a1814">{bs_q:.0f}</div>'
        else:
            bs_cells += f'<div class="heat-cell" style="background:#f5f1ea;color:#9b9387">—</div>'

        cw_q = proj_cw_by_h.get(h, 0.0)
        if cw_q >= 0.5:
            bg = sequential_color(cw_q, max_cw)
            cw_cells += f'<div class="heat-cell" style="background:{bg};color:#1a1814">{cw_q:.0f}</div>'
        else:
            cw_cells += f'<div class="heat-cell" style="background:#f5f1ea;color:#9b9387">—</div>'

    # Get day adjustment description (for header)
    all_adj = week_config.adjustments_fn()
    day_adj_summary = all_adj[d].summary()
    has_adj = all_adj[d].has_adjustment()
    adj_chip = f'<span class="day-chip">{day_adj_summary}</span>' if has_adj else ''

    return f"""
<section class="day">
  <div class="day-head">
    <div class="day-title">
      <h2>{short} <span class="dlabel">{name} {d_str}</span></h2>
      {adj_chip}
    </div>
    <div class="stats">
      <div class="stat"><span class="lbl">Adjusted sales</span><span class="val">${day_sales:,.0f}</span></div>
      <div class="stat"><span class="lbl">vs Baseline</span><span class="val {delta_cls}">{sales_delta:+,.0f}<span class="pct-small">({sales_delta_pct:+.1f}%)</span></span></div>
      <div class="stat"><span class="lbl">FOH $</span><span class="val">${foh_cost:.0f}</span></div>
      <div class="stat"><span class="lbl">BOH $</span><span class="val">${boh_cost:.0f}</span></div>
      <div class="stat pct {pct_cls}"><span class="lbl">Labor %</span><span class="val">{daily_labor_pct:.1f}%</span></div>
    </div>
  </div>
  <div class="shifts">
{foh_bars}
{boh_bars}
{donut_list}
  </div>
  <div class="heatmaps">
    <div class="heat-row">
      <div class="lbl"></div>
      <div class="cells">{axis_cells}</div>
    </div>
    <div class="heat-row"><div class="lbl"><div class="name-row"><span class="name">orders</span><span class="unit">/hr</span></div></div><div class="cells">{orders_cells}</div></div>
    <div class="heat-row"><div class="lbl"><div class="name-row"><span class="name">sales</span><span class="unit">/hr</span></div></div><div class="cells">{sales_cells}</div></div>
    <div class="heat-row"><div class="lbl"><div class="name-row"><span class="name">BS+CW sales</span><span class="unit">/hr</span></div><div class="total-row"><span class="total-proj">${bscw_sales_day:.0f}</span><span class="total-avg">/ day</span></div></div><div class="cells">{bscw_sales_cells}</div></div>
    <div class="heat-row"><div class="lbl"><div class="name-row"><span class="name">Other sales</span><span class="unit">/hr</span></div><div class="total-row"><span class="total-proj">${other_sales_day:.0f}</span><span class="total-avg">/ day</span></div></div><div class="cells">{other_sales_cells}</div></div>
    <div class="heat-row compare"><div class="lbl"><div class="name-row"><span class="name">FOH need</span><span class="unit">people</span></div></div><div class="cells">{need_cells}</div></div>
    <div class="heat-row compare"><div class="lbl"><div class="name-row"><span class="name">FOH sched</span><span class="unit">people</span></div></div><div class="cells">{sched_cells}</div></div>
    <div class="heat-row"><div class="lbl"><div class="name-row"><span class="name">Bfast Sand</span><span class="unit">/hr</span></div><div class="total-row"><span class="total-proj">{bs_proj_day:.0f}</span><span class="total-avg">/ {bs_avg_day:.0f} avg</span></div></div><div class="cells">{bs_cells}</div></div>
    <div class="heat-row"><div class="lbl"><div class="name-row"><span class="name">Caesar Wrap</span><span class="unit">/hr</span></div><div class="total-row"><span class="total-proj">{cw_proj_day:.0f}</span><span class="total-avg">/ {cw_avg_day:.0f} avg</span></div></div><div class="cells">{cw_cells}</div></div>
  </div>
</section>
"""


# Additional CSS specific to Sheet 2
SHEET_2_CSS = """
.page-break { page-break-before: always; padding-top: 14px; }

.projection-summary {
  display: grid;
  grid-template-columns: 1.2fr 1fr;
  gap: 16px;
  margin-bottom: 18px;
}
.proj-block {
  background: white;
  border: 1px solid var(--rule);
  padding: 16px 18px;
}
.proj-block-title {
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--ink-soft);
  margin-bottom: 10px;
}
.proj-sales-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px dashed var(--rule);
}
.proj-sales-amount {
  font-family: 'Fraunces', serif;
  font-size: 26px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.proj-sales-vs-base {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--ink-soft);
}
.delta-up { color: #4a6b2a; }
.delta-down { color: #9d3812; }

.item-totals {
  display: flex;
  gap: 24px;
  margin-bottom: 18px;
  padding: 12px 16px;
  background: rgba(0,0,0,0.025);
  border-left: 3px solid #6b5e4f;
  font-family: 'Inter', sans-serif;
  font-size: 12px;
}
.item-totals .item-total {
  display: flex;
  align-items: baseline;
  gap: 10px;
}
.item-totals .lbl {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--ink-soft);
  font-weight: 600;
}
.item-totals .val { font-weight: 600; color: var(--ink); }
.item-totals .base { color: var(--ink-soft); }
.item-totals .delta { font-weight: 600; }

.proj-factor-list, .proj-issue-list {
  margin: 0;
  padding-left: 18px;
  font-family: 'Inter', sans-serif;
  font-size: 12px;
  line-height: 1.5;
  color: var(--ink);
}
.proj-factor-list li { margin-bottom: 4px; }
.proj-factor-list li.factor-with-subs { font-weight: 600; color: var(--ink); }
.proj-factor-list .factor-subs {
  margin: 4px 0 8px 0;
  padding-left: 18px;
  list-style: none;
  font-weight: 400;
  color: var(--ink-soft);
}
.proj-factor-list .factor-subs li {
  margin-bottom: 3px;
  position: relative;
  padding-left: 10px;
}
.proj-factor-list .factor-subs li::before {
  content: '›';
  position: absolute;
  left: 0;
  color: #6b5e4f;
  font-weight: 700;
}
.proj-issue-list li {
  margin-bottom: 6px;
  padding-left: 6px;
  list-style: none;
  position: relative;
}
.proj-issue-list li::before {
  content: '';
  position: absolute;
  left: -12px;
  top: 6px;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #c4541f;
}
.proj-issue-list li.sev-warning::before { background: #d4a015; }
.proj-issue-list li.no-issues::before { background: #4a6b2a; }
.proj-issue-list li.no-issues-day::before { background: #4a6b2a; }
.proj-issue-list li .when {
  font-family: 'JetBrains Mono', monospace;
  font-weight: 500;
  color: var(--ink);
}
.proj-issue-list li .fix {
  font-style: italic;
  color: var(--ink-soft);
}
.proj-issue-list li .ok-text {
  color: var(--ink-soft);
  font-style: italic;
}

/* Daily summary table — sits between sales-headline and factor list */
table.daily-summary {
  width: 100%;
  border-collapse: collapse;
  margin: 4px 0 14px 0;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
}
table.daily-summary th {
  text-align: right;
  font-weight: 500;
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ink-soft);
  padding: 6px 8px 4px 8px;
  border-bottom: 1px solid var(--rule);
}
table.daily-summary th:first-child { text-align: left; }
table.daily-summary td {
  padding: 4px 8px;
  border-bottom: 1px dashed rgba(0,0,0,0.06);
}
table.daily-summary tr:last-child td { border-bottom: none; }
table.daily-summary td.day-cell {
  font-weight: 500;
  color: var(--ink);
  white-space: nowrap;
}
table.daily-summary td.num { text-align: right; color: var(--ink); }
table.daily-summary td.num.strong { font-weight: 600; }
table.daily-summary td.delta-up { color: #4a6b2a; }
table.daily-summary td.delta-down { color: #c4541f; }

/* ---- Projection accuracy scorecard (current week, top of sheet) ---- */
section.proj-accuracy {
  margin: 0 0 18px 0;
  padding: 14px 18px;
  background: #fbfaf7;
  border: 1px solid var(--rule);
  border-left: 3px solid var(--opener);
  border-radius: 6px;
}
.proj-accuracy .acc-title {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ink-soft);
  margin-bottom: 8px;
}
table.daily-summary.accuracy { width: 100%; }
table.daily-summary.accuracy th:not(:first-child),
table.daily-summary.accuracy td.num { text-align: right; }
table.daily-summary.accuracy tr.wtd-row td {
  border-top: 1px solid var(--rule);
  border-bottom: none;
  font-weight: 600;
  padding-top: 6px;
}
table.daily-summary.accuracy tr.full-row td {
  border-bottom: none;
  color: var(--ink-soft);
}
table.daily-summary.accuracy td.pending,
table.daily-summary.accuracy .pending {
  color: var(--ink-soft);
  font-style: italic;
  font-weight: 400;
}
.proj-accuracy .acc-note {
  margin-top: 8px;
  font-size: 10.5px;
  color: var(--ink-soft);
  line-height: 1.45;
}
.proj-accuracy .acc-subtitle {
  margin: 14px 0 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--ink-soft);
}
table.daily-summary.bs-accuracy td.num,
table.daily-summary.bs-accuracy th:not(:first-child) { text-align: right; }

/* Day-header chip for adjustment summary */
.day-title { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
.day-chip {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  background: rgba(196, 84, 31, 0.10);
  color: #7a2818;
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 500;
}

/* Pinch-point cell highlight */
.heat-cell.pinch {
  box-shadow: inset 0 0 0 2px #c4541f;
  position: relative;
}

/* Per-day baseline delta in stats */
.stats .val .pct-small {
  font-size: 10px;
  font-weight: 400;
  margin-left: 3px;
  opacity: 0.7;
}

/* ---- Current-week labor outlook ---- */
section.labor-outlook {
  margin: 0 0 18px 0;
  padding: 14px 18px;
  background: #fbfaf7;
  border: 1px solid var(--rule);
  border-left: 3px solid var(--closer, #7a5c3e);
  border-radius: 6px;
}
.labor-outlook .labor-scroll { overflow-x: auto; }
table.labor-table { width: 100%; min-width: 640px; }
table.labor-table td.day-cell .proj-tag {
  font-family: 'JetBrains Mono', monospace;
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--ink-soft);
  background: rgba(0,0,0,0.05);
  padding: 1px 5px;
  border-radius: 8px;
  margin-left: 4px;
}
.blended-outlook {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 14px;
}
.blended-card {
  background: white;
  border: 1px solid var(--rule);
  border-radius: 6px;
  padding: 10px 14px;
}
.blended-card .bl-k {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--ink-soft);
  margin-bottom: 4px;
}
.blended-card .bl-v {
  font-family: 'Fraunces', serif;
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.01em;
}
.blended-card .bl-meta {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10.5px;
  color: var(--ink-soft);
  margin-top: 2px;
}
"""
