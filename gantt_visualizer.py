"""
Render the weekly schedule as a printable HTML Gantt chart.

Reads:
  - schedule_YYYY-MM-DD.csv  (the shifts)
  - summary_YYYY-MM-DD.md    (the daily/weekly stats — we re-derive instead)

Writes:
  - gantt_YYYY-MM-DD.html    (self-contained, printable)

Design notes: this is an operations tool. Restrained typography, monospaced
where numbers matter, color reserved for signal (role bucket, labor-% status).
Print-friendly (8.5"x11" landscape works).
"""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Dict
from dataclasses import dataclass

import config


@dataclass
class ShiftRow:
    date: date
    day_of_week: str
    role: str
    start_min: int   # minutes from midnight
    end_min: int
    break_start_min: int | None
    break_end_min: int | None
    paid_hours: float

    @property
    def role_bucket(self) -> str:
        if self.role == "boh_manager": return "mgr"
        if self.role.startswith("foh_opener"): return "opener"
        if self.role.startswith("foh_mid"): return "mid"
        if "dish_closer" in self.role: return "dish"
        if "station_closer" in self.role: return "station"
        if self.role.startswith("boh"): return "boh"
        return "other"

    @property
    def display_role(self) -> str:
        return {
            "opener": "Opener",
            "mid": "Mid",
            "dish": "Closer (dish)",
            "station": "Closer (station)",
            "boh": "Cook",
            "mgr": "Cook (Manager)",
        }.get(self.role_bucket, self.role)


def _hm_to_min(hm: str) -> int:
    if not hm:
        return -1
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def _load_shifts(csv_path: Path) -> List[ShiftRow]:
    rows: List[ShiftRow] = []
    with csv_path.open() as f:
        for r in csv.DictReader(f):
            bs = _hm_to_min(r["break_start"])
            be = _hm_to_min(r["break_end"])
            rows.append(ShiftRow(
                date=date.fromisoformat(r["date"]),
                day_of_week=r["day_of_week"],
                role=r["role"],
                start_min=_hm_to_min(r["start"]),
                end_min=_hm_to_min(r["end"]),
                break_start_min=bs if bs >= 0 else None,
                break_end_min=be if be >= 0 else None,
                paid_hours=float(r["paid_hours"]),
            ))
    return rows


def _aggregate_daily(shifts: List[ShiftRow], foh_wage: float, boh_wage: float) -> Dict[date, dict]:
    by_day: Dict[date, dict] = {}
    for s in shifts:
        d = by_day.setdefault(s.date, {
            "foh_hours": 0.0, "boh_hours": 0.0,
            "foh_cost": 0.0, "boh_cost": 0.0,
            "shifts": [],
        })
        d["shifts"].append(s)
        # Manager shifts are salary-funded, not hourly — exclude from cost calc
        if s.role in ("boh_manager", "foh_manager"):
            continue
        if s.role.startswith("foh"):
            d["foh_hours"] += s.paid_hours
            d["foh_cost"] += s.paid_hours * foh_wage
        else:
            d["boh_hours"] += s.paid_hours
            d["boh_cost"] += s.paid_hours * boh_wage
    return by_day


# Display window: 6:00am to 11:30pm (covers all shifts)
DAY_START_MIN = 6 * 60
DAY_END_MIN = 23 * 60 + 30
DAY_SPAN_MIN = DAY_END_MIN - DAY_START_MIN


def _bar_left_pct(start_min: int) -> float:
    return max(0, (start_min - DAY_START_MIN) / DAY_SPAN_MIN * 100)


def _bar_width_pct(start_min: int, end_min: int) -> float:
    w = (end_min - start_min) / DAY_SPAN_MIN * 100
    return max(0.5, w)


def _hour_ticks() -> List[tuple[int, float]]:
    """Returns [(hour_label, left_pct), ...] for hour gridlines."""
    ticks = []
    for h in range(6, 24):
        m = h * 60
        if DAY_START_MIN <= m <= DAY_END_MIN:
            ticks.append((h, (m - DAY_START_MIN) / DAY_SPAN_MIN * 100))
    return ticks


def _daily_sales_projection(meta_path: Path) -> Dict[str, float]:
    """Crude parse: pull projected sales from the summary markdown."""
    proj: Dict[str, float] = {}
    if not meta_path.exists():
        return proj
    in_table = False
    for line in meta_path.read_text().splitlines():
        if line.startswith("| Day |") and "Date" in line:
            in_table = True
            continue
        if in_table:
            if not line.startswith("|") or line.startswith("|---"):
                continue
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) < 9:
                continue
            day_name, date_str, sales = cells[0], cells[1], cells[2]
            if day_name in ("Day", ""):
                continue
            try:
                v = float(sales.replace("$", "").replace(",", ""))
                proj[date_str] = v
            except ValueError:
                pass
            # Stop at end of table
            if cells[0] == "Sun":
                break
    return proj


def render_gantt(week_start: date) -> Path:
    out_dir = Path(config.OUTPUT_DIR)
    tag = week_start.isoformat()
    schedule_csv = out_dir / f"schedule_{tag}.csv"
    summary_md = out_dir / f"summary_{tag}.md"
    if not schedule_csv.exists():
        raise FileNotFoundError(f"No schedule found at {schedule_csv}")

    shifts = _load_shifts(schedule_csv)
    by_day = _aggregate_daily(shifts, config.AVG_FOH_WAGE, config.AVG_BOH_WAGE)
    sales_proj = _daily_sales_projection(summary_md)

    # Build per-day rows (Mon-Sun)
    days = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        info = by_day.get(d, {"foh_hours": 0, "boh_hours": 0, "foh_cost": 0, "boh_cost": 0, "shifts": []})
        sales = sales_proj.get(d.isoformat(), 0.0)
        # Manager allocation per day = (FOH mgr + BOH mgr salary) / 52 weeks / 7 days
        mgr_daily = (config.FOH_MANAGER_ANNUAL_SALARY +
                     config.BOH_MANAGER_ANNUAL_SALARY) / 52.0 / 7.0
        hourly_labor = info["foh_cost"] + info["boh_cost"]
        labor = hourly_labor + mgr_daily
        labor_pct = (labor / sales * 100) if sales else 0.0
        days.append({
            "date": d,
            "name": d.strftime("%A"),
            "shortname": d.strftime("%a"),
            "sales": sales,
            "labor": labor,
            "hourly_labor": hourly_labor,
            "mgr_labor": mgr_daily,
            "labor_pct": labor_pct,
            "shifts": sorted(info["shifts"], key=lambda x: (x.start_min, x.role)),
        })

    target_pct = config.LABOR_TARGET_PCT * 100
    week_sales = sum(d["sales"] for d in days)
    week_labor = sum(d["labor"] for d in days)
    week_pct = (week_labor / week_sales * 100) if week_sales else 0.0
    # Hours = hourly staff only (managers are salaried)
    week_hours = sum(s.paid_hours for s in shifts
                     if s.role not in ("boh_manager", "foh_manager"))

    out = out_dir / f"gantt_{tag}.html"
    out.write_text(_render_html(days, target_pct, week_start,
                                week_sales, week_labor, week_pct, week_hours))
    return out


def _pct_class(pct: float, target: float, tol_pp: float = 2.0) -> str:
    """CSS class for a labor percentage cell."""
    if pct == 0: return "pct-zero"
    if pct <= target + tol_pp: return "pct-ok"
    if pct <= target + 5: return "pct-warn"
    return "pct-over"


def _render_html(days, target_pct, week_start, week_sales, week_labor,
                 week_pct, week_hours) -> str:
    # Per-bar rendering
    def shift_bar_html(s: ShiftRow) -> str:
        left = _bar_left_pct(s.start_min)
        width = _bar_width_pct(s.start_min, s.end_min)
        bar_class = f"bar bar-{s.role_bucket}"
        # Break overlay (white slash)
        break_html = ""
        if s.break_start_min is not None and s.break_end_min is not None:
            bl = (s.break_start_min - s.start_min) / (s.end_min - s.start_min) * 100
            bw = (s.break_end_min - s.break_start_min) / (s.end_min - s.start_min) * 100
            break_html = f'<div class="brk" style="left:{bl:.1f}%;width:{bw:.1f}%"></div>'
        # Label (only if bar is wide enough)
        label = s.display_role if width > 8 else s.display_role[:3]
        return (
            f'<div class="{bar_class}" style="left:{left:.1f}%;width:{width:.1f}%" '
            f'title="{s.display_role}: {s.start_min//60:02d}:{s.start_min%60:02d}–'
            f'{s.end_min//60:02d}:{s.end_min%60:02d} ({s.paid_hours:.1f}hr)">'
            f'{break_html}<span class="lbl">{label}</span></div>'
        )

    # Build day blocks
    day_blocks = []
    for d in days:
        # Compute headcount-by-hour for the FOH overlay strip
        hc = [0] * 24
        for s in d["shifts"]:
            if not s.role.startswith("foh"):
                continue
            sh = s.start_min // 60
            eh = (s.end_min + 29) // 60  # round up
            for h in range(sh, min(eh, 24)):
                hc[h] += 1

        bars_html = "\n".join(shift_bar_html(s) for s in d["shifts"])
        pct_class = _pct_class(d["labor_pct"], target_pct)
        labor_pct_display = f"{d['labor_pct']:.1f}%" if d["labor_pct"] else "—"

        day_blocks.append(f"""
        <section class="day">
          <header class="day-head">
            <div class="day-name">
              <span class="day-num">{d["date"].day}</span>
              <span class="day-word">{d["name"]}</span>
            </div>
            <div class="day-stats">
              <div class="stat"><span class="k">Proj. Sales</span><span class="v">${d["sales"]:,.0f}</span></div>
              <div class="stat"><span class="k">Labor $</span><span class="v">${d["labor"]:,.0f}</span></div>
              <div class="stat pct {pct_class}"><span class="k">Labor %</span><span class="v">{labor_pct_display}</span></div>
            </div>
          </header>
          <div class="track">
            <div class="grid">
              {''.join(f'<div class="tick" style="left:{lp:.1f}%"><span>{h:02d}</span></div>'
                       for h, lp in _hour_ticks())}
            </div>
            {bars_html}
          </div>
        </section>
        """)

    week_pct_class = _pct_class(week_pct, target_pct)
    diff_text = ""
    diff_pp = week_pct - target_pct
    if abs(diff_pp) <= 2:
        diff_text = f"On target ({diff_pp:+.1f}pp)"
    elif diff_pp > 0:
        diff_text = f"Over by {diff_pp:.1f}pp"
    else:
        diff_text = f"Under by {-diff_pp:.1f}pp"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Knotted — Week of {week_start.strftime('%b %d, %Y')}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,700&family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #faf8f5;
    --ink: #1a1814;
    --ink-soft: #6b6359;
    --rule: #e5dfd4;
    --rule-strong: #1a1814;
    --accent: #c4541f;
    --opener: #5d7a3d;
    --mid: #7b9c5c;
    --dish: #c4541f;
    --station: #9d3812;
    --boh: #2f4e6b;
    --mgr: #5a3a78;
    --ok: #5d7a3d;
    --warn: #b88718;
    --over: #9d3812;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Inter', system-ui, sans-serif;
    background: var(--bg);
    color: var(--ink);
    padding: 32px 40px 64px;
    max-width: 1400px;
    margin: 0 auto;
    font-size: 13px;
    line-height: 1.5;
  }}
  .masthead {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    padding-bottom: 20px;
    margin-bottom: 32px;
    border-bottom: 2px solid var(--rule-strong);
  }}
  .masthead h1 {{
    font-family: 'Fraunces', serif;
    font-weight: 700;
    font-size: 36px;
    letter-spacing: -0.02em;
    line-height: 1;
  }}
  .masthead h1 em {{
    font-style: italic;
    font-weight: 400;
    color: var(--ink-soft);
  }}
  .masthead .week {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px;
    color: var(--ink-soft);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }}
  .week-summary {{
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 0;
    margin-bottom: 32px;
    border: 1px solid var(--rule);
    background: white;
  }}
  .week-summary .cell {{
    padding: 16px 20px;
    border-right: 1px solid var(--rule);
  }}
  .week-summary .cell:last-child {{ border-right: none; }}
  .week-summary .k {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--ink-soft);
    display: block;
    margin-bottom: 6px;
  }}
  .week-summary .v {{
    font-family: 'Fraunces', serif;
    font-size: 24px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }}
  .week-summary .v.mono {{
    font-family: 'JetBrains Mono', monospace;
    font-weight: 500;
  }}
  .week-summary .pct-ok .v {{ color: var(--ok); }}
  .week-summary .pct-warn .v {{ color: var(--warn); }}
  .week-summary .pct-over .v {{ color: var(--over); }}
  .verdict {{
    font-size: 11px;
    color: var(--ink-soft);
    margin-top: 4px;
  }}

  /* Legend */
  .legend {{
    display: flex;
    gap: 20px;
    align-items: center;
    margin-bottom: 16px;
    font-size: 11px;
    color: var(--ink-soft);
  }}
  .legend .item {{
    display: flex;
    align-items: center;
    gap: 6px;
  }}
  .legend .sw {{
    width: 14px;
    height: 10px;
    border-radius: 1px;
  }}
  .sw-opener {{ background: var(--opener); }}
  .sw-mid    {{ background: var(--mid); }}
  .sw-dish   {{ background: var(--dish); }}
  .sw-station{{ background: var(--station); }}
  .sw-boh    {{ background: var(--boh); }}
  .sw-mgr    {{ background: var(--mgr); }}

  /* Day rows */
  .day {{
    margin-bottom: 14px;
    background: white;
    border: 1px solid var(--rule);
    page-break-inside: avoid;
  }}
  .day-head {{
    display: flex;
    align-items: stretch;
    border-bottom: 1px solid var(--rule);
  }}
  .day-name {{
    padding: 14px 20px;
    border-right: 1px solid var(--rule);
    min-width: 200px;
    display: flex;
    align-items: baseline;
    gap: 12px;
  }}
  .day-num {{
    font-family: 'Fraunces', serif;
    font-size: 32px;
    font-weight: 700;
    line-height: 1;
    letter-spacing: -0.02em;
  }}
  .day-word {{
    font-family: 'Fraunces', serif;
    font-style: italic;
    font-size: 17px;
    color: var(--ink-soft);
  }}
  .day-stats {{
    display: flex;
    flex: 1;
  }}
  .day-stats .stat {{
    padding: 12px 20px;
    border-right: 1px solid var(--rule);
    flex: 1;
  }}
  .day-stats .stat:last-child {{ border-right: none; }}
  .day-stats .k {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-soft);
    display: block;
    margin-bottom: 3px;
  }}
  .day-stats .v {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 16px;
    font-weight: 500;
  }}
  .day-stats .pct.pct-ok .v {{ color: var(--ok); }}
  .day-stats .pct.pct-warn .v {{ color: var(--warn); }}
  .day-stats .pct.pct-over .v {{ color: var(--over); }}

  /* Track */
  .track {{
    position: relative;
    min-height: 200px;
    padding: 12px 20px;
  }}
  .grid {{
    position: absolute;
    top: 0; left: 20px; right: 20px; bottom: 0;
    pointer-events: none;
  }}
  .tick {{
    position: absolute;
    top: 0; bottom: 0;
    border-left: 1px solid var(--rule);
  }}
  .tick span {{
    position: absolute;
    top: -8px;
    left: -8px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 9px;
    color: var(--ink-soft);
    background: white;
    padding: 0 3px;
  }}

  /* Shift bars */
  .bar {{
    position: relative;
    height: 22px;
    margin: 4px 0;
    border-radius: 2px;
    color: white;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    display: flex;
    align-items: center;
    padding: 0 8px;
    box-shadow: 0 1px 0 rgba(0,0,0,0.08);
    overflow: hidden;
  }}
  .bar .lbl {{
    white-space: nowrap;
    text-shadow: 0 1px 0 rgba(0,0,0,0.15);
    letter-spacing: 0.02em;
  }}
  .bar .brk {{
    position: absolute;
    top: 0; bottom: 0;
    background: repeating-linear-gradient(
      45deg,
      rgba(255,255,255,0.45),
      rgba(255,255,255,0.45) 3px,
      transparent 3px,
      transparent 6px
    );
  }}
  .bar-opener  {{ background: var(--opener); }}
  .bar-mid     {{ background: var(--mid); }}
  .bar-dish    {{ background: var(--dish); }}
  .bar-station {{ background: var(--station); }}
  .bar-boh     {{ background: var(--boh); }}
  .bar-mgr     {{ background: var(--mgr); }}

  /* Footer */
  footer {{
    margin-top: 32px;
    padding-top: 16px;
    border-top: 1px solid var(--rule);
    font-size: 11px;
    color: var(--ink-soft);
    display: flex;
    justify-content: space-between;
  }}

  @media print {{
    body {{ padding: 16px; background: white; font-size: 11px; }}
    .day {{ box-shadow: none; }}
    .week-summary {{ box-shadow: none; }}
  }}
</style>
</head>
<body>
  <header class="masthead">
    <h1>Knotted <em>— weekly schedule</em></h1>
    <div class="week">Week of {week_start.strftime('%b %d, %Y').upper()}</div>
  </header>

  <section class="week-summary">
    <div class="cell">
      <span class="k">Projected Sales</span>
      <span class="v">${week_sales:,.0f}</span>
    </div>
    <div class="cell">
      <span class="k">Labor Hours</span>
      <span class="v mono">{week_hours:.0f}</span>
    </div>
    <div class="cell">
      <span class="k">Labor Cost</span>
      <span class="v">${week_labor:,.0f}</span>
    </div>
    <div class="cell {week_pct_class}">
      <span class="k">Labor %</span>
      <span class="v mono">{week_pct:.1f}%</span>
      <span class="verdict">target {target_pct:.0f}% &middot; {diff_text}</span>
    </div>
    <div class="cell">
      <span class="k">Avg Daily Sales</span>
      <span class="v">${week_sales/7:,.0f}</span>
    </div>
  </section>

  <div class="legend">
    <div class="item"><span class="sw sw-opener"></span>Opener</div>
    <div class="item"><span class="sw sw-mid"></span>Mid</div>
    <div class="item"><span class="sw sw-dish"></span>Closer (dish)</div>
    <div class="item"><span class="sw sw-station"></span>Closer (station)</div>
    <div class="item"><span class="sw sw-boh"></span>Cook</div>
    <div class="item"><span class="sw sw-mgr"></span>Cook (Manager)</div>
    <div class="item" style="margin-left: auto;">
      <span style="display:inline-block;width:14px;height:10px;background:repeating-linear-gradient(45deg,rgba(0,0,0,0.4),rgba(0,0,0,0.4) 2px,transparent 2px,transparent 4px);"></span>
      Unpaid break
    </div>
  </div>

  {''.join(day_blocks)}

  <footer>
    <span>FOH ${config.AVG_FOH_WAGE:.2f}/hr &middot; BOH ${config.AVG_BOH_WAGE:.2f}/hr &middot; Target {target_pct:.0f}% labor</span>
    <span>Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</span>
  </footer>
</body>
</html>
"""


if __name__ == "__main__":
    import sys
    ws = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 5, 25)
    path = render_gantt(ws)
    print(f"✓ Gantt: {path}")
