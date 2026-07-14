"""
Scheduler.

Turns the projected hourly demand profile into concrete shifts for a given week.

Output shift model:
    Shift = {
        "date":        date object,
        "role":        "foh_opener" | "foh_mid" | "foh_dish_closer" | "foh_station_closer" | "boh_cook",
        "start":       datetime,
        "end":         datetime,
        "break_start": datetime | None,
        "break_end":   datetime | None,
        "paid_hours":  float,    # hours minus unpaid break
    }

FOH logic per day:
  - One opener: starts at opener_start, stays as long as possible without being
    overwhelmed (= until projected demand requires a 2nd person OR until 8hrs+break)
  - Add mid-shift people: scheduled to cover hours where demand exceeds 1 person
  - Two closers, both stay past close:
      * dish_closer: leaves +30 min after close
      * station_closer: leaves +60 min after close
  - Break rule: shifts longer than FOH_BREAK_TRIGGER_HOURS get an unpaid 30-min
    break scheduled before the 5th hour worked

BOH logic per day:
  - All cooks start at BOH_SHIFT_START (7am)
  - All cooks work BOH_SHIFT_HOURS (8 hours) -> a clean 7a-3p
  - Headcount comes from required_boh_for_day()
  - 30-min unpaid break before 5th hour for each
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Tuple
from collections import defaultdict
from dataclasses import dataclass, field, asdict

import config
from sales_analyzer import (
    HourlyProjection,
    required_foh_for_hour,
    required_boh_for_day,
)

log = logging.getLogger(__name__)


@dataclass
class Shift:
    date: date
    role: str
    start: datetime
    end: datetime
    break_start: datetime | None = None
    break_end: datetime | None = None

    @property
    def total_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    @property
    def paid_hours(self) -> float:
        h = self.total_hours
        if self.break_start and self.break_end:
            h -= (self.break_end - self.break_start).total_seconds() / 3600.0
        return h

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "day_of_week": self.date.strftime("%a"),
            "role": self.role,
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
            "break_start": self.break_start.strftime("%H:%M") if self.break_start else "",
            "break_end": self.break_end.strftime("%H:%M") if self.break_end else "",
            "total_hours": round(self.total_hours, 2),
            "paid_hours": round(self.paid_hours, 2),
        }


def _combine(d: date, t: time) -> datetime:
    return datetime.combine(d, t)


def _add_break(shift: Shift) -> Shift:
    """Schedule a 30-min unpaid meal break before the 5th hour worked."""
    if shift.total_hours <= config.FOH_BREAK_TRIGGER_HOURS:
        return shift
    # Place the break right before the 5th hour starts
    break_start = shift.start + timedelta(hours=config.FOH_BREAK_AFTER_HOURS - 0.5)
    break_end = break_start + timedelta(minutes=config.FOH_BREAK_MINUTES)
    shift.break_start = break_start
    shift.break_end = break_end
    return shift


def schedule_day_foh(
    target_date: date,
    projection: Dict[Tuple[int, int], HourlyProjection],
    foh_thresholds: List[Tuple[float, int]],
) -> List[Shift]:
    """Build all FOH shifts for one day."""
    dow = target_date.weekday()
    hours_cfg = config.HOURS_BY_DOW[dow]
    opener_start = _combine(target_date, hours_cfg.opener_start)
    open_time = _combine(target_date, hours_cfg.open_time)
    close_time = _combine(target_date, hours_cfg.close_time)
    dish_end = close_time + timedelta(minutes=config.FOH_DISH_CLOSER_EXTRA_MIN)
    station_end = close_time + timedelta(minutes=config.FOH_STATION_CLOSER_EXTRA_MIN)

    # For each operating hour, what's the required FOH headcount?
    # Open hours = [open_time.hour ... close_time.hour - 1] (close hour is partial)
    required_by_hour: Dict[int, int] = {}
    for hr in range(opener_start.hour, close_time.hour):
        proj = projection.get((dow, hr))
        foh_sales = proj.foh_sales if proj else 0.0
        required_by_hour[hr] = required_foh_for_hour(foh_sales, foh_thresholds)
    # Pre-open hours: just the opener
    for hr in range(opener_start.hour, open_time.hour):
        required_by_hour[hr] = 1

    shifts: List[Shift] = []

    # ----- Two closers (staggered) -----
    # Dish closer comes in earlier (mid-shift) to take over from the opener;
    # they leave +30 min after close.
    # Station closer comes in later, stays +60 min after close.
    # Both shifts capped at ~8 hrs to avoid daily OT.

    # Dish closer: take over morning rush handoff, work through close
    dish_total_hours = 8.0  # target 8-hr shift
    dish_start_hr = int(dish_end.hour + dish_end.minute / 60.0 - dish_total_hours)
    dish_start_hr = max(open_time.hour, dish_start_hr)

    # Station closer: comes in later than dish closer, covers closing rush
    station_total_hours = 8.0
    station_start_hr = int(station_end.hour + station_end.minute / 60.0 - station_total_hours)
    # Don't start them at the exact same time as the dish closer
    station_start_hr = max(dish_start_hr + 2, station_start_hr)

    dish_closer = Shift(
        date=target_date,
        role="foh_dish_closer",
        start=_combine(target_date, time(dish_start_hr, 0)),
        end=dish_end,
    )
    _add_break(dish_closer)
    shifts.append(dish_closer)

    station_closer = Shift(
        date=target_date,
        role="foh_station_closer",
        start=_combine(target_date, time(station_start_hr, 0)),
        end=station_end,
    )
    _add_break(station_closer)
    shifts.append(station_closer)

    # Track headcount we've already committed for each hour from closers
    covered_by_hour: Dict[int, int] = defaultdict(int)
    for s in shifts:
        for hr in range(s.start.hour, s.end.hour + (1 if s.end.minute > 0 else 0)):
            if hr < 24:
                covered_by_hour[hr] += 1

    # ----- Opener -----
    # Opener starts at opener_start. They stay until either:
    #   (a) projected demand says we need a 2nd person AND that 2nd person
    #       is already on the floor from another shift, OR
    #   (b) they've hit 8 hrs (avoid CA daily OT), OR
    #   (c) the closers have already covered the slot
    opener_max_end_hr = opener_start.hour + 8
    opener_end_hr = opener_max_end_hr
    # Walk forward hour by hour and find the earliest point we can safely cut them
    for hr in range(opener_start.hour, opener_max_end_hr):
        need = required_by_hour.get(hr, 1)
        # opener counts as 1; how many others are on?
        others_on = covered_by_hour.get(hr, 0)
        # If others cover demand without the opener, they can leave at this hour
        if others_on >= need and hr >= open_time.hour + 4:
            opener_end_hr = hr
            break
    opener_end_hr = min(opener_end_hr, close_time.hour)

    opener = Shift(
        date=target_date,
        role="foh_opener",
        start=opener_start,
        end=_combine(target_date, time(opener_end_hr, 0)),
    )
    _add_break(opener)
    shifts.append(opener)

    # Update coverage with opener
    for hr in range(opener.start.hour, opener.end.hour):
        covered_by_hour[hr] += 1

    # ----- Mid shifts (fill demand gaps) -----
    # For every hour during open hours, if required > covered, we need to add
    # people. Group consecutive understaffed hours into shifts.
    mid_counter = 0
    for additional_person in range(1, 4):  # support up to a 4th person
        # Find spans where required exceeds current coverage
        understaffed_hours: List[int] = []
        for hr in range(open_time.hour, close_time.hour):
            need = required_by_hour.get(hr, 1)
            if covered_by_hour.get(hr, 0) < need:
                understaffed_hours.append(hr)
        if not understaffed_hours:
            break

        # Group into contiguous spans
        spans: List[Tuple[int, int]] = []
        current_start = understaffed_hours[0]
        prev = understaffed_hours[0]
        for hr in understaffed_hours[1:]:
            if hr == prev + 1:
                prev = hr
            else:
                spans.append((current_start, prev + 1))
                current_start = hr
                prev = hr
        spans.append((current_start, prev + 1))

        for span_start, span_end in spans:
            # Pad shift to at least 4 hours (CA reporting-time pay floor)
            duration = span_end - span_start
            if duration < 4:
                span_end = min(span_start + 4, close_time.hour)
                duration = span_end - span_start
            # Cap at 8 hrs to avoid daily OT
            if duration > 8:
                span_end = span_start + 8

            mid_counter += 1
            mid_shift = Shift(
                date=target_date,
                role=f"foh_mid_{mid_counter}",
                start=_combine(target_date, time(span_start, 0)),
                end=_combine(target_date, time(min(span_end, 23), 0)),
            )
            _add_break(mid_shift)
            shifts.append(mid_shift)
            for hr in range(span_start, span_end):
                covered_by_hour[hr] += 1

    return shifts


def schedule_day_boh(
    target_date: date,
    projection: Dict[Tuple[int, int], HourlyProjection],
    boh_thresholds: List[Tuple[float, int]],
) -> List[Shift]:
    """
    Build all BOH shifts for one day.

    New manager-aware logic:
      - Always BOH_COOKS_PER_DAY cooks per day (default 2)
      - On days the BOH manager works (BOH_MANAGER_DAYS_OF_WEEK), cook #1 is
        the manager (role='boh_manager') and the remaining slots are hourly
      - Manager shifts are produced for tracking, but their hours are NOT
        billed against the hourly wage in cost calculations (handled by the
        labor_calculator using the salary instead)
    """
    dow = target_date.weekday()
    manager_works_today = dow in config.BOH_MANAGER_DAYS_OF_WEEK
    cooks_needed = config.BOH_COOKS_PER_DAY

    shifts: List[Shift] = []
    for i in range(cooks_needed):
        start = _combine(target_date, config.BOH_SHIFT_START)
        end = start + timedelta(hours=config.BOH_SHIFT_HOURS)
        # First slot becomes the manager when they're on
        if i == 0 and manager_works_today:
            role = "boh_manager"
        else:
            role = f"boh_cook_{i+1 if not manager_works_today else i}"
        shift = Shift(
            date=target_date,
            role=role,
            start=start,
            end=end,
        )
        _add_break(shift)
        shifts.append(shift)
    return shifts


def schedule_week(
    week_start: date,
    projection: Dict[Tuple[int, int], HourlyProjection],
    foh_thresholds: List[Tuple[float, int]],
    boh_thresholds: List[Tuple[float, int]],
) -> List[Shift]:
    """Schedule a full week starting Monday `week_start`."""
    all_shifts: List[Shift] = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        all_shifts.extend(schedule_day_foh(d, projection, foh_thresholds))
        all_shifts.extend(schedule_day_boh(d, projection, boh_thresholds))
    return all_shifts
