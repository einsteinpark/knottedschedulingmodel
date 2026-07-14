"""
Compute and render an hour-by-hour labor budget for the week.

For each (date, hour) slot, compute:
  - Projected sales for that hour (from the demand projection)
  - Labor budget at target %: sales × LABOR_TARGET_PCT
  - Hourly portion of manager salaries (fixed cost spread across operating hours)
  - Hourly budget remaining for floor staff after manager allocation
  - Currently-scheduled labor cost for that hour
  - Headcount currently on the floor

This answers: "What should I spend on hourly labor each hour?"

Renders an HTML heatmap with the same aesthetic as the Gantt:
  - Each cell = an hour
  - Color = under budget / on budget / over
  - Cells show: $ budget (top), $ scheduled (middle), people on floor (bottom)
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import config
from csv_analyzer import build_projection_from_csvs, TastFiles


def _shift_minutes_in_hour(
    start_min: int, end_min: int, hour: int,
    break_start: int | None, break_end: int | None,
) -> float:
    """Paid minutes a shift contributes to a given hour (0-60)."""
    hr_start = hour * 60
    hr_end = (hour + 1) * 60
    overlap_start = max(start_min, hr_start)
    overlap_end = min(end_min, hr_end)
    if overlap_end <= overlap_start:
        return 0.0
    minutes = overlap_end - overlap_start
    if break_start is not None and break_end is not None:
        b_start = max(break_start, hr_start)
        b_end = min(break_end, hr_end)
        if b_end > b_start:
            minutes -= (b_end - b_start)
    return minutes


def compute_hourly_budget(
    schedule_csv: Path,
    week_start: date,
    uploads_dir: Path,
) -> Tuple[Dict[Tuple[date, int], dict], Dict]:
    """Returns (per-hour data, week meta)."""
    files = TastFiles(
        sales_by_day=uploads_dir / "Sales_by_day.csv",
        time_of_day=uploads_dir / "Time_of_day__totals_.csv",
        day_of_week=uploads_dir / "Day_of_week__totals_.csv",
        sales_category=uploads_dir / "Sales_category_summary.csv",
    )
    projection, _, _ = build_projection_from_csvs(files)

    # Load shifts
    shifts: List[dict] = []
    with schedule_csv.open() as f:
        for r in csv.DictReader(f):
            def hm(s):
                if not s: return None
                h, m = s.split(":")
                return int(h) * 60 + int(m)
            shifts.append({
                "date": date.fromisoformat(r["date"]),
                "role": r["role"],
                "start": hm(r["start"]),
                "end": hm(r["end"]),
                "break_start": hm(r["break_start"]),
                "break_end": hm(r["break_end"]),
            })

    # Compute total weekly operating hours (across the 7 days) for manager allocation
    weekly_open_hours = 0
    for i in range(7):
        d = week_start + timedelta(days=i)
        h = config.HOURS_BY_DOW[d.weekday()]
        weekly_open_hours += (h.close_time.hour - h.open_time.hour)

    mgr_weekly = (config.FOH_MANAGER_ANNUAL_SALARY +
                  config.BOH_MANAGER_ANNUAL_SALARY) / 52.0
    mgr_per_open_hour = mgr_weekly / weekly_open_hours if weekly_open_hours else 0

    result: Dict[Tuple[date, int], dict] = {}

    for i in range(7):
        d = week_start + timedelta(days=i)
        dow = d.weekday()
        hours_cfg = config.HOURS_BY_DOW[dow]
        # Report from opener_start through latest closer_end
        for h in range(hours_cfg.opener_start.hour, hours_cfg.close_time.hour + 2):
            p = projection.get((dow, h))
            proj_sales = p.total_sales if p else 0.0
            total_budget = proj_sales * config.LABOR_TARGET_PCT

            # Manager portion: only applied during open hours
            is_open = hours_cfg.open_time.hour <= h < hours_cfg.close_time.hour
            mgr_alloc = mgr_per_open_hour if is_open else 0
            hourly_budget = max(0, total_budget - mgr_alloc)

            # Sum scheduled hourly cost for this hour
            foh_min = boh_min = 0.0
            foh_cost = boh_cost = 0.0
            mgr_min = 0.0
            for s in shifts:
                if s["date"] != d:
                    continue
                mins = _shift_minutes_in_hour(
                    s["start"], s["end"], h, s["break_start"], s["break_end"]
                )
                if mins == 0:
                    continue
                if s["role"] == "boh_manager":
                    mgr_min += mins
                    continue
                wage = (config.AVG_FOH_WAGE if s["role"].startswith("foh")
                        else config.AVG_BOH_WAGE)
                cost = (mins / 60.0) * wage
                if s["role"].startswith("foh"):
                    foh_min += mins
                    foh_cost += cost
                else:
                    boh_min += mins
                    boh_cost += cost

            scheduled_hourly = foh_cost + boh_cost
            total_scheduled = scheduled_hourly + mgr_alloc

            result[(d, h)] = {
                "projected_sales": proj_sales,
                "total_budget": total_budget,
                "mgr_alloc": mgr_alloc,
                "hourly_budget": hourly_budget,
                "scheduled_hourly": scheduled_hourly,
                "total_scheduled": total_scheduled,
                "delta": scheduled_hourly - hourly_budget,
                "headcount_floor": (foh_min + boh_min + mgr_min) / 60.0,
                "headcount_foh": foh_min / 60.0,
                "headcount_boh_hourly": boh_min / 60.0,
                "headcount_boh_mgr": mgr_min / 60.0,
                "is_open": is_open,
            }

    meta = {
        "week_start": week_start,
        "mgr_weekly": mgr_weekly,
        "mgr_per_open_hour": mgr_per_open_hour,
        "weekly_open_hours": weekly_open_hours,
        "target_pct": config.LABOR_TARGET_PCT,
    }
    return result, meta


def _money(x: float) -> str:
    if abs(x) < 0.5: return "—"
    return f"${x:,.0f}"


def _cell_class(delta: float, budget: float) -> str:
    if budget < 1:
        return "c-noproj" if delta == 0 else "c-zero"
    ratio = delta / budget if budget else 0
    if ratio < -0.20: return "c-under"
    if ratio < 0.10:  return "c-onbudget"
    if ratio < 0.30:  return "c-warn"
    return "c-over"


def render_budget_html(hourly_data, meta, output_path: Path) -> None:
    week_start = meta["week_start"]
    target_pct = meta["target_pct"] * 100

    all_hours = sorted({h for (_, h) in hourly_data.keys()})
    days = [week_start + timedelta(days=i) for i in range(7)]

    # Week totals
    week_sales = sum(v["projected_sales"] for v in hourly_data.values())
    week_total_budget = sum(v["total_budget"] for v in hourly_data.values())
    week_scheduled_hourly = sum(v["scheduled_hourly"] for v in hourly_data.values())
    week_mgr = meta["mgr_weekly"]
    week_total_scheduled = week_scheduled_hourly + week_mgr
    week_pct = (week_total_scheduled / week_sales * 100) if week_sales else 0
    week_diff = week_pct - target_pct
    if abs(week_diff) <= 2:
        verdict_cls, verdict = "ok", f"On target ({week_diff:+.1f}pp)"
    elif week_diff > 0:
        verdict_cls, verdict = "over", f"Over by {week_diff:.1f}pp"
    else:
        verdict_cls, verdict = "under", f"Under by {-week_diff:.1f}pp"

    # Per-cell: compare TOTAL labor (hourly + mgr allocation) vs TOTAL budget.
    # This avoids penalizing hours where mgr-allocation alone > target.
    def cell_class_total(total_scheduled: float, total_budget: float) -> str:
        if total_budget < 1:
            return "c-noproj" if total_scheduled == 0 else "c-zero"
        delta = total_scheduled - total_budget
        ratio = delta / total_budget if total_budget else 0
        if ratio < -0.20: return "c-under"
        if ratio < 0.15:  return "c-onbudget"
        if ratio < 0.40:  return "c-warn"
        return "c-over"

    # Build day rows
    day_rows_html: List[str] = []
    col_totals: Dict[int, dict] = {h: {"budget": 0, "sched": 0} for h in all_hours}
    for d in days:
        cells: List[str] = []
        day_budget_total = day_sched_hourly = day_proj = day_mgr_alloc = 0.0
        for h in all_hours:
            v = hourly_data.get((d, h))
            if v is None:
                cells.append('<td class="c-closed" title="Closed"></td>')
                continue
            day_proj += v["projected_sales"]
            day_budget_total += v["total_budget"]
            day_sched_hourly += v["scheduled_hourly"]
            day_mgr_alloc += v["mgr_alloc"]
            col_totals[h]["budget"] += v["total_budget"]
            col_totals[h]["sched"] += v["scheduled_hourly"] + v["mgr_alloc"]

            total_scheduled = v["scheduled_hourly"] + v["mgr_alloc"]
            cls = cell_class_total(total_scheduled, v["total_budget"])
            hc = v["headcount_floor"]
            tooltip = (
                f"{d.strftime('%a')} {h:02d}:00 — "
                f"Proj sales ${v['projected_sales']:,.0f}, "
                f"Total budget ${v['total_budget']:,.0f}, "
                f"Total scheduled ${total_scheduled:,.0f} "
                f"({hc:.1f} people on floor)"
            )
            budget_str = _money(v["total_budget"])
            sched_str = _money(total_scheduled)
            hc_str = f"{hc:.1f}" if hc > 0 else "—"
            cells.append(
                f'<td class="{cls}" title="{tooltip}">'
                f'<div class="b">{budget_str}</div>'
                f'<div class="s">{sched_str}</div>'
                f'<div class="hc">{hc_str}</div>'
                f'</td>'
            )
        # Row total
        day_total_scheduled = day_sched_hourly + day_mgr_alloc
        day_pct = (day_total_scheduled / day_proj * 100) if day_proj else 0
        if abs(day_pct - target_pct) <= 2:
            day_pct_cls = "ok"
        elif day_pct > target_pct:
            day_pct_cls = "over"
        else:
            day_pct_cls = "under"

        day_rows_html.append(f"""
        <tr>
          <th class="dayhead">
            <div class="dnum">{d.day}</div>
            <div class="dname">{d.strftime("%a")}</div>
          </th>
          {''.join(cells)}
          <td class="rowtot">
            <div class="b">{_money(day_budget_total)}</div>
            <div class="s">{_money(day_total_scheduled)}</div>
            <div class="p {day_pct_cls}">{day_pct:.0f}%</div>
          </td>
        </tr>
        """)

    # Column header row
    col_header_html = '<th class="corner"></th>' + ''.join(
        f'<th class="hourhead">{h:02d}</th>' for h in all_hours
    ) + '<th class="corner">Day</th>'

    # Column totals row
    col_tot_html = '<th class="corner">Wk</th>' + ''.join(
        f'<td class="coltot"><div class="b">{_money(col_totals[h]["budget"])}</div>'
        f'<div class="s">{_money(col_totals[h]["sched"])}</div></td>'
        for h in all_hours
    ) + (f'<td class="grandtot"><div class="b">{_money(week_total_budget)}</div>'
         f'<div class="s">{_money(week_total_scheduled)}</div></td>')

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Knotted — Hourly Labor Budget — {week_start.isoformat()}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #faf8f5;
    --ink: #1a1814;
    --ink-soft: #6b6359;
    --rule: #e5dfd4;
    --under: #b8d4a0;
    --onbud: #f4ecd8;
    --warn: #f0c987;
    --over: #d97557;
    --zero: #f4f1eb;
    --noproj: #ffffff;
    --closed: #2a2825;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Inter', system-ui, sans-serif;
    background: var(--bg);
    color: var(--ink);
    padding: 32px 32px 64px;
    max-width: 1500px;
    margin: 0 auto;
    font-size: 12px;
  }}
  header.masthead {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    padding-bottom: 16px;
    margin-bottom: 24px;
    border-bottom: 2px solid var(--ink);
  }}
  .masthead h1 {{
    font-family: 'Fraunces', serif;
    font-weight: 700;
    font-size: 32px;
    letter-spacing: -0.02em;
  }}
  .masthead h1 em {{
    font-style: italic;
    font-weight: 400;
    color: var(--ink-soft);
  }}
  .masthead .week {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--ink-soft);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }}

  .summary {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0;
    margin-bottom: 16px;
    border: 1px solid var(--rule);
    background: white;
  }}
  .summary .cell {{
    padding: 14px 16px;
    border-right: 1px solid var(--rule);
  }}
  .summary .cell:last-child {{ border-right: none; }}
  .summary .k {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-soft);
    margin-bottom: 4px;
    display: block;
  }}
  .summary .v {{
    font-family: 'Fraunces', serif;
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }}
  .summary .v.mono {{
    font-family: 'JetBrains Mono', monospace;
    font-weight: 500;
  }}
  .summary .verdict {{
    margin-top: 3px;
    font-size: 11px;
    color: var(--ink-soft);
  }}
  .pct.ok    {{ color: #4a6b2c; }}
  .pct.under {{ color: #4a6b2c; }}
  .pct.over  {{ color: #9d3812; }}

  .explainer {{
    background: white;
    border: 1px solid var(--rule);
    padding: 14px 18px;
    margin-bottom: 16px;
    font-size: 12px;
    line-height: 1.5;
  }}
  .explainer strong {{ color: var(--ink); }}
  .explainer .label {{
    font-family: 'JetBrains Mono', monospace;
    background: var(--zero);
    padding: 1px 5px;
    border-radius: 2px;
    font-size: 11px;
  }}

  .key {{
    display: flex;
    gap: 16px;
    align-items: center;
    margin-bottom: 14px;
    font-size: 11px;
    color: var(--ink-soft);
    flex-wrap: wrap;
  }}
  .key .item {{ display: flex; align-items: center; gap: 6px; }}
  .key .sw {{ width: 16px; height: 12px; border: 1px solid rgba(0,0,0,0.1); }}
  .sw-under  {{ background: var(--under); }}
  .sw-onbud  {{ background: var(--onbud); }}
  .sw-warn   {{ background: var(--warn); }}
  .sw-over   {{ background: var(--over); }}
  .sw-closed {{ background: var(--closed); }}

  table.heat {{
    width: 100%;
    border-collapse: collapse;
    background: white;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
  }}
  .heat th, .heat td {{
    border: 1px solid var(--rule);
    padding: 4px 3px;
    text-align: center;
    vertical-align: middle;
    min-width: 46px;
  }}
  .heat thead th.hourhead {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    font-weight: 500;
    color: var(--ink-soft);
    padding: 6px 3px;
    background: var(--bg);
  }}
  .heat thead .corner {{ background: var(--bg); border: none; color: var(--ink-soft); font-size: 10px; }}

  .dayhead {{
    text-align: left !important;
    padding: 5px 10px !important;
    min-width: 60px;
    background: white;
    border-right: 2px solid var(--ink) !important;
  }}
  .dnum {{
    font-family: 'Fraunces', serif;
    font-size: 20px;
    font-weight: 700;
    line-height: 1;
  }}
  .dname {{
    font-family: 'Fraunces', serif;
    font-style: italic;
    font-size: 11px;
    color: var(--ink-soft);
  }}

  .heat td .b  {{ font-weight: 600; font-size: 11px; }}
  .heat td .s  {{ font-weight: 400; color: var(--ink-soft); font-size: 10px; }}
  .heat td .hc {{ font-weight: 400; font-size: 9px; opacity: 0.7; margin-top: 1px; }}

  .c-under   {{ background: var(--under); }}
  .c-onbudget{{ background: var(--onbud); }}
  .c-warn    {{ background: var(--warn); }}
  .c-over    {{ background: var(--over); color: white; }}
  .c-over .s, .c-over .hc {{ color: rgba(255,255,255,0.85); }}
  .c-zero    {{ background: var(--zero); }}
  .c-zero .b, .c-zero .s, .c-zero .hc {{ color: var(--ink-soft); opacity: 0.4; }}
  .c-noproj  {{ background: var(--noproj); }}
  .c-noproj .b, .c-noproj .s, .c-noproj .hc {{ opacity: 0.3; }}
  .c-closed  {{ background: var(--closed); }}

  .rowtot, .coltot, .grandtot {{
    background: var(--bg) !important;
    border-left: 2px solid var(--ink) !important;
  }}
  .rowtot .b, .coltot .b, .grandtot .b {{ font-weight: 600; font-size: 11px; }}
  .rowtot .s, .coltot .s, .grandtot .s {{ color: var(--ink-soft); font-size: 10px; }}
  .rowtot .p {{
    font-family: 'Fraunces', serif;
    font-weight: 600;
    font-size: 13px;
    margin-top: 2px;
  }}
  .p.ok    {{ color: #4a6b2c; }}
  .p.under {{ color: #4a6b2c; }}
  .p.over  {{ color: #9d3812; }}

  tfoot tr {{ border-top: 2px solid var(--ink); }}
  tfoot .coltot, tfoot .grandtot {{ border-top: 2px solid var(--ink) !important; }}

  .footnote {{
    margin-top: 18px;
    font-size: 11px;
    color: var(--ink-soft);
    line-height: 1.5;
  }}
  .footnote p {{ margin-bottom: 6px; }}

  @media print {{
    body {{ padding: 16px; background: white; font-size: 10px; }}
  }}
</style>
</head>
<body>
  <header class="masthead">
    <h1>Hourly Labor Budget <em>— week of {week_start.strftime('%b %d, %Y')}</em></h1>
    <div class="week">Target {target_pct:.0f}% · FOH ${config.AVG_FOH_WAGE:.2f} · BOH ${config.AVG_BOH_WAGE:.2f}</div>
  </header>

  <section class="summary">
    <div class="cell">
      <span class="k">Projected Weekly Sales</span>
      <span class="v">{_money(week_sales)}</span>
    </div>
    <div class="cell">
      <span class="k">Total Labor Budget (28%)</span>
      <span class="v">{_money(week_total_budget)}</span>
    </div>
    <div class="cell">
      <span class="k">Scheduled (hourly + mgrs)</span>
      <span class="v">{_money(week_total_scheduled)}</span>
    </div>
    <div class="cell">
      <span class="k">Labor % at Schedule</span>
      <span class="v mono pct {verdict_cls}">{week_pct:.1f}%</span>
      <span class="verdict">{verdict}</span>
    </div>
  </section>

  <div class="explainer">
    Each cell is one hour. Top <span class="label">$ budget</span> = projected sales × {target_pct:.0f}% target.
    Middle <span class="label">$ scheduled</span> = total labor cost for that hour (hourly staff + manager allocation).
    Bottom = headcount on floor. <strong>Day and Week totals are what really matter</strong> — individual cells fluctuate based on when sales happen.
  </div>

  <div class="key">
    <div class="item"><span class="sw sw-under"></span>Under budget (room)</div>
    <div class="item"><span class="sw sw-onbud"></span>On budget (±15%)</div>
    <div class="item"><span class="sw sw-warn"></span>Over by 15–40%</div>
    <div class="item"><span class="sw sw-over"></span>Over by 40%+</div>
    <div class="item"><span class="sw sw-closed"></span>Closed</div>
  </div>

  <table class="heat">
    <thead>
      <tr>{col_header_html}</tr>
    </thead>
    <tbody>
      {''.join(day_rows_html)}
    </tbody>
    <tfoot>
      <tr>{col_tot_html}</tr>
    </tfoot>
  </table>

  <div class="footnote">
    <p><strong>How to use this.</strong> The right column (Day totals) and the row at the bottom (week totals) tell you whether you're on budget overall. Individual hour cells will be lumpy — opening hours have labor but no sales yet, peak lunch hours have more sales than labor — that's normal. Aim for the day-total <strong>labor %</strong> in the right column to be near 28%.</p>
    <p><strong>Why some lunch hours look red.</strong> Around noon you'll see warm/red cells because the staffing curve doesn't perfectly track the sales curve — once you have 3 people on the floor they cost the same whether you do $400 or $500 that hour. Day totals smooth this out.</p>
    <p><strong>Manager cost.</strong> Combined manager salaries (${week_mgr:,.0f}/week, ${meta['mgr_per_open_hour']:.2f}/open-hour) are spread evenly across the {meta['weekly_open_hours']} open hours and included in "scheduled" everywhere.</p>
  </div>
</body>
</html>
"""
    output_path.write_text(html)


if __name__ == "__main__":
    import sys
    ws = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 5, 25)
    out_dir = Path(config.OUTPUT_DIR)
    schedule_csv = out_dir / f"schedule_{ws.isoformat()}.csv"
    hourly_data, meta = compute_hourly_budget(
        schedule_csv, ws, Path("/mnt/user-data/uploads")
    )
    output_path = out_dir / f"hourly_budget_{ws.isoformat()}.html"
    render_budget_html(hourly_data, meta, output_path)
    print(f"✓ Hourly budget: {output_path}")
