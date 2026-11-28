"""
Unified daily schedule view for Cafe Knotted.

For each day, shows top-to-bottom:
  1. Shift bars (FOH in role colors + BOH in slate, with breaks)
  2. Orders/hour heatmap (sequential color: light = empty, dark = busy)
  3. Sales/hour heatmap (same sequential scale)
  4. FOH need heatmap (categorical headcount)
  5. FOH scheduled heatmap (same categorical scale as #4 for direct comparison)

BOH is shown in shift bars and cost line only; it's fixed (2 cooks 7-3:30
every day; manager covers cook #1 Wed-Sun).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from collections import defaultdict

import config
from shift_optimizer import FOHShift, PROPOSED_SCHEDULE, schedule_cost, required_for_hour
from break_scheduler import assign_breaks
from csv_analyzer import build_projection_from_csvs, TastFiles


# Display window: 6am to 11pm inclusive
DISPLAY_HOURS = list(range(6, 24))


# Cross-platform date formatting (Windows strftime has no %-d).
def _bd(d) -> str:   # e.g. "Nov 2"
    return f"{d:%b} {d.day}"


def _bdy(d) -> str:  # e.g. "Nov 2, 2026"
    return f"{d:%b} {d.day}, {d.year}"


# Kintow/Colette restyle — look-and-feel override layer, appended last in <style>
# so it wins the cascade without removing any base rules.
RESTYLE_CSS = r"""
/* ===================== KINTOW / COLETTE RESTYLE ===================== */
:root{
  --bg:#FAF3F0; --card:#FFFFFF; --surface:#F4EAE6; --tint:#F3EDE2;
  --rule:#EDDFD9; --rule-strong:#E6D3CC;
  --ink:#16221B; --ink-soft:#57524C; --ink-hint:#69635D;
  --pine:#2F4B3C; --terracotta:#BF3A2B;
  --green-900:#0F2417; --green-800:#15311C; --green-700:#1D4026; --green-grad:#20472A;
  --on-dark:#F9F0EA;
  --good:#2D6238; --warn:#583904; --bad:#AF3528;
  --serif:'Newsreader',Georgia,serif;
  --label:'Space Grotesk','Inter',sans-serif;
}
body{ font-family:'Inter',system-ui,sans-serif; -webkit-font-smoothing:antialiased;
      text-rendering:optimizeLegibility; }

/* figures: Inter tabular numerals (replaces the "digital" monospace) */
.bar, .bar .brk .brk-label, .heat-cell, .timeline-axis, .day-head .stats,
.summary, .summary .v, .summary .v-pct, .summary .v-sub, .summary .meta,
.comparison, .comparison .cmp-val, .comparison .cmp-delta,
table.daily-summary, table.labor-table, .item-totals, .blended-card,
.proj-sales-amount, .proj-sales-vs-base, .goal-amount, .goal-detail, .goal-annual,
.goal-unit, .masthead .sub, .tab-btn .tab-sub, .legend, .day-chip, .proj-tag{
  font-variant-numeric: tabular-nums; font-feature-settings:"tnum" 1;
}

/* editorial serif headlines, light weight */
.masthead h1{ font-family:var(--serif); font-weight:300; letter-spacing:-0.035em; }
.masthead h1 em{ font-weight:300; font-style:italic; }
.summary .v, .goal-amount, .proj-sales-amount, .blended-card .bl-v,
.day-head .dnum, .tab-btn .tab-num{
  font-family:var(--serif); font-weight:300; letter-spacing:-0.02em;
}
/* drop the redundant abbreviation: show only the full day name */
.day-head .dnum{ display:none; }
.day-head .dlabel{ font-family:var(--serif); font-weight:300; font-style:normal;
  font-size:22px; color:var(--ink); letter-spacing:-0.02em; }

/* tracked uppercase section labels -> Space Grotesk */
.masthead .sub, .summary .k, .proj-block-title, .goal-label,
.proj-accuracy .acc-title, .proj-accuracy .acc-subtitle,
.item-totals .lbl, .blended-card .bl-k, .comparison .cmp-label{
  font-family:var(--label); font-weight:600; text-transform:uppercase;
  letter-spacing:0.16em;
}

header.masthead{ border-bottom:1px solid var(--rule-strong); }

/* SIGNATURE: dark green summary card */
.summary{
  background:linear-gradient(160deg,var(--green-grad) 0%,var(--green-900) 100%);
  border:1px solid var(--green-900); border-radius:10px; overflow:hidden;
}
.summary .col{
  border-right:1px solid rgba(249,240,234,0.12);
  background:rgba(249,240,234,0.04);
  padding:18px 20px;
}
.summary .col:last-child{ border-right:none; }
.summary .k{ color:rgba(249,240,234,0.68); }
.summary .v{ color:var(--on-dark); }
.summary .v.mono{ color:var(--on-dark); }
.summary .v-pct, .summary .v-sub, .summary .meta{ color:rgba(249,240,234,0.60); }
.summary .pct.ok .val, .summary .pct.under, .summary .pct.ok{ color:#9FC98A; }
.summary .pct.over{ color:#F0A594; }

/* cards & panels: rounded hairline, no shadow, breathing room */
.day, .proj-block, .comparison, .blended-card,
section.proj-accuracy, section.labor-outlook{
  border:1px solid var(--rule); border-radius:10px; box-shadow:none;
  background:var(--card);
}
section.proj-accuracy, section.labor-outlook{ background:#FCF7F4; }
.day{ margin-bottom:22px; }
.day-head{ border-bottom:1px solid var(--rule); padding:14px 20px; }
.day-body{ padding:18px 20px 20px; }
.proj-block{ padding:20px 22px; }

/* segmented tab track (Kintow) */
.tabs{
  display:inline-flex; gap:4px; padding:4px;
  background:var(--tint); border:1px solid var(--rule);
  border-radius:999px; margin-bottom:26px;
}
.tab-btn{
  border:none; border-radius:999px; margin:0; padding:9px 20px 9px 16px;
  background:transparent; align-items:center;
}
.tab-btn:hover{ background:rgba(22,34,27,0.04); }
.tab-btn.active{ background:var(--card); border:none;
  box-shadow:0 1px 2px rgba(22,34,27,0.06); }
.tab-btn .tab-num{ font-size:14px; opacity:0.45; }
.tab-btn.active .tab-num{ opacity:1; }
.tab-btn .tab-title{ color:var(--ink-soft); }

/* Kintow warmth: terracotta as the house accent */
.masthead h1 em{ color:var(--terracotta); }
.proj-block-title, .proj-accuracy .acc-title, .proj-accuracy .acc-subtitle,
.item-totals .lbl, .goal-label, .blended-card .bl-k{ color:var(--terracotta); }
.tab-btn.active .tab-num{ color:var(--terracotta); }
.tab-btn.active .tab-title{ color:var(--terracotta); }
section.proj-accuracy{ border-left:3px solid var(--terracotta); }
section.labor-outlook{ border-left:3px solid var(--terracotta); }
.proj-block{ border-top:3px solid var(--terracotta); }
.day-chip{ background:rgba(191,58,43,0.10); color:#8A2419; }
.heat-cell.pinch{ box-shadow:inset 0 0 0 2px var(--terracotta); }
.proj-issue-list li::before{ background:var(--terracotta); }
.proj-issue-list li.sev-warning::before{ background:var(--warn); }
.goal-banner.trim-over{ background:#F6E3DF; border-left-color:var(--terracotta); color:#6E201A; }
.goal-banner.trim-under{ background:#DCE6DB; border-left-color:var(--good); color:#1E3A24; }

:focus-visible{ outline:2px solid var(--terracotta); outline-offset:2px; }
@media (prefers-reduced-motion:reduce){ *{ transition:none!important; animation:none!important; } }

/* responsive down to 390px */
@media (max-width:900px){
  .summary{ grid-template-columns:1fr 1fr; }
  .projection-summary, .blended-outlook{ grid-template-columns:1fr; }
}
@media (max-width:560px){
  body{ padding:20px 16px 48px; }
  .summary{ grid-template-columns:1fr; }
  .summary .col{ border-right:none; border-bottom:1px solid rgba(249,240,234,0.12); }
  .summary .col:last-child{ border-bottom:none; }
  .tabs{ display:flex; width:100%; }
  .tab-btn{ flex:1; justify-content:center; }
}
/* item scorecard: muted "(proj)" parenthetical next to the actual */
.bs-accuracy .base{ color:var(--ink-soft); font-weight:400; font-size:0.9em; }
/* =================== END KINTOW / COLETTE RESTYLE =================== */
"""


# ---------------------------------------------------------------------------
# BOH (cook) schedule — fixed pattern
# ---------------------------------------------------------------------------

@dataclass
class BOHShift:
    label: str
    start_min: int
    end_min: int
    wage: float
    is_manager: bool
    break_start_min: int
    break_end_min: int
    unfilled: bool = False   # planned but not yet staffed (rendered yellow)

    @property
    def total_hours(self) -> float:
        return (self.end_min - self.start_min) / 60.0

    @property
    def paid_hours(self) -> float:
        # CA meal break (30-min unpaid) only on shifts LONGER than 6 hours.
        # 6-hour-or-shorter shifts take no break -> full hours paid.
        if self.is_manager or self.total_hours <= 6.0:
            return self.total_hours
        return self.total_hours - 0.5

    @property
    def cost(self) -> float:
        return 0.0 if self.is_manager else self.paid_hours * self.wage

    @property
    def start_str(self) -> str:
        h, m = divmod(self.start_min, 60); return f"{h:02d}:{m:02d}"

    @property
    def end_str(self) -> str:
        # % 1440 so a past-midnight end (e.g. 25:00) reads as 01:00, not 25:00.
        h, m = divmod(self.end_min % 1440, 60); return f"{h:02d}:{m:02d}"


def boh_shifts_for_day(dow: int, pm_prep_override: Optional[int] = None) -> List[BOHShift]:
    """
    BOH production-cook roster (operator, 2026-11), all at the blended BOH rate:
      Mon-Thu:  AM Cook 8:00a-4:30p
      Fri-Sun:  AM Cook 7:00a-1:00p (6h) + Daytime Cook 11:30a-5:30p (6h) — leaner
                weekend day shifts
      Every day: PM Prep 4:30p-1:00a
    Early donut production (3:00a-11:30a) is handled in donut_prep_shifts().

    pm_prep_override is accepted for call-site compatibility but no longer used —
    PM prep is a single 4:30p-1a shift every day.
    """
    w = config.AVG_BOH_WAGE
    shifts: List[BOHShift] = []
    if dow in (4, 5, 6):  # Fri / Sat / Sun — leaner 6h day shifts (no meal break)
        shifts.append(BOHShift("AM Cook", 7 * 60, 13 * 60, w, False,            # 7:00a-1:00p
                               break_start_min=0, break_end_min=0))
        shifts.append(BOHShift("Daytime Cook", 11 * 60 + 30, 17 * 60 + 30, w, False,  # 11:30a-5:30p
                               break_start_min=0, break_end_min=0))
    else:  # Mon-Thu — AM Cook 8:00a-4:30p (8.5h -> 0.5 break)
        shifts.append(BOHShift("AM Cook", 8 * 60, 16 * 60 + 30, w, False,
                               break_start_min=12 * 60, break_end_min=12 * 60 + 30))
    # PM Prep 4:30p-1:00a next day (end encoded as 25:00 so cost/paid_hours are
    # correct; 8.5h -> 0.5 break -> 8.0 paid). Drawn bar is clamped to the midnight
    # chart edge; the label reads 1:00a via the overnight-aware end_str.
    shifts.append(BOHShift("PM Prep", 16 * 60 + 30, 25 * 60, w, False,
                           break_start_min=20 * 60 + 30, break_end_min=21 * 60))
    return shifts


# Current-week-only BOH additions. The former temp prep cook (3pm-9pm) is now
# superseded by the baseline PM Prep (4:00pm-10:30pm, in boh_shifts_for_day),
# so this is empty to avoid double-counting PM prep on the current week.
PREP_COOK_WAGE = config.AVG_BOH_WAGE

def current_week_extra_boh(dow: int) -> List[BOHShift]:
    return []


# Overnight donut prep (PRE-Donut-Friend only). 10:00pm-6:30am, $20/hr.
# Two every day, three on Thu/Fri/Sat. 8.5hr span -> 8.0 paid hrs @ $20 = $160
# each. These are NOT drawn on the bar chart (they run outside the display
# window) — they're listed and costed only. end_min encodes 6:30am next day as
# minutes past the shift's own midnight (22:00 + 8.5h) so cost/paid_hours are
# correct; display uses a fixed overnight label.
DONUT_PREP_WAGE = config.AVG_BOH_WAGE

def _overnight_hm(minutes: int) -> str:
    """Format minutes-past-midnight (may exceed 1440 for overnight) as e.g. 10:00p."""
    m = minutes % 1440
    h, mm = divmod(m, 60)
    ampm = 'a' if h < 12 else 'p'
    hh = h % 12 or 12
    return f"{hh}:{mm:02d}{ampm}"

def donut_prep_shifts(dow: int) -> List[BOHShift]:
    # Early donut production, 3:00a-11:30a every day (operator, 2026-11). They clock
    # in at 3a; the hourly chart still starts at 6a, so this is listed off-chart with
    # a 3a-start note, costed for the full 8.5h span (>6h -> 0.5 break -> 8.0 paid).
    return [
        BOHShift("Donut Prep", 3 * 60, 11 * 60 + 30,  # 3:00a-11:30a
                 DONUT_PREP_WAGE, False, break_start_min=0, break_end_min=0),
    ]


# ---------------------------------------------------------------------------
# Hourly demand data (orders + sales)
# ---------------------------------------------------------------------------

def build_hourly_metrics(uploads_dir: Path) -> Tuple[
    Dict[Tuple[int, int], float],   # orders by (dow, hour)
    Dict[Tuple[int, int], float],   # sales by (dow, hour)
]:
    """
    Combines Toast's Time-of-day report (hourly totals across all days) with
    the day-of-week report (DOW totals) to project orders & sales by (dow, hour).
    """
    # Hourly totals (aggregated)
    hour_orders: Dict[int, int] = {}
    hour_sales: Dict[int, float] = {}
    with (uploads_dir / "Time_of_day__totals_.csv").open() as f:
        for r in csv.DictReader(f):
            try:
                h = int(r["Hour of day"])
                hour_orders[h] = int(r["Total orders"] or 0)
                hour_sales[h] = float(r["Net sales"] or 0)
            except (ValueError, TypeError):
                continue
    tot_o = sum(hour_orders.values()) or 1
    tot_s = sum(hour_sales.values()) or 1
    order_share = {h: v / tot_o for h, v in hour_orders.items()}
    sales_share = {h: v / tot_s for h, v in hour_sales.items()}

    # Daily totals -> per-DOW baseline via shared weighted helper (most recent
    # config.BASELINE_WEEKS weeks, weighted toward the most recent). Keeps the
    # intra-day SHAPE from the full Time-of-day sample; only the day LEVEL is
    # recency-weighted.
    from csv_analyzer import weighted_dow_baseline
    avg_s_by_dow, avg_o_by_dow = weighted_dow_baseline(uploads_dir)

    orders_proj: Dict[Tuple[int, int], float] = {}
    sales_proj: Dict[Tuple[int, int], float] = {}
    for dow in range(7):
        for h in DISPLAY_HOURS:
            orders_proj[(dow, h)] = avg_o_by_dow.get(dow, 0) * order_share.get(h, 0)
            sales_proj[(dow, h)] = avg_s_by_dow.get(dow, 0) * sales_share.get(h, 0)
    return orders_proj, sales_proj


# ---------------------------------------------------------------------------
# Coverage math (FOH only for need-vs-scheduled comparison)
# ---------------------------------------------------------------------------

def foh_scheduled_for_hour(foh_shifts: List[FOHShift], hour: int) -> Tuple[float, int]:
    """
    Returns (avg_headcount, min_headcount) during the hour.

    avg = mean across the hour (for cost calculations)
    min = lowest count at any 15-min mark in the hour (for gap detection)
    """
    hour_start = hour * 60
    hour_end = hour_start + 60
    total_min = 0.0

    # For min: check headcount at each 15-min mark
    min_count = 999
    for offset_min in (0, 15, 30, 45):
        t = hour_start + offset_min
        count_at_t = 0
        for s in foh_shifts:
            if s.start_min <= t < s.end_min:
                # Are they on break at this moment?
                if s.has_break and s.break_start_min is not None:
                    if s.break_start_min <= t < s.break_end_min:
                        continue
                count_at_t += 1
        if count_at_t < min_count:
            min_count = count_at_t

    # Avg via minute integration (existing logic)
    for s in foh_shifts:
        overlap = max(0, min(s.end_min, hour_end) - max(s.start_min, hour_start))
        if s.has_break and s.break_start_min is not None:
            b = max(0, min(s.break_end_min, hour_end) - max(s.break_start_min, hour_start))
            overlap -= b
        total_min += overlap

    avg = total_min / 60.0
    return avg, min_count if min_count < 999 else 0


def foh_need_for_hour(dow: int, hour: int, orders_proj: Dict[Tuple[int, int], float],
                       sales_proj: Optional[Dict[Tuple[int, int], float]] = None,
                       other_sales_proj: Optional[Dict[Tuple[int, int], float]] = None) -> int:
    """
    Required FOH headcount = max(orders-based, sales-based).

    Pre-open / post-close hours (≤2 orders/hr) return 0 since there are
    essentially no customers to serve, even if someone is clocked in for prep.

    If `other_sales_proj` is provided, the new sales-tier rules apply:
      - Total sales $1000+/hr → 5 (unchanged)
      - Other (non-BS/CW) sales $400+/hr → 4 (new — replaces $525 total)
    Otherwise falls back to legacy total-sales-only rule.
    """
    from shift_optimizer import required_combined, required_for_orders
    orders = orders_proj.get((dow, hour), 0)
    if orders <= 2:
        return 0
    if sales_proj is not None:
        total = sales_proj.get((dow, hour), 0)
        other = other_sales_proj.get((dow, hour), None) if other_sales_proj else None
        return required_combined(orders, total, other)
    return required_for_orders(orders)


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def _seq_color(t: float) -> str:
    """t in [0,1]: cream → tan → amber."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        s = t * 2
        r = int(0xf7 + (0xd9 - 0xf7) * s)
        g = int(0xf2 + (0xb8 - 0xf2) * s)
        b = int(0xe6 + (0x8a - 0xe6) * s)
    else:
        s = (t - 0.5) * 2
        r = int(0xd9 + (0x9d - 0xd9) * s)
        g = int(0xb8 + (0x6a - 0xb8) * s)
        b = int(0x8a + (0x1f - 0x8a) * s)
    return f"#{r:02x}{g:02x}{b:02x}"


def _seq_color_yellow(t: float) -> str:
    """t in [0,1]: cream → soft yellow → gold. Used for the sales-breakdown rows
    (BS+CW sales and Other sales) so they're visually distinct from the brown
    ramp used elsewhere."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        s = t * 2
        # cream (#faf7e8) → soft yellow (#f5e08c)
        r = int(0xfa + (0xf5 - 0xfa) * s)
        g = int(0xf7 + (0xe0 - 0xf7) * s)
        b = int(0xe8 + (0x8c - 0xe8) * s)
    else:
        s = (t - 0.5) * 2
        # soft yellow (#f5e08c) → gold (#c99a1f)
        r = int(0xf5 + (0xc9 - 0xf5) * s)
        g = int(0xe0 + (0x9a - 0xe0) * s)
        b = int(0x8c + (0x1f - 0x8c) * s)
    return f"#{r:02x}{g:02x}{b:02x}"


def sequential_color(value: float, vmax: float) -> str:
    if vmax <= 0: return "#f7f2e6"
    return _seq_color(value / vmax)


def sequential_color_yellow(value: float, vmax: float) -> str:
    if vmax <= 0: return "#faf7e8"
    return _seq_color_yellow(value / vmax)


def headcount_color(n: float) -> str:
    """Categorical headcount color. Same scale for Need and Scheduled."""
    if n < 0.5:  return "#f4f1eb"
    if n < 1.5:  return "#cfddb7"
    if n < 2.5:  return "#9dbf78"
    if n < 3.5:  return "#5d7a3d"
    if n < 4.5:  return "#3d5827"
    return "#243617"


def text_on(bg: str) -> str:
    r = int(bg[1:3], 16); g = int(bg[3:5], 16); b = int(bg[5:7], 16)
    return "#1a1814" if (0.299*r + 0.587*g + 0.114*b) > 150 else "#ffffff"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render_dashboard(uploads_dir: Path, output_path: Path) -> None:
    files = TastFiles(
        sales_by_day=uploads_dir / "Sales_by_day.csv",
        time_of_day=uploads_dir / "Time_of_day__totals_.csv",
        day_of_week=uploads_dir / "Day_of_week__totals_.csv",
        sales_category=uploads_dir / "Sales_category_summary.csv",
    )
    projection, _, metadata = build_projection_from_csvs(files)
    orders_proj, sales_proj = build_hourly_metrics(uploads_dir)

    # Item-level hourly data (Breakfast Sandwich, Chicken Caesar Wrap)
    from item_hourly import load_item_hourly, TRACKED_ITEMS, ITEM_LABELS
    item_hourly = load_item_hourly(uploads_dir)

    # Pre-compute global scale for orders/sales heatmaps (so colors are comparable across days)
    max_orders = max(orders_proj.values()) if orders_proj else 1.0
    max_sales = max(sales_proj.values()) if sales_proj else 1.0
    # Per-item max for color scaling
    max_per_item: Dict[str, float] = {}
    for (dow, h, item), qty in item_hourly.items():
        max_per_item[item] = max(max_per_item.get(item, 0.0), qty)

    # Build other_sales_proj (sales minus BS/CW revenue) for the new need rule.
    # Per-hour BS/CW revenue = BS qty × $12.50 + CW qty × $14.50.
    BS_PRICE_NEED = 12.50
    CW_PRICE_NEED = 14.50
    other_sales_proj: Dict[Tuple[int, int], float] = {}
    for (dow_k, h_k), total in sales_proj.items():
        bs_q = item_hourly.get((dow_k, h_k, "Breakfast Sandwich"), 0.0)
        cw_q = item_hourly.get((dow_k, h_k, "Chicken Caesar Wrap"), 0.0)
        bscw = bs_q * BS_PRICE_NEED + cw_q * CW_PRICE_NEED
        other_sales_proj[(dow_k, h_k)] = max(0, total - min(bscw, total))

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday"]
    short_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    # Pre-compute everything per day
    day_data = []
    week_foh = week_boh = 0.0
    week_foh_hrs = week_boh_hrs = 0.0
    week_foh_shift_count = 0
    week_boh_shift_count = 0
    week_donut = 0.0
    week_donut_hrs = 0.0
    week_donut_count = 0
    for dow in range(7):
        foh_shifts, _ = assign_breaks(list(PROPOSED_SCHEDULE[dow]), dow, projection, orders_proj, sales_proj)
        boh_shifts = boh_shifts_for_day(dow)
        donut_shifts = donut_prep_shifts(dow)
        foh_cost, foh_hrs, _ = schedule_cost(foh_shifts)
        boh_cost = sum(s.cost for s in boh_shifts)
        boh_hrs = sum(s.paid_hours for s in boh_shifts if not s.is_manager)
        donut_cost = sum(s.cost for s in donut_shifts)
        donut_hrs = sum(s.paid_hours for s in donut_shifts)
        week_foh += foh_cost; week_boh += boh_cost
        week_foh_hrs += foh_hrs; week_boh_hrs += boh_hrs
        week_foh_shift_count += len(foh_shifts)
        week_boh_shift_count += len(boh_shifts)
        week_donut += donut_cost
        week_donut_hrs += donut_hrs
        week_donut_count += len(donut_shifts)

        # Per-hour data
        hours_data = []
        for h in DISPLAY_HOURS:
            orders = orders_proj.get((dow, h), 0)
            sales = sales_proj.get((dow, h), 0)
            need = foh_need_for_hour(dow, h, orders_proj, sales_proj, other_sales_proj)
            sched_avg, sched_min = foh_scheduled_for_hour(foh_shifts, h)
            # Per-item quantities for this (dow, hour)
            items = {item: item_hourly.get((dow, h, item), 0.0)
                     for item in TRACKED_ITEMS}
            hours_data.append({
                "h": h, "orders": orders, "sales": sales,
                "need": need,
                "sched_avg": sched_avg,
                "sched_min": sched_min,
                "items": items,
            })

        # Daily projected sales (for labor %)
        daily_proj_sales = sum(d["sales"] for d in hours_data)

        day_data.append({
            "dow": dow,
            "name": day_names[dow],
            "short": short_names[dow],
            "foh_shifts": foh_shifts,
            "boh_shifts": boh_shifts,
            "foh_cost": foh_cost,
            "boh_cost": boh_cost,
            "foh_hrs": foh_hrs,
            "boh_hrs": boh_hrs,
            "donut_shifts": donut_shifts,
            "donut_cost": donut_cost,
            "hours_data": hours_data,
            "proj_sales": daily_proj_sales,
        })

    # Weekly totals
    mgr_weekly = (config.FOH_MANAGER_ANNUAL_SALARY + config.BOH_MANAGER_ANNUAL_SALARY) / 52.0
    week_sales = sum(d["proj_sales"] for d in day_data)
    week_labor = week_foh + week_boh + mgr_weekly
    week_pct = week_labor / week_sales * 100 if week_sales else 0

    output_path.write_text(_build_html(
        day_data, max_orders, max_sales, max_per_item,
        week_sales, week_foh, week_boh, mgr_weekly, week_labor, week_pct,
        week_foh_hrs, week_boh_hrs,
        week_foh_shift_count, week_boh_shift_count,
        metadata.get("date_min"), metadata.get("date_max"),
        metadata.get("days_analyzed"), metadata.get("days_total"),
        uploads_dir,  # for sheet 2
        week_donut, week_donut_hrs, week_donut_count,
    ), encoding="utf-8")


def _build_html(day_data, max_orders, max_sales, max_per_item,
                week_sales, week_foh, week_boh, mgr_weekly, week_labor, week_pct,
                week_foh_hrs, week_boh_hrs,
                week_foh_shift_count, week_boh_shift_count,
                date_min, date_max, days_analyzed, days_total,
                uploads_dir,
                week_donut=0.0, week_donut_hrs=0.0, week_donut_count=0) -> str:
    target_pct = config.LABOR_TARGET_PCT * 100
    diff = week_pct - target_pct
    tol_pp = config.LABOR_WEEK_TOLERANCE * 100
    if abs(diff) <= tol_pp:
        verdict_cls = "ok"; verdict = f"on target ({diff:+.1f}pp)"
    elif diff > 0:
        verdict_cls = "over"; verdict = f"over by {diff:.1f}pp"
    else:
        verdict_cls = "under"; verdict = f"under by {-diff:.1f}pp"

    # Percentages for each labor component (of weekly sales)
    foh_pct = week_foh / week_sales * 100 if week_sales else 0
    boh_pct = week_boh / week_sales * 100 if week_sales else 0
    mgr_pct = mgr_weekly / week_sales * 100 if week_sales else 0

    # Pre-DF (Tab 1a) variant: overnight donut prep added to BOH / total labor.
    week_boh_a = week_boh + week_donut
    week_boh_hrs_a = week_boh_hrs + week_donut_hrs
    week_boh_count_a = week_boh_shift_count + week_donut_count
    week_labor_a = week_labor + week_donut
    boh_pct_a = week_boh_a / week_sales * 100 if week_sales else 0
    week_pct_a = week_labor_a / week_sales * 100 if week_sales else 0
    diff_a = week_pct_a - target_pct
    if abs(diff_a) <= tol_pp:
        verdict_cls_a = "ok"; verdict_a = f"on target ({diff_a:+.1f}pp)"
    elif diff_a > 0:
        verdict_cls_a = "over"; verdict_a = f"over by {diff_a:.1f}pp"
    else:
        verdict_cls_a = "under"; verdict_a = f"under by {-diff_a:.1f}pp"

    # Trim-goal math: how much labor needs to come out (or how much room we have)
    target_labor = week_sales * config.LABOR_TARGET_PCT
    trim_amount = week_labor - target_labor  # positive = over goal, must trim
    # Hours equivalent at assumed blended wage of $20/hr
    HOURS_ASSUMED_WAGE = 20.0
    trim_hours = trim_amount / HOURS_ASSUMED_WAGE
    # Tolerance band: within ±2pp of target reads as "on target" rather than
    # a hard over/under (keeps the banner consistent with the summary verdict).
    if diff > tol_pp:
        trim_cls = "trim-over"
        trim_label = "Over goal — trim needed"
        trim_arrow = "↓"
    elif diff < -tol_pp:
        trim_cls = "trim-under"
        trim_label = "Under goal — room to add"
        trim_arrow = "↑"
    elif trim_amount > 0:
        trim_cls = "trim-ok"
        trim_label = "On target — slightly over, within tolerance"
        trim_arrow = "→"
    elif trim_amount < 0:
        trim_cls = "trim-ok"
        trim_label = "On target — slightly under, within tolerance"
        trim_arrow = "→"
    else:
        trim_cls = "trim-ok"
        trim_label = "At goal"
        trim_arrow = "→"

    # Date range subtitle
    if date_min and date_max:
        baseline_wks = getattr(config, "BASELINE_WEEKS", 4)
        # The weighted baseline uses the most recent `baseline_wks` occurrences
        # of each DOW, i.e. a window starting ~baseline_wks weeks before the last
        # data day — not the full data span.
        from datetime import timedelta as _td
        baseline_min = max(date_min, date_max - _td(weeks=baseline_wks) + _td(days=1))
        date_range_str = (
            f"based on the most recent {baseline_wks} weeks of Toast POS data, "
            f"recency-weighted ({_bd(baseline_min)} – {_bdy(date_max)})"
        )
        tab1_dates = f"{_bd(date_min).upper()} – {_bdy(date_max).upper()}"
    else:
        date_range_str = ""
        tab1_dates = ""

    # Tab date ranges: previous (2), current (3), forward (4)
    try:
        from forward_projection import PROJECTED_DATES, CURRENT_WEEK_DATES, PREVIOUS_WEEK_DATES
        _pv0, _pv1 = PREVIOUS_WEEK_DATES[0], PREVIOUS_WEEK_DATES[-1]
        tab_previous_dates = f"{_bd(_pv0).upper()} – {_bdy(_pv1).upper()}"
        _c0, _c1 = CURRENT_WEEK_DATES[0], CURRENT_WEEK_DATES[-1]
        tab_current_dates = f"{_bd(_c0).upper()} – {_bdy(_c1).upper()}"
        _p0, _p1 = PROJECTED_DATES[0], PROJECTED_DATES[-1]
        tab_forward_dates = f"{_bd(_p0).upper()} – {_bdy(_p1).upper()}"
    except Exception:
        tab_previous_dates = ""
        tab_current_dates = ""
        tab_forward_dates = ""

    # Model comparison vs prior baseline (if configured)
    prior_total = getattr(config, "PRIOR_MODEL_TOTAL_LABOR", None)
    prior_pct = getattr(config, "PRIOR_MODEL_TOTAL_PCT", None)
    prior_label = getattr(config, "PRIOR_MODEL_LABEL", "previous version")
    if prior_total is not None and prior_pct is not None:
        cost_delta = week_labor - prior_total
        pct_delta = week_pct - prior_pct
        if cost_delta > 0.5:
            cmp_cls = "cmp-up"; cmp_arrow = "↑"; cmp_word = "increase"
        elif cost_delta < -0.5:
            cmp_cls = "cmp-down"; cmp_arrow = "↓"; cmp_word = "decrease"
        else:
            cmp_cls = "cmp-flat"; cmp_arrow = "→"; cmp_word = "flat vs"
        comparison_block = f"""
<section class="comparison {cmp_cls}">
  <div class="cmp-label">vs {prior_label}</div>
  <div class="cmp-row">
    <div class="cmp-col">
      <span class="cmp-key">Labor $</span>
      <span class="cmp-val">${prior_total:,.0f} <span class="cmp-arrow">{cmp_arrow}</span> ${week_labor:,.0f}</span>
      <span class="cmp-delta">{cost_delta:+,.0f} ({cost_delta/prior_total*100:+.1f}%)</span>
    </div>
    <div class="cmp-col">
      <span class="cmp-key">Labor %</span>
      <span class="cmp-val">{prior_pct:.2f}% <span class="cmp-arrow">{cmp_arrow}</span> {week_pct:.2f}%</span>
      <span class="cmp-delta">{pct_delta:+.2f} pp</span>
    </div>
  </div>
</section>
"""
    else:
        comparison_block = ""

    days_html_a = "\n".join(_day_block(d, max_orders, max_sales, max_per_item, include_donut=True) for d in day_data)
    days_html_b = "\n".join(_day_block(d, max_orders, max_sales, max_per_item, include_donut=False) for d in day_data)
    days_html = days_html_b  # back-compat alias

    # Build Sheet 2 (current week) and Sheet 3 (forward projection)
    try:
        from forward_dashboard import render_sheet_2, SHEET_2_CSS
        from forward_projection import (
            CURRENT_WEEK_CONFIG, FORWARD_WEEK_CONFIG, PREVIOUS_WEEK_CONFIG,
        )
        # Refresh forward projection lock + freeze any week that has rolled into
        # the current week, then read actuals back in the accuracy table.
        try:
            from projection_lock import maintain_locks
            maintain_locks(uploads_dir)
        except Exception as le:
            print(f"[warning] lock maintenance failed: {le}")
        sheet_previous_html = render_sheet_2(uploads_dir, PREVIOUS_WEEK_CONFIG)
        sheet_current_html = render_sheet_2(uploads_dir, CURRENT_WEEK_CONFIG)
        sheet_forward_html = render_sheet_2(uploads_dir, FORWARD_WEEK_CONFIG)
        sheet_2_css = SHEET_2_CSS
    except Exception as e:
        # Fail gracefully if the sheet panels have issues; just skip them
        import traceback
        print(f"[warning] sheet panels failed: {e}")
        traceback.print_exc()
        sheet_previous_html = ""
        sheet_current_html = ""
        sheet_forward_html = ""
        sheet_2_css = ""

    # -- Tab 1a (Pre-DF baseline): same schedule + overnight donut prep --------
    panel_1a = f"""<div id="tab-baseline-a" class="tab-panel active" role="tabpanel">
<header class="masthead">
  <h1>Knotted AD <em>— Baseline</em></h1>
  <div class="sub">Baseline schedule + early donut prep (3am&ndash;11:30am)</div>
</header>
<section class="summary">
  <div class="col">
    <div class="k">Projected Weekly Sales</div>
    <div class="v">${week_sales:,.0f}</div>
    <div class="meta">{date_range_str}</div>
  </div>
  <div class="col">
    <div class="k">FOH Hourly</div>
    <div class="v">${week_foh:,.0f} <span class="v-pct">{foh_pct:.1f}%</span></div>
    <div class="meta">{week_foh_hrs:.1f} paid hrs · {week_foh_shift_count} shifts</div>
  </div>
  <div class="col">
    <div class="k">BOH Hourly <span class="v-pct">incl. donut</span></div>
    <div class="v">${week_boh_a:,.0f} <span class="v-pct">{boh_pct_a:.1f}%</span></div>
    <div class="meta">{week_boh_hrs_a:.1f} paid hrs · {week_boh_count_a} shifts</div>
  </div>
  <div class="col">
    <div class="k">Manager Salaries</div>
    <div class="v">${mgr_weekly:,.0f} <span class="v-pct">{mgr_pct:.1f}%</span></div>
    <div class="meta">{int(config.MANAGER_SALARY_ALLOCATION*100)}% of ${(config.FOH_MANAGER_ANNUAL_SALARY_FULL + config.BOH_MANAGER_ANNUAL_SALARY_FULL)/1000:g}K/yr</div>
  </div>
  <div class="col">
    <div class="k">Total Labor</div>
    <div class="v"><span class="pct {verdict_cls_a}">{week_pct_a:.1f}%</span> <span class="v-sub">${week_labor_a:,.0f}</span></div>
    <div class="meta">{verdict_a} of {target_pct:.0f}%</div>
  </div>
</section>
<div class="alloc-note">
  <strong>In-house donut production.</strong> The baseline schedule
  plus <strong>early donut prep</strong>: {week_donut_count} shifts/week
  (1&times;/day), 3:00am&ndash;11:30am, 8h each =
  <strong>${week_donut:,.0f}/week</strong> added BOH labor. Donut prep is listed per
  day and included in the labor totals above; the chart still starts at 6am, so the
  3am&ndash;6am pre-open hours are noted but not drawn on the bar chart.
</div>
{days_html_a}
</div><!-- /tab-baseline-a -->
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Knotted Weekly Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Newsreader:opsz,wght@6..72,300;6..72,400;6..72,500;6..72,600;6..72,700&family=Inter:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #faf8f5; --ink: #1a1814; --ink-soft: #6b6359; --rule: #e5dfd4;
  --opener: #5d7a3d; --mid: #7b9c5c; --dish: #c4541f; --station: #9d3812;
  --boh: #5c6b78; --boh-mgr: #3e4c58;
  --jr: #7b9c5c; --md: #c4541f; --sr: #5a3a78;
  /* Role-based (simplified): front-of-house = green, closers = purple */
  --front: #5d7a3d;     /* sage green, opener/rush/peak/mid */
  --closer: #5a3a78;    /* deep purple, dish closer + station closer */
  --unfilled: #e6b422;  /* amber yellow — planned but unstaffed (TBD) */
  --unfilled-stripe: #d9a200; --unfilled-edge: #a87d00;
  --good: #4a6b2c; --warn: #b88718; --bad: #9d3812;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: 'Inter', system-ui, sans-serif;
  background: var(--bg); color: var(--ink);
  padding: 28px 28px 60px; max-width: 1600px; margin: 0 auto;
  font-size: 12px; line-height: 1.5;
}}
header.masthead {{
  display: flex; align-items: baseline; justify-content: space-between;
  padding-bottom: 14px; margin-bottom: 22px; border-bottom: 2px solid var(--ink);
}}
.masthead h1 {{ font-family: 'Newsreader', Georgia, serif; font-weight: 700; font-size: 30px; letter-spacing: -0.02em; }}
.masthead h1 em {{ font-style: italic; font-weight: 400; color: var(--ink-soft); }}
.masthead .sub {{ font-family: 'Inter', sans-serif; font-size: 11px; color: var(--ink-soft); text-transform: uppercase; letter-spacing: 0.08em; }}

.summary {{
  display: grid; grid-template-columns: 1.4fr 1fr 1fr 1fr 1.2fr; gap: 0;
  margin-bottom: 20px; border: 1px solid var(--rule); background: white;
}}
.summary .col {{
  padding: 14px 18px; border-right: 1px solid var(--rule);
  display: flex; flex-direction: column; justify-content: flex-start;
  min-height: 78px;
}}
.summary .col:last-child {{ border: none; }}
.summary .k {{
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--ink-soft); margin-bottom: 6px;
}}
.summary .v {{
  font-family: 'Newsreader', Georgia, serif; font-size: 22px; font-weight: 700;
  letter-spacing: -0.01em; line-height: 1.1;
}}
.summary .v.mono {{ font-family: 'Inter', sans-serif; font-weight: 500; }}
.summary .v-pct {{
  font-family: 'Inter', sans-serif; font-size: 12px; font-weight: 500;
  color: var(--ink-soft); margin-left: 6px; letter-spacing: 0;
}}
.summary .v-sub {{
  font-family: 'Inter', sans-serif; font-size: 12px; font-weight: 500;
  color: var(--ink-soft); margin-left: 6px; letter-spacing: 0;
}}
.summary .meta {{
  font-size: 10px; color: var(--ink-soft); margin-top: auto; padding-top: 6px;
  font-family: 'Inter', sans-serif; line-height: 1.4;
}}
.pct.ok {{ color: var(--good); }} .pct.under {{ color: var(--good); }} .pct.over {{ color: var(--bad); }}

/* Allocation note (manager salary split disclosure) */
.alloc-note {{
  margin-bottom: 18px; padding: 9px 14px;
  background: rgba(0,0,0,0.025);
  border-left: 3px solid var(--ink-soft);
  font-family: 'Inter', sans-serif;
  font-size: 11px; line-height: 1.5;
  color: var(--ink-soft);
}}
.alloc-note strong {{ color: var(--ink); font-weight: 600; }}
.alloc-note.partial-day-note {{
  background: rgba(196, 84, 31, 0.06);
  border-left-color: #c4541f;
  color: #5a3a2a;
}}
.alloc-note.partial-day-note strong {{ color: #7a2818; }}

/* Model comparison block (sits right below goal banner) */
.comparison {{
  display: flex; align-items: stretch; gap: 0;
  margin-bottom: 22px; background: white;
  border: 1px solid var(--rule);
  font-family: 'Inter', sans-serif;
}}
.comparison .cmp-label {{
  padding: 14px 18px;
  font-family: 'Inter', sans-serif;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--ink-soft);
  background: rgba(0,0,0,0.025);
  border-right: 1px solid var(--rule);
  display: flex; align-items: center;
  white-space: nowrap;
}}
.comparison .cmp-row {{
  flex: 1;
  display: flex;
  gap: 0;
}}
.comparison .cmp-col {{
  flex: 1; padding: 12px 22px;
  display: flex; flex-direction: column; gap: 2px;
  border-right: 1px solid var(--rule);
}}
.comparison .cmp-col:last-child {{ border-right: none; }}
.comparison .cmp-key {{
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.1em;
  color: var(--ink-soft);
}}
.comparison .cmp-val {{
  font-family: 'Inter', sans-serif;
  font-size: 14px; font-weight: 500;
  color: var(--ink); letter-spacing: -0.01em;
}}
.comparison .cmp-arrow {{
  font-family: 'Newsreader', Georgia, serif;
  font-size: 16px; font-weight: 700;
  margin: 0 6px;
  opacity: 0.5;
}}
.comparison.cmp-up .cmp-arrow {{ color: var(--bad); opacity: 0.85; }}
.comparison.cmp-down .cmp-arrow {{ color: var(--good); opacity: 0.85; }}
.comparison.cmp-flat .cmp-arrow {{ color: var(--ink-soft); }}
.comparison .cmp-delta {{
  font-family: 'Inter', sans-serif;
  font-size: 11px; font-weight: 500;
  color: var(--ink-soft);
}}
.comparison.cmp-up .cmp-delta {{ color: var(--bad); }}
.comparison.cmp-down .cmp-delta {{ color: var(--good); }}

/* Labor goal banner */
.goal-banner {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 16px 22px; margin-bottom: 20px;
  border-left: 4px solid; border-radius: 0;
  font-family: 'Inter', sans-serif;
}}
.goal-banner.trim-over {{
  background: #fdf1e6; border-left-color: var(--bad); color: #5a2810;
}}
.goal-banner.trim-under {{
  background: #ecf3e3; border-left-color: var(--good); color: #2d4517;
}}
.goal-banner.trim-ok {{
  background: #f0ede5; border-left-color: var(--ink-soft); color: var(--ink);
}}
.goal-left {{ display: flex; align-items: center; gap: 16px; }}
.goal-arrow {{
  font-family: 'Newsreader', Georgia, serif; font-size: 36px; font-weight: 700;
  line-height: 1; opacity: 0.85;
}}
.goal-label {{
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;
  font-weight: 600; margin-bottom: 3px;
}}
.goal-detail {{
  font-family: 'Inter', sans-serif; font-size: 12px;
  font-weight: 500; opacity: 0.85;
}}
.goal-right {{ text-align: right; }}
.goal-amount {{
  font-family: 'Newsreader', Georgia, serif; font-size: 28px; font-weight: 700;
  letter-spacing: -0.01em; line-height: 1.1;
}}
.goal-unit {{
  font-family: 'Inter', sans-serif; font-size: 13px;
  font-weight: 500; opacity: 0.7; margin-left: 2px;
}}
.goal-annual {{
  font-family: 'Inter', sans-serif; font-size: 11px;
  font-weight: 500; opacity: 0.75; margin-top: 4px;
}}

.legend {{ display: flex; gap: 14px; flex-wrap: wrap; align-items: center; margin-bottom: 18px; font-size: 10px; color: var(--ink-soft); }}
.legend.assumptions {{ margin-top: -10px; margin-bottom: 22px; padding-top: 10px; border-top: 1px dashed var(--rule); }}
.legend .group {{ display: flex; gap: 10px; align-items: center; padding-right: 14px; border-right: 1px solid var(--rule); }}
.legend .group:last-child {{ border-right: none; }}
.legend .item {{ display: flex; align-items: center; gap: 5px; }}
.legend .item.rule {{ font-family: 'Inter', sans-serif; font-size: 10px; padding: 1px 6px; background: rgba(0,0,0,0.025); border-radius: 3px; }}
.legend .item.rule.sep {{ background: rgba(124,156,92,0.10); }}
.legend .sw {{ width: 14px; height: 12px; border: 1px solid rgba(0,0,0,0.08); }}
.sw-opener {{ background: var(--opener); }}
.sw-mid    {{ background: var(--mid); }}
.sw-dish   {{ background: var(--dish); }}
.sw-station{{ background: var(--station); }}
.sw-front  {{ background: var(--front); }}
.sw-closer {{ background: var(--closer); }}
.sw-boh    {{ background: var(--boh); }}
.sw-boh-mgr {{ background: var(--boh-mgr); }}
.sw-unfilled {{ background: var(--unfilled); border: 1px dashed var(--unfilled-edge); }}
.sw-hc-0 {{ background: #f4f1eb; }}
.sw-hc-1 {{ background: #cfddb7; }}
.sw-hc-2 {{ background: #9dbf78; }}
.sw-hc-3 {{ background: #5d7a3d; }}
.sw-hc-4 {{ background: #3d5827; }}
.sw-hc-5 {{ background: #243617; }}

.day {{
  background: white; border: 1px solid var(--rule); margin-bottom: 18px;
  page-break-inside: avoid;
}}
.day-head {{
  display: flex; padding: 12px 18px; border-bottom: 1px solid var(--rule);
  align-items: center; gap: 16px; justify-content: space-between;
}}
.day-head .name {{ display: flex; align-items: baseline; gap: 10px; }}
.day-head .dnum {{ font-family: 'Newsreader', Georgia, serif; font-size: 22px; font-weight: 700; }}
.day-head .dlabel {{ font-family: 'Newsreader', Georgia, serif; font-style: italic; font-size: 14px; color: var(--ink-soft); }}
.day-head .stats {{ display: flex; gap: 18px; font-family: 'Inter', sans-serif; font-size: 11px; }}
.day-head .stats .stat {{ text-align: right; }}
.day-head .stats .stat .lbl {{ display: block; font-size: 9px; color: var(--ink-soft); text-transform: uppercase; letter-spacing: 0.06em; }}
.day-head .stats .stat .val {{ font-weight: 600; }}
.day-head .stats .pct.ok    .val {{ color: var(--good); }}
.day-head .stats .pct.over  .val {{ color: var(--bad); }}
.day-head .stats .pct.under .val {{ color: var(--good); }}

.day-body {{ padding: 14px 18px 16px; }}

/* Shift bars (Gantt) */
.timeline-axis {{
  display: flex; padding-left: 100px; margin-bottom: 4px;
  font-family: 'Inter', sans-serif; font-size: 9px; color: var(--ink-soft);
}}
.timeline-axis .tick {{
  flex: 1; text-align: left; border-left: 1px solid var(--rule); padding-left: 3px;
}}
.shift-row {{ display: flex; align-items: center; margin-bottom: 3px; }}
.shift-row .lbl {{
  width: 100px; padding-right: 8px; font-size: 10px;
  display: flex; align-items: center; gap: 5px;
}}
.shift-row .lbl .dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
.dot-opener  {{ background: var(--opener); }}
.dot-mid     {{ background: var(--mid); }}
.dot-dish    {{ background: var(--dish); }}
.dot-station {{ background: var(--station); }}
.dot-boh     {{ background: var(--boh); }}
.dot-boh-mgr {{ background: var(--boh-mgr); }}
.dot-donut   {{ background: #b07a3c; }}
.donut-row .track {{ display: flex; align-items: center; }}
.donut-note {{
  font-size: 11px; color: var(--ink-soft); font-style: italic;
  font-family: 'Inter', sans-serif;
}}
.dot-jr {{ background: var(--jr); }}
.dot-md {{ background: var(--md); }}
.dot-sr {{ background: var(--sr); }}
.dot-front   {{ background: var(--front); }}
.dot-closer  {{ background: var(--closer); }}
.dot-unfilled {{ background: var(--unfilled); border: 1px solid var(--unfilled-edge); }}

.shift-row .track {{
  flex: 1; height: 22px; background: #f9f7f1;
  border: 1px solid var(--rule); position: relative;
}}
.bar {{
  position: absolute; top: 0; bottom: 0;
  color: white; font-family: 'Inter', sans-serif; font-size: 9px;
  font-weight: 500; display: flex; align-items: center; padding: 0 6px;
  overflow: hidden; white-space: nowrap;
}}
.bar.b-opener  {{ background: var(--opener); }}
.bar.b-mid     {{ background: var(--mid); }}
.bar.b-dish    {{ background: var(--dish); }}
.bar.b-station {{ background: var(--station); }}
.bar.b-boh     {{ background: var(--boh); }}
.bar.b-boh-mgr {{ background: var(--boh-mgr); }}
.bar.b-jr {{ background: var(--jr); }}
.bar.b-md {{ background: var(--md); }}
.bar.b-sr {{ background: var(--sr); }}
.bar.b-front  {{ background: var(--front); }}
.bar.b-closer {{ background: var(--closer); }}
.bar.b-unfilled {{
  background: repeating-linear-gradient(45deg,
    var(--unfilled), var(--unfilled) 7px,
    var(--unfilled-stripe) 7px, var(--unfilled-stripe) 14px);
  color: #4a3a00; border: 1px dashed var(--unfilled-edge);
}}
.tbd-tag {{
  margin-left: 5px; font-family: 'Inter', sans-serif;
  font-size: 8px; font-weight: 600; letter-spacing: 0.05em;
  background: var(--unfilled); color: #4a3a00;
  border: 1px solid var(--unfilled-edge);
  padding: 0 4px; border-radius: 7px; vertical-align: middle;
}}
.bar .brk {{
  position: absolute; top: 0; bottom: 0;
  background: repeating-linear-gradient(
    45deg, rgba(255,255,255,0.55), rgba(255,255,255,0.55) 3px,
    transparent 3px, transparent 7px);
  border-left: 1px solid rgba(255,255,255,0.6);
  border-right: 1px solid rgba(255,255,255,0.6);
  display: flex; align-items: center; justify-content: center;
  overflow: visible;
}}
.bar .brk .brk-label {{
  font-family: 'Inter', sans-serif;
  font-size: 8px; font-weight: 600;
  color: white;
  background: rgba(0,0,0,0.40);
  padding: 1px 3px;
  border-radius: 2px;
  white-space: nowrap;
  letter-spacing: -0.02em;
}}

/* Heatmaps */
.heatmaps {{ margin-top: 14px; }}
.heat-row {{ display: flex; align-items: center; margin-bottom: 2px; }}
.heat-row .lbl {{
  width: 100px; padding-right: 8px; font-size: 10px;
  font-family: 'Inter', sans-serif; color: var(--ink-soft);
  display: flex; flex-direction: column; justify-content: center;
  gap: 1px;
}}
.heat-row .lbl .name-row {{
  display: flex; align-items: center; gap: 4px;
}}
.heat-row .lbl .name {{ font-weight: 500; color: var(--ink); }}
.heat-row .lbl .total-row {{
  font-size: 10px;
  display: flex; gap: 4px; align-items: baseline;
}}
.heat-row .lbl .total-proj {{ font-weight: 600; color: var(--ink); }}
.heat-row .lbl .total-avg {{ color: var(--ink-soft); }}
.heat-row .cells {{ flex: 1; display: flex; gap: 1px; height: 28px; }}
.heat-cell {{
  flex: 1; display: flex; align-items: center; justify-content: center;
  font-family: 'Inter', sans-serif; font-size: 10px;
  font-weight: 500; border-radius: 1px;
}}
.heat-row.compare .lbl {{ font-weight: 600; }}

.divider {{ height: 8px; }}

@media print {{
  body {{ padding: 14px; background: white; font-size: 10px; }}
  .day {{ border: 1px solid var(--rule); }}
  .page-break {{ page-break-before: always; }}
  /* For print: show both tabs as pages */
  .tabs {{ display: none; }}
  .tab-panel {{ display: block !important; }}
  .tab-panel + .tab-panel {{ page-break-before: always; }}
}}

/* ---- Tab navigation ---- */
.tabs {{
  display: flex; gap: 0; margin-bottom: 22px;
  border-bottom: 2px solid var(--ink);
}}
.tab-btn {{
  display: flex; align-items: baseline; gap: 10px;
  padding: 14px 22px 12px; background: transparent;
  border: none; border-bottom: 3px solid transparent;
  margin-bottom: -2px; cursor: pointer;
  font-family: 'Inter', sans-serif;
  color: var(--ink-soft);
  transition: color 0.15s, border-color 0.15s, background 0.15s;
  text-align: left;
}}
.tab-btn:hover {{ color: var(--ink); background: rgba(0,0,0,0.02); }}
.tab-btn.active {{
  color: var(--ink);
  border-bottom-color: var(--ink);
  background: white;
}}
.tab-btn .tab-num {{
  font-family: 'Newsreader', Georgia, serif; font-size: 22px; font-weight: 700;
  letter-spacing: -0.01em; opacity: 0.4;
}}
.tab-btn.active .tab-num {{ opacity: 1; }}
.tab-btn .tab-title {{
  font-size: 15px; font-weight: 600; letter-spacing: -0.01em;
}}
.tab-btn .tab-sub {{
  font-family: 'Inter', sans-serif;
  font-size: 10px; color: var(--ink-soft);
  text-transform: uppercase; letter-spacing: 0.06em;
}}

.tab-panel {{ display: none; }}
.tab-panel.active {{ display: block; }}

{sheet_2_css}
{RESTYLE_CSS}
</style></head>
<body>
<nav class="tabs" role="tablist">
  <button class="tab-btn active" data-tab="baseline-a" role="tab" aria-selected="true">
    <span class="tab-num">1</span>
    <span class="tab-title">Baseline</span>
    <span class="tab-sub">{tab1_dates}</span>
  </button>
  <button class="tab-btn" data-tab="previous-week" role="tab" aria-selected="false">
    <span class="tab-num">2</span>
    <span class="tab-title">Previous Week</span>
    <span class="tab-sub">{tab_previous_dates}</span>
  </button>
  <button class="tab-btn" data-tab="current-week" role="tab" aria-selected="false">
    <span class="tab-num">3</span>
    <span class="tab-title">Current Week</span>
    <span class="tab-sub">{tab_current_dates}</span>
  </button>
  <button class="tab-btn" data-tab="projection" role="tab" aria-selected="false">
    <span class="tab-num">4</span>
    <span class="tab-title">Forward Projection</span>
    <span class="tab-sub">{tab_forward_dates}</span>
  </button>
</nav>

{panel_1a}


<div id="tab-previous-week" class="tab-panel" role="tabpanel">
{sheet_previous_html}
</div><!-- /tab-previous-week -->

<div id="tab-current-week" class="tab-panel" role="tabpanel">
{sheet_current_html}
</div><!-- /tab-current-week -->

<div id="tab-projection" class="tab-panel" role="tabpanel">
{sheet_forward_html}
</div><!-- /tab-projection -->

<script>
(function() {{
  const tabs = document.querySelectorAll('.tab-btn');
  const panels = document.querySelectorAll('.tab-panel');
  tabs.forEach(function(t) {{
    t.addEventListener('click', function() {{
      const target = t.dataset.tab;
      tabs.forEach(function(b) {{
        const active = (b.dataset.tab === target);
        b.classList.toggle('active', active);
        b.setAttribute('aria-selected', active ? 'true' : 'false');
      }});
      panels.forEach(function(p) {{
        p.classList.toggle('active', p.id === ('tab-' + target));
      }});
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
    }});
  }});
}})();
</script>

</body>
</html>
"""


def _day_block(d, max_orders, max_sales, max_per_item, include_donut=False) -> str:
    target_pct = config.LABOR_TARGET_PCT * 100
    mgr_daily = (config.FOH_MANAGER_ANNUAL_SALARY + config.BOH_MANAGER_ANNUAL_SALARY) / 52.0 / 7.0
    donut_cost = d.get("donut_cost", 0.0) if include_donut else 0.0
    boh_cost_disp = d["boh_cost"] + donut_cost
    day_labor = d["foh_cost"] + boh_cost_disp + mgr_daily
    day_pct = day_labor / d["proj_sales"] * 100 if d["proj_sales"] else 0
    if abs(day_pct - target_pct) <= 2:
        pct_cls = "ok"
    elif day_pct > target_pct:
        pct_cls = "over"
    else:
        pct_cls = "under"

    # --- Shift bars ---
    DAY_START = 6 * 60
    DAY_END = 23 * 60
    SPAN = DAY_END - DAY_START
    def pct_left(m: int) -> float:
        return max(0, min(100, (m - DAY_START) / SPAN * 100))

    axis = ''.join(f'<div class="tick">{h:02d}</div>' for h in range(6, 24))

    def _role_class(s: FOHShift) -> str:
        """Map shift label to role-based CSS class.
        Closers (dish + station) → purple. All others (opener, rush, peak, mid)
        → green. This overrides the prior tier-based coloring.
        """
        label_lower = s.label.lower()
        if label_lower.startswith("closer"):
            return "closer"
        return "front"

    def render_foh_bar(s: FOHShift) -> str:
        bl = pct_left(s.start_min)
        bw = pct_left(s.end_min) - bl
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
        role = _role_class(s)
        return (
            f'<div class="shift-row">'
            f'<div class="lbl"><span class="dot dot-{role}"></span>{s.label}</div>'
            f'<div class="track">'
            f'<div class="bar b-{role}" style="left:{bl}%;width:{bw}%">'
            f'{s.start_str}–{s.end_str}{brk}</div>'
            f'</div></div>'
        )

    def render_boh_bar(b: BOHShift) -> str:
        bl = pct_left(b.start_min)
        br = pct_left(b.end_min)
        bl = max(bl, 0.0)           # clamp pre-6a start (donut 3a) to the chart edge
        bw = min(br, 100.0) - bl    # clamp post-midnight end (PM prep 1a) to the edge
        # Break stripe only for hourly cooks that actually have a break time set
        if b.is_manager or not b.break_start_min:
            brk = ""
        else:
            bs_rel = (b.break_start_min - b.start_min) / (b.end_min - b.start_min) * 100
            bw_rel = 30 / (b.end_min - b.start_min) * 100
            brk_h, brk_m = divmod(b.break_start_min, 60)
            brk_label = f"{brk_h:02d}:{brk_m:02d}"
            brk = (
                f'<div class="brk" style="left:{bs_rel}%;width:{bw_rel}%">'
                f'<span class="brk-label">{brk_label}</span>'
                f'</div>'
            )
        cls = "b-boh-mgr" if b.is_manager else "b-boh"
        dot_cls = "dot-boh-mgr" if b.is_manager else "dot-boh"
        return (
            f'<div class="shift-row">'
            f'<div class="lbl"><span class="dot {dot_cls}"></span>{b.label}</div>'
            f'<div class="track">'
            f'<div class="bar {cls}" style="left:{bl}%;width:{bw}%">'
            f'{b.start_str}–{b.end_str}{brk}</div>'
            f'</div></div>'
        )

    foh_bars = "\n".join(render_foh_bar(s) for s in d["foh_shifts"])
    boh_bars = "\n".join(render_boh_bar(b) for b in d["boh_shifts"])

    # Donut prep: drawn as a BOH bar too. The 3a clock-in clamps to the 6a chart
    # edge; the bar label shows the real 03:00-11:30 (noting the 3a start).
    donut_list = ""
    if include_donut and d.get("donut_shifts"):
        donut_list = "\n".join(render_boh_bar(b) for b in d["donut_shifts"])

    # --- Heatmaps ---
    def heat_row(label_main: str, label_sub: str, cells_html: str, compare: bool = False, total_line: str = "") -> str:
        cls = "heat-row compare" if compare else "heat-row"
        total_html = f'<div class="total-row">{total_line}</div>' if total_line else ""
        return (f'<div class="{cls}">'
                f'<div class="lbl">'
                f'<div class="name-row"><span class="name">{label_main}</span>'
                f'{f"<span>{label_sub}</span>" if label_sub else ""}</div>'
                f'{total_html}'
                f'</div>'
                f'<div class="cells">{cells_html}</div></div>')

    # Orders heatmap
    orders_cells = ""
    for hd in d["hours_data"]:
        bg = sequential_color(hd["orders"], max_orders)
        tc = text_on(bg)
        display = f'{hd["orders"]:.0f}' if hd["orders"] >= 1 else "—"
        orders_cells += f'<div class="heat-cell" style="background:{bg};color:{tc};">{display}</div>'

    # Sales heatmap
    sales_cells = ""
    for hd in d["hours_data"]:
        bg = sequential_color(hd["sales"], max_sales)
        tc = text_on(bg)
        display = f'${hd["sales"]:.0f}' if hd["sales"] >= 1 else "—"
        sales_cells += f'<div class="heat-cell" style="background:{bg};color:{tc};">{display}</div>'

    # Need
    need_cells = ""
    for hd in d["hours_data"]:
        bg = headcount_color(hd["need"])
        tc = text_on(bg)
        need_cells += f'<div class="heat-cell" style="background:{bg};color:{tc};">{hd["need"]}</div>'

    # Scheduled — show AVG headcount during the hour, rounded to nearest 0.5,
    # so partial coverage (someone clocks in/out or breaks mid-hour) is visible.
    # Color uses the floor of avg (worst-case bound) so half-coverage hours
    # don't get flattered by rounding up to the higher headcount tier.
    sched_cells = ""
    for hd in d["hours_data"]:
        avg = hd["sched_avg"]
        # Round to nearest 0.5
        rounded = round(avg * 2) / 2
        # Color by the floor (e.g., 2.5 colored as 2, so partial coverage doesn't read as full)
        import math
        bg = headcount_color(math.floor(rounded))
        tc = text_on(bg)
        if rounded == 0:
            display = "—"
        elif rounded == int(rounded):
            display = f'{int(rounded)}'
        else:
            display = f'{rounded:.1f}'
        sched_cells += f'<div class="heat-cell" style="background:{bg};color:{tc};">{display}</div>'

    # Item heatmap cells: Breakfast Sandwich + Caesar Wrap per hour
    # Use a SHARED max across BS and CW so the color intensity is comparable
    # between the two rows (e.g. 7 in CW shouldn't be darker than 10 in BS).
    from item_hourly import TRACKED_ITEMS, ITEM_LABELS
    item_cells: Dict[str, str] = {}
    item_totals: Dict[str, float] = {}
    shared_item_max = max(
        (max_per_item.get(item, 1.0) or 1.0) for item in TRACKED_ITEMS
    )
    for item in TRACKED_ITEMS:
        cells = ""
        total = 0.0
        for hd in d["hours_data"]:
            qty = hd["items"].get(item, 0.0)
            total += qty
            if qty < 0.5:
                cells += f'<div class="heat-cell" style="background:#f5f1ea;color:#9b9387">—</div>'
            else:
                bg = sequential_color(qty, shared_item_max)
                tc = text_on(bg)
                # Round to whole units for display (per-day-of-week averages can be fractional)
                display = f'{qty:.0f}' if qty >= 1 else f'{qty:.1f}'
                cells += f'<div class="heat-cell" style="background:{bg};color:{tc};">{display}</div>'
        item_cells[item] = cells
        item_totals[item] = total

    # BS+CW sales and Other sales per hour (historical, for Tab 1)
    # Use YELLOW color scale to visually distinguish from the brown-ramp rows
    # above. Share max across both rows so they're directly comparable.
    BS_PRICE = 12.50
    CW_PRICE = 14.50
    bscw_sales_hourly: List[float] = []
    other_sales_hourly: List[float] = []
    for hd in d["hours_data"]:
        bs_qty = hd["items"].get("Breakfast Sandwich", 0.0)
        cw_qty = hd["items"].get("Chicken Caesar Wrap", 0.0)
        bscw = bs_qty * BS_PRICE + cw_qty * CW_PRICE
        bscw = min(bscw, hd["sales"])
        other = max(0, hd["sales"] - bscw)
        bscw_sales_hourly.append(bscw)
        other_sales_hourly.append(other)
    shared_sales_max = max(
        max(bscw_sales_hourly) if bscw_sales_hourly else 1.0,
        max(other_sales_hourly) if other_sales_hourly else 1.0,
    )
    bscw_sales_day = sum(bscw_sales_hourly)
    other_sales_day = sum(other_sales_hourly)

    bscw_sales_cells = ""
    other_sales_cells = ""
    for i, hd in enumerate(d["hours_data"]):
        bscw_h = bscw_sales_hourly[i]
        if bscw_h > 0.5:
            bg = sequential_color_yellow(bscw_h, shared_sales_max)
            tc = text_on(bg)
            bscw_sales_cells += f'<div class="heat-cell" style="background:{bg};color:{tc};">${bscw_h:.0f}</div>'
        else:
            bscw_sales_cells += f'<div class="heat-cell" style="background:#faf7e8;color:#9b9387">—</div>'

        other_h = other_sales_hourly[i]
        if other_h > 0.5:
            bg = sequential_color_yellow(other_h, shared_sales_max)
            tc = text_on(bg)
            other_sales_cells += f'<div class="heat-cell" style="background:{bg};color:{tc};">${other_h:.0f}</div>'
        else:
            other_sales_cells += f'<div class="heat-cell" style="background:#faf7e8;color:#9b9387">$0</div>'

    # Hour axis above heatmaps
    axis_cells = ''.join(
        f'<div class="heat-cell" style="background:transparent;color:var(--ink-soft);font-weight:500;">{h:02d}</div>'
        for h in DISPLAY_HOURS
    )

    return f"""
<section class="day">
  <div class="day-head">
    <div class="name">
      <div class="dnum">{d['short']}</div>
      <div class="dlabel">{d['name']}</div>
    </div>
    <div class="stats">
      <div class="stat"><span class="lbl">Sales</span><span class="val">${d['proj_sales']:,.0f}</span></div>
      <div class="stat"><span class="lbl">FOH $</span><span class="val">${d['foh_cost']:,.0f}</span></div>
      <div class="stat"><span class="lbl">BOH $</span><span class="val">${boh_cost_disp:,.0f}</span></div>
      <div class="stat"><span class="lbl">Mgr $</span><span class="val">${mgr_daily:,.0f}</span></div>
      <div class="stat pct {pct_cls}"><span class="lbl">Labor %</span><span class="val">{day_pct:.1f}%</span></div>
    </div>
  </div>
  <div class="day-body">
    <div class="timeline-axis">{axis}</div>
    {foh_bars}
    {boh_bars}
    {donut_list}

    <div class="heatmaps">
      <div class="heat-row">
        <div class="lbl"></div>
        <div class="cells">{axis_cells}</div>
      </div>
      {heat_row("Orders", "/hr", orders_cells)}
      {heat_row("Sales", "/hr", sales_cells)}
      {heat_row("BS+CW sales", "/hr", bscw_sales_cells, total_line=f'<span class="total-proj">${bscw_sales_day:.0f}</span><span class="total-avg">/ day</span>')}
      {heat_row("Other sales", "/hr", other_sales_cells, total_line=f'<span class="total-proj">${other_sales_day:.0f}</span><span class="total-avg">/ day</span>')}
      {heat_row("FOH need", "people", need_cells, compare=True)}
      {heat_row("FOH sched", "people", sched_cells, compare=True)}
      {heat_row(ITEM_LABELS["Breakfast Sandwich"], "/hr", item_cells["Breakfast Sandwich"], total_line=f'<span class="total-proj">{item_totals["Breakfast Sandwich"]:.0f}</span><span class="total-avg">/ day</span>')}
      {heat_row(ITEM_LABELS["Chicken Caesar Wrap"], "/hr", item_cells["Chicken Caesar Wrap"], total_line=f'<span class="total-proj">{item_totals["Chicken Caesar Wrap"]:.0f}</span><span class="total-avg">/ day</span>')}
    </div>
  </div>
</section>
"""


if __name__ == "__main__":
    uploads = Path(__file__).parent / "data"
    out = Path(config.OUTPUT_DIR) / "weekly_dashboard.html"
    render_dashboard(uploads, out)
    print(f"✓ Dashboard: {out}")
