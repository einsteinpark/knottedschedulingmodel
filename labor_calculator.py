"""
Labor cost calculator.

Applies California overtime rules:
  - >8 hrs in a single day      -> 1.5x for hours 8-12
  - >12 hrs in a single day     -> 2.0x for hours past 12
  - >40 hrs in a single week    -> 1.5x for those hours (whichever is greater)

For schedule-time estimates we assume each shift = one employee, so daily OT
is computed per shift (paid hours) and weekly OT is computed across all shifts
sharing the same role bucket for the week. In practice you'd assign these
shifts to named employees in 7shifts, but for projection this is a fair proxy.

Outputs:
  - Per-shift cost
  - Per-day summary (cost, hours, vs target labor %)
  - Per-week summary (cost, hours, vs target labor %)
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, List, Tuple
from collections import defaultdict
from dataclasses import dataclass

import config
from scheduler import Shift
from sales_analyzer import HourlyProjection


@dataclass
class DailySummary:
    date: date
    day_of_week: str
    projected_sales: float
    foh_hours: float
    boh_hours: float
    foh_cost: float            # hourly only
    boh_cost: float            # hourly only
    manager_cost: float        # daily portion of manager salaries
    total_labor_cost: float    # everything

    @property
    def labor_pct(self) -> float:
        return self.total_labor_cost / self.projected_sales if self.projected_sales else 0.0


@dataclass
class WeeklySummary:
    week_start: date
    projected_sales: float
    total_hours: float
    foh_cost: float            # hourly only
    boh_cost: float            # hourly only
    foh_manager_cost: float    # weekly portion of FOH manager salary
    boh_manager_cost: float    # weekly portion of BOH manager salary
    total_labor_cost: float    # everything

    @property
    def labor_pct(self) -> float:
        return self.total_labor_cost / self.projected_sales if self.projected_sales else 0.0

    @property
    def labor_pct_hourly_only(self) -> float:
        """Labor % excluding manager salaries."""
        hrly = self.foh_cost + self.boh_cost
        return hrly / self.projected_sales if self.projected_sales else 0.0


def _shift_cost_with_daily_ot(paid_hours: float, wage: float) -> float:
    """Apply CA daily OT to a single shift's paid hours."""
    if paid_hours <= config.CA_OT_DAILY_THRESHOLD:
        return paid_hours * wage
    if paid_hours <= config.CA_DT_DAILY_THRESHOLD:
        reg = config.CA_OT_DAILY_THRESHOLD * wage
        ot = (paid_hours - config.CA_OT_DAILY_THRESHOLD) * wage * config.CA_OT_MULTIPLIER
        return reg + ot
    # double-time territory
    reg = config.CA_OT_DAILY_THRESHOLD * wage
    ot = (config.CA_DT_DAILY_THRESHOLD - config.CA_OT_DAILY_THRESHOLD) * wage * config.CA_OT_MULTIPLIER
    dt = (paid_hours - config.CA_DT_DAILY_THRESHOLD) * wage * config.CA_DT_MULTIPLIER
    return reg + ot + dt


def cost_shifts(
    shifts: List[Shift],
    avg_foh_wage: float,
    avg_boh_wage: float,
) -> Dict:
    """
    Cost every shift, applying daily CA OT rules.

    Weekly OT note: Without assignment of shifts to specific employees, we can't
    accurately compute weekly OT (which depends on per-employee weekly totals).
    A naive aggregation across all FOH shifts as if one person worked them would
    massively overstate cost. We instead assume the schedule will be staffed in
    a way that avoids weekly OT (multiple part-time employees, no single one
    crosses 40hrs), and flag any shift over 8 hrs/day for daily OT.

    If an individual employee actually does cross 40hrs/week when shifts are
    assigned in 7shifts, the labor cost will be slightly higher than projected.

    Returns:
        {
            "shift_costs":   {id(shift) -> cost},
            "totals_by_role":{role_bucket -> {hours, cost}},
            "ot_warnings":   list of strings describing any daily OT shifts
        }
    """
    shift_cost: Dict[int, float] = {}
    ot_warnings: List[str] = []

    for s in shifts:
        # Manager shifts are salary-funded, not hourly — zero hourly cost
        if s.role == "boh_manager" or s.role == "foh_manager":
            shift_cost[id(s)] = 0.0
            continue
        wage = avg_foh_wage if s.role.startswith("foh") else avg_boh_wage
        cost = _shift_cost_with_daily_ot(s.paid_hours, wage)
        shift_cost[id(s)] = cost
        if s.paid_hours > config.CA_OT_DAILY_THRESHOLD:
            ot_warnings.append(
                f"{s.date} {s.role}: {s.paid_hours:.1f}hr "
                f"(>{config.CA_OT_DAILY_THRESHOLD}hr triggers daily OT)"
            )

    # Aggregate
    totals_by_role: Dict[str, Dict[str, float]] = defaultdict(lambda: {"hours": 0.0, "cost": 0.0})
    for s in shifts:
        bucket = "foh" if s.role.startswith("foh") else "boh"
        totals_by_role[bucket]["hours"] += s.paid_hours
        totals_by_role[bucket]["cost"] += shift_cost[id(s)]

    return {
        "shift_costs": shift_cost,
        "totals_by_role": dict(totals_by_role),
        "ot_warnings": ot_warnings,
    }


def summarize(
    shifts: List[Shift],
    projection: Dict[Tuple[int, int], HourlyProjection],
    week_start: date,
    avg_foh_wage: float,
    avg_boh_wage: float,
) -> Tuple[List[DailySummary], WeeklySummary, List[str]]:
    """Produce daily and weekly labor% summaries + OT warnings."""
    costs = cost_shifts(shifts, avg_foh_wage, avg_boh_wage)
    shift_cost = costs["shift_costs"]
    ot_warnings = costs.get("ot_warnings", [])

    # Manager salaries spread per-day (52 weeks/yr, 7 days/wk)
    foh_mgr_per_day = config.FOH_MANAGER_ANNUAL_SALARY / 52.0 / 7.0
    boh_mgr_per_day = config.BOH_MANAGER_ANNUAL_SALARY / 52.0 / 7.0
    mgr_per_day = foh_mgr_per_day + boh_mgr_per_day  # always charged daily

    # Day-by-day
    daily: List[DailySummary] = []
    week_total_sales = 0.0
    for i in range(7):
        d = week_start + timedelta(days=i)
        dow = d.weekday()
        daily_sales = sum(
            p.total_sales for (pdow, _h), p in projection.items() if pdow == dow
        )
        week_total_sales += daily_sales

        # Exclude manager shifts from "hours" since they're not hourly
        def is_hourly(s):
            return s.role not in ("boh_manager", "foh_manager")
        foh_hours = sum(s.paid_hours for s in shifts
                        if s.date == d and s.role.startswith("foh") and is_hourly(s))
        boh_hours = sum(s.paid_hours for s in shifts
                        if s.date == d and s.role.startswith("boh") and is_hourly(s))
        foh_cost = sum(shift_cost[id(s)] for s in shifts
                       if s.date == d and s.role.startswith("foh"))
        boh_cost = sum(shift_cost[id(s)] for s in shifts
                       if s.date == d and s.role.startswith("boh"))

        daily.append(DailySummary(
            date=d,
            day_of_week=d.strftime("%a"),
            projected_sales=daily_sales,
            foh_hours=foh_hours,
            boh_hours=boh_hours,
            foh_cost=foh_cost,
            boh_cost=boh_cost,
            manager_cost=mgr_per_day,
            total_labor_cost=foh_cost + boh_cost + mgr_per_day,
        ))

    weekly = WeeklySummary(
        week_start=week_start,
        projected_sales=week_total_sales,
        total_hours=sum(d.foh_hours + d.boh_hours for d in daily),
        foh_cost=sum(d.foh_cost for d in daily),
        boh_cost=sum(d.boh_cost for d in daily),
        foh_manager_cost=foh_mgr_per_day * 7,
        boh_manager_cost=boh_mgr_per_day * 7,
        total_labor_cost=sum(d.total_labor_cost for d in daily),
    )
    return daily, weekly, ot_warnings
