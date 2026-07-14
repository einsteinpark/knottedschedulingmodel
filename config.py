"""
Cafe Knotted Scheduler - Configuration

All tunable parameters live here. Edit this file to adjust:
  - Operating hours
  - Labor target / COGS assumptions
  - Staffing thresholds (FOH/BOH)
  - Manual ignore dates
  - Toast / 7shifts credentials (via env vars)
"""

import os
import datetime as _dt


# ---------------------------------------------------------------------------
# Run-date anchor — makes the dashboard's week windows roll forward on their own
# ---------------------------------------------------------------------------
# Everything date-relative (current week, forward week, baseline cutoff) is
# derived from this. By default it's "today", so a scheduled Sunday-night run
# advances automatically with no edits. Pin it for testing/backfill with the
# env var DASHBOARD_AS_OF=YYYY-MM-DD.
def as_of_date() -> "_dt.date":
    override = os.environ.get("DASHBOARD_AS_OF")
    if override:
        try:
            return _dt.date.fromisoformat(override)
        except ValueError:
            pass
    return _dt.date.today()


def current_week_start(today: "_dt.date" = None) -> "_dt.date":
    """Monday of the week containing the as-of date."""
    d = today or as_of_date()
    return d - _dt.timedelta(days=d.weekday())
from datetime import time
from dataclasses import dataclass, field
from typing import List


# =============================================================================
# CREDENTIALS  (set as environment variables, never commit to source)
# =============================================================================
TOAST_CLIENT_ID         = os.getenv("TOAST_CLIENT_ID", "")
TOAST_CLIENT_SECRET     = os.getenv("TOAST_CLIENT_SECRET", "")
TOAST_RESTAURANT_GUID   = os.getenv("TOAST_RESTAURANT_GUID", "")
TOAST_API_HOSTNAME      = os.getenv("TOAST_API_HOSTNAME", "https://ws-api.toasttab.com")

SEVENSHIFTS_ACCESS_TOKEN = os.getenv("SEVENSHIFTS_ACCESS_TOKEN", "")
SEVENSHIFTS_COMPANY_ID   = os.getenv("SEVENSHIFTS_COMPANY_ID", "")
SEVENSHIFTS_LOCATION_ID  = os.getenv("SEVENSHIFTS_LOCATION_ID", "")  # optional filter


# =============================================================================
# FINANCIAL TARGETS
# =============================================================================
# Labor target is set directly (not derived from prime cost) per operator decision.
LABOR_TARGET_PCT        = 0.25   # 25% of net sales

# Reference figures (informational — no longer drive the target):
TARGET_PRIME_COST_PCT   = 0.60   # COGS + labor combined
ASSUMED_COGS_PCT        = 0.32   # plug-in: what we expect food/bev cost to be

# Tolerance: weekly result is acceptable within this band of target
LABOR_WEEK_TOLERANCE    = 0.02   # i.e. 23%–27% is OK

# ---- Baseline window: how many trailing weeks define "typical" ----
# 8 weeks (reverted from 4 once the Nate.Eatz viral lift subsided): a longer,
# more stable window for normal operating conditions. Per-DOW baseline uses the
# most recent BASELINE_WEEKS occurrences of each weekday, weighted toward the
# most recent (see BASELINE_DOW_WEIGHTS).
BASELINE_WEEKS = 8

# Per-DOW recency weights, most-recent occurrence first. The last TWO weeks are
# weighted most heavily (weight 3 each), the prior two moderately (2), and the
# oldest four at baseline (1). Last-2-weeks carry ~43% of each DOW baseline —
# recent-aware but far more stable than the viral-period 4-week [4,3,2,1].
# If fewer than len() occurrences exist, the leading weights are used/renormalized.
BASELINE_DOW_WEIGHTS = [3, 3, 2, 2, 1, 1, 1, 1]

# Baseline/recency use only COMPLETED data strictly before the current week.
# Derived from the run-date anchor so it rolls automatically.
BASELINE_HISTORY_CUTOFF = current_week_start().isoformat()

# ---- Current-week cool-off (re-tune of not-yet-completed days) ----
# Fraction of the viral recency LIFT retained on/after COOLOFF_FROM_DATE for the
# current week. 1.0 = full lift (no cool-off).
# Reset to 1.0: both completed days came in AT/ABOVE projection once finalized
# (Mon $4,767 = +0.8%, Tue $4,922 = +18.3% vs the new-model projection). The
# earlier 0.85 cool-off was based on a mid-day partial ($2,865) that the full
# day blew past — afternoon/evening business consistently recovers the lunchtime
# reads. No cool-off warranted; lower this only if a finalized day confirms a fade.
CURRENT_WEEK_COOLOFF = 1.0
COOLOFF_FROM_DATE = as_of_date().isoformat()  # today forward; rolls automatically

# ---- Automatic calibration from finalized days ----
# Each render, compare the model's projection for the current week's COMPLETED
# days (from actuals.csv) to what actually happened, and nudge the not-yet-
# completed days by that ratio. This is the genuinely automatic fine-tune: it
# self-corrects as you drop each finalized day into actuals.csv.
#   DAMPING: fraction of the observed miss applied (0.5 = move halfway, so one
#            wild day can't whipsaw the rest of the week).
#   MAX:     hard cap on the adjustment (±15%).
AUTO_CALIBRATE = True
AUTO_CALIBRATE_DAMPING = 0.5
AUTO_CALIBRATE_MAX = 0.15

# ---- Recency-weighting window for forward projection ----
# Compare RECENT N weeks vs PRIOR M weeks within the baseline to detect trend.
RECENCY_RECENT_WEEKS = 1   # most recent stretch
RECENCY_PRIOR_WEEKS  = 3   # what to compare against

# ---- Prior model snapshot (for old-vs-new comparison on Sheet 1) ----
# Update this when you snapshot a new baseline before making changes.
# Set to None to hide the comparison block entirely.
PRIOR_MODEL_TOTAL_LABOR = 7760.0     # previous saved state (May 21-Jun 17 4wk), $/wk
PRIOR_MODEL_TOTAL_PCT   = 25.2       # previous saved state, % of sales
PRIOR_MODEL_LABEL       = "previous 4-wk baseline"


# =============================================================================
# OPERATING HOURS  (per day-of-week: 0=Mon ... 6=Sun)
# Open/close = customer-facing. Opener arrives early, closer leaves late.
# =============================================================================
@dataclass(frozen=True)
class DayHours:
    open_time: time
    close_time: time
    opener_start: time     # opener clocks in before doors open
    closer_end: time       # closer (station-breakdown) clocks out after close

# Mon-Thu (0-3) and Sunday (6): 8a-8p
# Fri-Sat (4-5): 8a-10p
WEEKDAY_HOURS = DayHours(
    open_time=time(8, 0),
    close_time=time(20, 0),
    opener_start=time(6, 30),
    closer_end=time(21, 0),
)
WEEKEND_HOURS = DayHours(
    open_time=time(8, 0),
    close_time=time(22, 0),
    opener_start=time(6, 30),
    closer_end=time(23, 0),
)
HOURS_BY_DOW = {
    0: WEEKDAY_HOURS,  # Mon
    1: WEEKDAY_HOURS,  # Tue
    2: WEEKDAY_HOURS,  # Wed
    3: WEEKDAY_HOURS,  # Thu
    4: WEEKEND_HOURS,  # Fri
    5: WEEKEND_HOURS,  # Sat
    6: WEEKDAY_HOURS,  # Sun
}


# =============================================================================
# FOH (BARISTA/CASHIER) RULES
# Driven by donut/pastry/beverage sales
# =============================================================================
# How many people FOH needs at a given projected hourly sales level.
# Thresholds are AUTO-DERIVED at runtime from historical sales-per-labor-hour
# (see sales_analyzer.derive_foh_thresholds). These defaults are fallbacks
# only if there isn't enough historical labor data to derive them.
FALLBACK_FOH_THRESHOLDS = [
    (0.0,    1),   # < $80/hr  -> 1 person
    (80.0,   2),   # >= $80    -> 2 people
    (200.0,  3),   # >= $200   -> 3 people
    (350.0,  4),   # >= $350   -> 4 people
]

# Target sales-per-labor-hour for FOH (used when deriving thresholds).
# If your historical SPLH is $X, and labor target is 28%, then at avg FOH wage W:
#   required SPLH per person = W / 0.28
# We use the actual historical avg wage from 7shifts.
FOH_BREAK_AFTER_HOURS   = 5      # CA: 30-min unpaid meal break before 5th hour worked
FOH_BREAK_MINUTES       = 30
FOH_BREAK_TRIGGER_HOURS = 6      # only schedule a break if shift > this many hours

# Closing structure (both stay past close)
FOH_DISH_CLOSER_EXTRA_MIN     = 30   # dish closer leaves +30 min after close
FOH_STATION_CLOSER_EXTRA_MIN  = 60   # station closer leaves +60 min after close


# =============================================================================
# BOH (COOK) RULES
# Driven by savory food sales
# =============================================================================
BOH_SHIFT_START         = time(7, 0)    # cooks start at 7am
BOH_SHIFT_HOURS         = 8.5           # 7:00am-3:30pm (8.5hr w/ 30min break = 8 paid)
BOH_MIN_COOKS_WEEKDAY   = 1            # weekday floor (algorithm may raise)
BOH_MIN_COOKS_WEEKEND   = 2            # weekend floor
BOH_MAX_COOKS           = 3            # cap

# Daily savory-sales threshold above which we add a 2nd or 3rd cook.
# Auto-derived from historical data; these are fallbacks.
FALLBACK_BOH_DAILY_THRESHOLDS = [
    (0.0,    1),
    (800.0,  2),
    (1800.0, 3),
]


# =============================================================================
# MENU CATEGORIZATION
# Map Toast sales-category or item-tag names to our FOH/BOH buckets.
# Tune this to match your actual Toast menu setup.
# =============================================================================
FOH_CATEGORY_KEYWORDS = [
    "donut", "doughnut", "pastry", "pastries", "bakery",
    "beverage", "drink", "coffee", "espresso", "tea", "latte",
    "matcha", "refresher", "milk", "lemonade",
]
BOH_CATEGORY_KEYWORDS = [
    "savory", "sandwich", "toast", "egg", "breakfast",
    "lunch", "hot food", "kitchen",
]


# =============================================================================
# HISTORICAL DATA WINDOW
# =============================================================================
LOOKBACK_WEEKS          = 8       # how many weeks of history to pull
OUTLIER_ZSCORE_CUTOFF   = 2.5     # auto-flag days outside this z-score on daily $


# =============================================================================
# MANUAL IGNORE DATES
# YYYY-MM-DD strings. Days you know skew the data (events, holidays, closures).
# Add as needed.
# =============================================================================
MANUAL_IGNORE_DATES: List[str] = [
    # (Memorial Day was previously excluded; now included per operator request)
    "2026-07-04",  # Independence Day — holiday-depressed Saturday ($7,584, vs
                   # ~$8.8-9.8k normal Saturdays). Excluded so it doesn't drag
                   # down the recency-weighted Saturday baseline.
]


# =============================================================================
# MANAGEMENT (salaried, fixed weekly cost regardless of schedule)
# =============================================================================
# Full annual salaries (what the managers are actually paid):
FOH_MANAGER_ANNUAL_SALARY_FULL = 77500.0  # FOH manager - oversight, NOT in schedule
BOH_MANAGER_ANNUAL_SALARY_FULL = 78000.0  # BOH manager - covers 5 cook shifts/wk

# Salary allocation: both managers split their time between Cafe Knotted AD
# and WCC. Only 50% of their salary is allocated to THIS location's labor;
# the other 50% sits on WCC's books. This factor scales the salary cost shown
# in the dashboard. It does NOT change the schedule — the BOH manager still
# physically covers the same cook shifts here.
MANAGER_SALARY_ALLOCATION = 0.50  # 50% allocated to Knotted AD, 50% to WCC

# Effective (allocated) salaries used everywhere in the labor model:
FOH_MANAGER_ANNUAL_SALARY = FOH_MANAGER_ANNUAL_SALARY_FULL * MANAGER_SALARY_ALLOCATION
BOH_MANAGER_ANNUAL_SALARY = BOH_MANAGER_ANNUAL_SALARY_FULL * MANAGER_SALARY_ALLOCATION

# Manager pulls cook #1 duty on these days (0=Mon..6=Sun)
# Manager works Wed/Thu/Fri/Sat/Sun (off Mon/Tue)
BOH_MANAGER_DAYS_OF_WEEK = [2, 3, 4, 5, 6]  # Wed-Sun

# Always require 2 cooks per day (manager + 1 hourly, or 2 hourly when manager off)
BOH_COOKS_PER_DAY = 2


# =============================================================================
# OUTPUT
# =============================================================================
OUTPUT_DIR              = "/mnt/user-data/outputs"
GENERATE_FOR_WEEKS_AHEAD = 1      # how many weeks into the future to schedule


# Wages — set to your actual numbers (would normally come from 7shifts)
AVG_FOH_WAGE = 21.00
AVG_BOH_WAGE = 21.00
CA_OT_DAILY_THRESHOLD   = 8.0     # >8 hrs/day = 1.5x
CA_DT_DAILY_THRESHOLD   = 12.0    # >12 hrs/day = 2.0x
CA_OT_WEEKLY_THRESHOLD  = 40.0    # >40 hrs/week = 1.5x
CA_OT_MULTIPLIER        = 1.5
CA_DT_MULTIPLIER        = 2.0
