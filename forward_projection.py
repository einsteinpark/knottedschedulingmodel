"""
Forward projection for the upcoming week (May 25-31, 2026).

Methodology:
  1. Start with historical hourly orders/sales by DOW
  2. Apply recency weighting (recent 2 weeks weighted 2x vs prior 6 weeks)
     - Practically: scale historical baseline by ratio of recent-vs-prior averages
  3. Apply per-day calendar/weather multipliers
  4. Output adjusted hourly orders/sales for Mon 5/25 - Sun 5/31

The multipliers are estimates based on directional knowledge — there is no
empirical baseline in Knotted's data for "rainy day" or "Memorial Day."
Treat as educated forecasts, not point predictions.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import csv

from csv_analyzer import build_projection_from_csvs, TastFiles, _parse_yyyymmdd, _to_float


# -----------------------------------------------------------------------------
# Projected weeks
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Projected weeks — all derived from the run-date anchor (config.as_of_date),
# so a scheduled run rolls them forward automatically. Pin with DASHBOARD_AS_OF.
# -----------------------------------------------------------------------------
import config as _config

# Current week = week containing the as-of date (Mon-Sun)
CURRENT_WEEK_START = _config.current_week_start()
CURRENT_WEEK_DATES = [CURRENT_WEEK_START + timedelta(days=i) for i in range(7)]

# Forward week = the next week
PROJECTED_WEEK_START = CURRENT_WEEK_START + timedelta(days=7)
PROJECTED_DATES = [PROJECTED_WEEK_START + timedelta(days=i) for i in range(7)]

# Last completed week (trailing) — used for recency/lift "last week" windows
LAST_WEEK_START = CURRENT_WEEK_START - timedelta(days=7)
LAST_WEEK_END = CURRENT_WEEK_START - timedelta(days=1)


@dataclass
class WeekConfig:
    """Configuration for projecting one week.

    Allows us to render two parallel projections from the same pipeline:
      - Current week (Jun 22-28): FULL Nate.Eatz lift (viral effect still fresh)
      - Forward week (Jun 29-Jul 5): HALF the Nate.Eatz lift (operator-instructed
        wane assumption) + July 4 holiday + Thu SoFi Round of 32
    """
    week_dates: List[date]
    lift_multiplier: float  # 0.5 = halved Thu-Sun lift; 1.0 = full lift
    label: str              # "current week" / "forward week" — used in dashboards
    adjustments_fn: callable      # () -> Dict[date, DayAdjustment]
    factor_blurbs: List[str]      # explanation strings for driver factors panel
    # Extra FOH shifts merged on top of PROPOSED_SCHEDULE for THIS week only,
    # keyed by day-of-week. Used for current-week operator additions.
    extra_foh_shifts: Dict[int, list] = field(default_factory=dict)
    # Per-DOW base-roster replacements for THIS week only (current-week retimes).
    base_foh_overrides: Dict[int, list] = field(default_factory=dict)
    # Per-DOW sales/orders multiplier applied to this week's projection, keyed by
    # weekday (0=Mon..6=Sun). Used to shape the lift by day of week — e.g. weekday
    # cooldown + weekend hold — on top of the recency baseline.
    dow_adjust: Dict[int, float] = field(default_factory=dict)


# -----------------------------------------------------------------------------
# Day adjustment factors
#
# Each factor multiplies the historical hourly baseline for that DOW.
# 1.0 = no change. <1.0 = expect less traffic. >1.0 = expect more.
#
# These are encoded as PER-HOUR multipliers (some events affect mornings only,
# some affect the whole day).
# -----------------------------------------------------------------------------

@dataclass
class DayAdjustment:
    """Per-day multipliers + reasons. Per-hour overrides if any.

    Each factor can apply a different multiplier to SALES vs ORDERS.
    This matters when a lift affects ticket size (AOV) more than transaction
    count — e.g. a viral post that drives bigger orders but not necessarily
    more transactions.
    """
    date: date
    factors: List[Tuple[str, float, float, str]] = field(default_factory=list)
    # factors list of (factor_name, sales_mult, orders_mult, hour_scope)
    # hour_scope: "all", "am" (open-11am), "midday" (11am-2pm), "pm" (2pm-close)

    def _matches_scope(self, hour: int, scope: str) -> bool:
        if scope == "all": return True
        if scope == "am": return hour < 11
        if scope == "midday": return 11 <= hour < 14
        if scope == "pm": return hour >= 14
        return False

    def sales_multiplier_for_hour(self, hour: int) -> float:
        """Combined SALES multiplier for a specific hour."""
        mult = 1.0
        for _, sales_mult, _, scope in self.factors:
            if self._matches_scope(hour, scope):
                mult *= sales_mult
        return mult

    def orders_multiplier_for_hour(self, hour: int) -> float:
        """Combined ORDERS multiplier for a specific hour."""
        mult = 1.0
        for _, _, orders_mult, scope in self.factors:
            if self._matches_scope(hour, scope):
                mult *= orders_mult
        return mult

    # Backwards-compat: callers using multiplier_for_hour get the sales multiplier
    def multiplier_for_hour(self, hour: int) -> float:
        return self.sales_multiplier_for_hour(hour)

    def has_adjustment(self) -> bool:
        return any(f[1] != 1.0 or f[2] != 1.0 for f in self.factors)

    def summary(self) -> str:
        """One-line summary like 'Memorial Day (-15%), sunny (+0%)'."""
        parts = []
        for name, sales_mult, orders_mult, _ in self.factors:
            if sales_mult == 1.0 and orders_mult == 1.0:
                continue
            # Show sales mult by default; if orders mult differs noticeably, show both
            sales_pct = (sales_mult - 1.0) * 100
            orders_pct = (orders_mult - 1.0) * 100
            if abs(sales_pct - orders_pct) > 5:
                parts.append(f"{name} (sales {sales_pct:+.0f}%, orders {orders_pct:+.0f}%)")
            else:
                parts.append(f"{name} ({sales_pct:+.0f}%)")
        return ", ".join(parts) if parts else "no adjustments"


# Item prices (used to convert BS/CW qty to revenue contribution)
NATE_BS_PRICE = 12.50
NATE_CW_PRICE = 14.50
# Hours where BS/CW actually sell in volume
NATE_BSCW_HOURS = set(range(8, 14))


# ---------------------------------------------------------------------------
# Calendar event registry — date-keyed adjustments that roll on their own.
#
# Each known event is keyed by its real calendar date. The builder applies only
# the events that fall inside the week being rendered, so when the week windows
# advance, past events drop off and the new week is neutral until you add its
# events here. Add a new event by appending a {date: {...}} entry — one edit,
# no code changes. (A future enhancement can populate this from a live events
# API; for now it's the structured place where you or I drop known events.)
#
# Factor tuple: (label, sales_multiplier, orders_multiplier, daypart)
#   daypart in {"am","midday","pm","all"}.
# ---------------------------------------------------------------------------
LAUSD_SUMMER_BREAK = (date(2026, 6, 11), date(2026, 8, 14))  # auto-expires in fall

CALENDAR_EVENTS: Dict[date, Dict] = {
    date(2026, 6, 24): {
        "factors": [
            ("FIFA Mexico match (in Mexico, 8pm PT) - light fan watch-party draw",
             0.92, 0.92, "pm"),
        ],
        "blurb": "Wed Jun 24 — Czechia vs Mexico in MEXICO CITY (8pm PT). Not at SoFi "
                 "but the local Mexican fan base will tune in. Per Jun 11 Mexico match "
                 "precedent (-11%), applied -8% PM only — much lighter than USA SoFi days.",
    },
    date(2026, 6, 25): {
        "factors": [
            ("FIFA USA at SoFi (7pm PT) - pre-game drift, fans heading west",
             0.92, 0.92, "midday"),
            ("FIFA USA at SoFi (7pm PT) - 70K to Inglewood + watch parties",
             0.78, 0.82, "pm"),
        ],
        "blurb": "FIFA World Cup — Thu Jun 25 USA match at SoFi (Türkiye vs USA, 7pm PT). "
                 "USA at SoFi means heavy watch-party / stadium pull. Per Jun 12 USA-Paraguay "
                 "precedent (-27% sales), applied -8% midday (pre-game drift) and -22% PM "
                 "(peak watch + stadium ingress).",
    },
    date(2026, 6, 28): {
        "factors": [
            ("FIFA Round of 32 at SoFi (12pm PT) - pre-game departures soften AM",
             0.92, 0.92, "am"),
            ("FIFA Round of 32 at SoFi (12pm PT) - kickoff overlaps brunch peak",
             0.90, 0.90, "midday"),
        ],
        "blurb": "Sun Jun 28 — Round of 32 at SoFi (12pm PT, neutral teams). Daytime "
                 "kickoff overlaps brunch peak; applied -8% AM and -10% midday.",
    },
    date(2026, 7, 1): {
        "factors": [
            ("FIFA USA match (Santa Clara, not LA) - watch-party draw", 0.95, 0.95, "pm"),
        ],
        "blurb": "Wed Jul 1 — USA Round of 32 in Santa Clara (5pm PT), not LA. Light "
                 "-5% PM watch-party effect.",
    },
    date(2026, 7, 2): {
        "factors": [
            ("FIFA Round of 32 at SoFi (12pm PT) - 70K to Inglewood, peak overlaps midday",
             0.78, 0.82, "midday"),
            ("FIFA SoFi match - post-match crowd-pull continues PM", 0.92, 0.92, "pm"),
        ],
        "blurb": "Thu Jul 2 — Round of 32 at SoFi (12pm PT, ~70K to Inglewood). Daytime "
                 "match overlaps cafe midday peak; per SoFi precedent applied -22% midday / "
                 "-8% PM.",
    },
    date(2026, 7, 3): {
        "factors": [
            ("July 3 - early holiday departures soften PM", 0.95, 0.95, "pm"),
        ],
        "blurb": "Fri Jul 3 — early July-4-weekend departures soften PM (-5%).",
    },
    date(2026, 7, 4): {
        "factors": [
            # Operator view (Jun 30): the viral weekend destination crowd offsets
            # MOST of the usual July-4 downtown exodus. Applied a mild residual
            # -3.5% instead of the pre-lift -15/-20/-25% holiday softness.
            ("July 4 - mild residual holiday dip (viral weekend crowd offsets most)",
             1.00, 1.00, "am"),
            ("July 4 - mild midday holiday dip", 0.95, 0.95, "midday"),
            ("July 4 - mild PM holiday dip", 0.95, 0.95, "pm"),
        ],
        "blurb": "Sat Jul 4 — Independence Day, but operator view is that the viral "
                 "weekend destination crowd offsets most of the usual July-4 downtown "
                 "exodus. Applied a MILD residual dip (-5% midday/PM, AM held) vs the "
                 "-15/-20/-25% used pre-lift. Lands ~$9.3k. RISK: if downtown empties "
                 "as it historically does, Sat could come in ~$7.6k instead — auto-cal "
                 "will correct once the day lands, but staff with that downside in mind.",
    },
    date(2026, 7, 5): {
        "factors": [
            ("July 5 - viral weekend holds; minimal holiday tail", 0.99, 0.99, "all"),
        ],
        "blurb": "Sun Jul 5 — operator view: viral weekend lift still holds. Holiday "
                 "tail softness essentially removed (mild -1%). Lands ~$9.0k.",
    },
}


def _lausd_active(d: date) -> bool:
    start, end = LAUSD_SUMMER_BREAK
    return start <= d <= end and d.weekday() < 5


def _build_adjustments(week_dates: List[date]) -> Dict[date, DayAdjustment]:
    """Neutral per-day adjustments, plus LAUSD weekday shape (while on summer
    break) and any CALENDAR_EVENTS that fall inside this week. Rolls on its own:
    a week with no known events simply gets the neutral + LAUSD baseline."""
    adjustments: Dict[date, DayAdjustment] = {d: DayAdjustment(date=d) for d in week_dates}
    for d in week_dates:
        if _lausd_active(d):
            adjustments[d].factors.append(
                ("LAUSD summer break - AM school-run thinner", 0.97, 0.97, "am"))
            adjustments[d].factors.append(
                ("LAUSD summer break - midday leisure lift", 1.03, 1.03, "midday"))
        ev = CALENDAR_EVENTS.get(d)
        if ev:
            for f in ev["factors"]:
                adjustments[d].factors.append(f)
    return adjustments


def build_current_week_adjustments() -> Dict[date, DayAdjustment]:
    return _build_adjustments(CURRENT_WEEK_DATES)


def build_week_adjustments() -> Dict[date, DayAdjustment]:
    return _build_adjustments(PROJECTED_DATES)


def _blurbs_for_week(week_dates: List[date], lift_intro: str) -> List[str]:
    """Driver-factor bullets generated from what's actually applied this week,
    so the prose stays in sync as the weeks roll."""
    blurbs = [lift_intro]
    for d in week_dates:
        ev = CALENDAR_EVENTS.get(d)
        if ev and ev.get("blurb"):
            blurbs.append(ev["blurb"])
    if any(_lausd_active(d) for d in week_dates):
        blurbs.append(
            "LAUSD on summer break — modest weekday shape shift Mon-Fri: -3% AM "
            "(school-run thinner), +3% midday (leisure lift).")
    blurbs.append(
        "Weather (checked Jul 14): typical warm, dry LA July through the forward "
        "week — highs low-to-high 80s this week (Wed ~90\u00b0 the warmest), upper "
        "80s and dry next week, ~0 chance of rain throughout. Nothing anomalous "
        "(no rain event, no >95\u00b0 heat wave, no unusual cool spell), and the "
        "baseline is already built from June-July data under these same "
        "conditions, so no weather multiplier is applied. A rain day or an "
        "extreme-heat day would be added as a day adjustment if it appeared.")
    return blurbs


# -----------------------------------------------------------------------------
# WeekConfig instances — the two parallel projections
# -----------------------------------------------------------------------------

_CURRENT_LIFT_INTRO = (
    "Baseline model (viral lift subsided). The Nate.Eatz social spike has faded, "
    "so projections are now the plain per-day-of-week baseline: the most recent "
    "8 occurrences of each weekday, weighted toward the last 2 weeks (weight 3 "
    "each vs 1 for the oldest four). No separate viral lift is applied. As new "
    "actuals land they fold into this window, so the baseline tracks the current "
    "normal level on its own."
)
_FORWARD_LIFT_INTRO = (
    "Baseline model (viral lift subsided). Same 8-week weighted day-of-week "
    "baseline (last 2 weeks heaviest), no viral lift and no weekend shaping — the "
    "recent-weighted baseline carries the day-of-week levels directly. This is the "
    "normal, pre-viral projection method."
)

CURRENT_WEEK_FACTOR_BLURBS = _blurbs_for_week(CURRENT_WEEK_DATES, _CURRENT_LIFT_INTRO)
FORWARD_WEEK_FACTOR_BLURBS = _blurbs_for_week(PROJECTED_DATES, _FORWARD_LIFT_INTRO)

from shift_optimizer import (
    CURRENT_WEEK_EXTRA_SHIFTS, CURRENT_WEEK_SHIFT_OVERRIDES, FORWARD_WEEK_EXTRA_SHIFTS,
)

CURRENT_WEEK_CONFIG = WeekConfig(
    week_dates=CURRENT_WEEK_DATES,
    lift_multiplier=0.0,  # Viral lift subsided -> projection is the 8-week baseline
    label="current week",
    adjustments_fn=build_current_week_adjustments,
    factor_blurbs=CURRENT_WEEK_FACTOR_BLURBS,
    extra_foh_shifts=CURRENT_WEEK_EXTRA_SHIFTS,
    base_foh_overrides=CURRENT_WEEK_SHIFT_OVERRIDES,
)

FORWARD_WEEK_CONFIG = WeekConfig(
    week_dates=PROJECTED_DATES,
    lift_multiplier=0.0,  # Viral lift subsided -> projection is the 8-week baseline
    label="forward week",
    adjustments_fn=build_week_adjustments,
    factor_blurbs=FORWARD_WEEK_FACTOR_BLURBS,
    extra_foh_shifts=FORWARD_WEEK_EXTRA_SHIFTS,
    # No per-DOW shaping: post-viral, the 8-week weighted baseline (last 2 weeks
    # heaviest) carries the day-of-week levels on its own.
    dow_adjust={},
)


# -----------------------------------------------------------------------------
# Recency weighting
# -----------------------------------------------------------------------------

def compute_recency_factor(uploads_dir: Path, lift_multiplier: float = 0.5) -> Dict[int, float]:
    """
    Uniform-lift recency: per operator instruction, apply lift_multiplier × last
    week's Thu-Sun average lift uniformly across ALL 7 days of the projected week.

    lift_multiplier: 0.5 = halved (forward week, wane assumed); 1.0 = full (current week).

    Rationale: last Thu-Sun saw massive Nate.Eatz lifts (+41% to +88% vs the
    8-week baseline). Mon-Wed last week pre-dated the post, so their data
    isn't representative. We want to:
      (a) Apply SOME lift to Mon-Wed too, since they should also be elevated
          going forward (the viral effect doesn't skip Mon-Wed)
      (b) Moderate the Thu-Sun lift, since viral effects fade over time —
          half of last week's lift is the operator's expectation

    Computation:
      1. Compute per-DOW recency (last week / 8wk baseline) for diagnostics
      2. Average the Thu/Fri/Sat/Sun lifts (the post-Nate.Eatz days)
      3. Halve that average lift
      4. Apply uniformly to all 7 days

    Honors config.MANUAL_IGNORE_DATES — manually excluded days are dropped.
    """
    import config

    ignored: set = set()
    for s in getattr(config, "MANUAL_IGNORE_DATES", []):
        try:
            ignored.add(date.fromisoformat(s))
        except (ValueError, TypeError):
            continue

    sales_path = uploads_dir / "Sales_by_day.csv"

    daily_by_date: Dict[date, float] = {}
    _cutoff = getattr(config, "BASELINE_HISTORY_CUTOFF", None)
    _cutoff_d = date.fromisoformat(_cutoff) if _cutoff else None
    with sales_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                d = _parse_yyyymmdd(row["yyyyMMdd"])
                if d in ignored:
                    continue
                if _cutoff_d and d >= _cutoff_d:
                    continue  # only completed pre-current-week history
                daily_by_date[d] = _to_float(row["Net sales"])
            except (KeyError, ValueError):
                continue

    if not daily_by_date:
        return {dow: 1.0 for dow in range(7)}

    # Step 1: per-DOW last-week vs the WEIGHTED 4-week baseline. Using the same
    # weighted baseline as build_hourly_metrics keeps the lift consistent — as
    # recent actuals get folded into the baseline (most recent week weighted
    # heaviest), the computed lift naturally compresses toward zero, which is
    # the self-correcting behavior we want for projected-vs-actual accuracy.
    from csv_analyzer import weighted_dow_baseline
    weighted_baseline_s, _ = weighted_dow_baseline(uploads_dir)

    max_date = max(daily_by_date.keys())
    recent_cutoff = max_date - timedelta(days=6)

    last_week_by_dow: Dict[int, List[float]] = {}
    for d, sales in daily_by_date.items():
        if d >= recent_cutoff:
            last_week_by_dow.setdefault(d.weekday(), []).append(sales)

    # Compute per-DOW raw recency ratio (last week / weighted baseline)
    raw_ratios: Dict[int, float] = {}
    for dow in range(7):
        last = last_week_by_dow.get(dow, [])
        baseline_avg = weighted_baseline_s.get(dow, 0.0)
        if not last or baseline_avg <= 0:
            raw_ratios[dow] = 1.0
            continue
        last_avg = sum(last) / len(last)
        raw_ratios[dow] = last_avg / baseline_avg

    # Step 2: average lift across Thu(3), Fri(4), Sat(5), Sun(6)
    # Lift = ratio - 1 (so +88% lift = ratio of 1.88)
    thu_sun_lifts = [raw_ratios[dow] - 1.0 for dow in (3, 4, 5, 6)]
    avg_thu_sun_lift = sum(thu_sun_lifts) / len(thu_sun_lifts)

    # Step 3 & 4: halve it, apply uniformly to all 7 days
    uniform_lift = avg_thu_sun_lift * lift_multiplier
    uniform_factor = 1.0 + uniform_lift

    return {dow: uniform_factor for dow in range(7)}


# Cache item lifts (tiny dataset; avoid recomputing per hour during render)
_BSCW_LIFT_CACHE: Dict = {}

def bscw_item_lifts(uploads_dir: Path, lift_multiplier: float = 1.0) -> Tuple[float, float]:
    """
    Per-ITEM recency lift for Breakfast Sandwich and Caesar Wrap, computed the
    same way as the revenue lift (avg Thu-Sun of last-week vs the 4-week
    recency-weighted baseline), but per item. The breakfast sandwich is the
    actual viral driver and surged far more than overall revenue, so blending
    it into the single revenue lift under-projected it. Returns (bs_factor,
    cw_factor) = 1 + item_lift * lift_multiplier.
    """
    key = (str(uploads_dir), round(lift_multiplier, 4))
    if key in _BSCW_LIFT_CACHE:
        return _BSCW_LIFT_CACHE[key]

    from collections import defaultdict as _dd
    from item_hourly import item_daily_by_date
    import config

    cutoff = getattr(config, "BASELINE_HISTORY_CUTOFF", None)
    cutoff_d = date.fromisoformat(cutoff) if cutoff else None
    data = item_daily_by_date(uploads_dir, cutoff=cutoff_d)

    n_weeks = int(getattr(config, "BASELINE_WEEKS", 4))
    weights = list(getattr(config, "BASELINE_DOW_WEIGHTS", [4, 3, 2, 1]))

    def _factor(item_name: str) -> float:
        daily = {d: q for (d, it), q in data.items() if it == item_name}
        if not daily:
            return 1.0
        by_dow = _dd(list)
        for d, v in daily.items():
            by_dow[d.weekday()].append((d, v))
        wb: Dict[int, float] = {}
        for dow in range(7):
            occ = [v for _, v in sorted(by_dow[dow], reverse=True)[:n_weeks]]
            w = weights[:len(occ)]
            wb[dow] = sum(a * b for a, b in zip(occ, w)) / sum(w) if occ else 0.0
        max_d = max(daily)
        recent_cutoff = max_d - timedelta(days=6)
        last_wk = {d.weekday(): v for d, v in daily.items() if d >= recent_cutoff}
        ratios = [last_wk[dow] / wb[dow] for dow in (3, 4, 5, 6)
                  if wb.get(dow) and dow in last_wk]
        if not ratios:
            return 1.0
        item_lift = sum(r - 1.0 for r in ratios) / len(ratios)
        return 1.0 + item_lift * lift_multiplier

    result = (_factor("Breakfast Sandwich"), _factor("Chicken Caesar Wrap"))
    _BSCW_LIFT_CACHE[key] = result
    return result


# -----------------------------------------------------------------------------
# Weekend BS/CW composition (operator-set, Jul 3)
# -----------------------------------------------------------------------------
# The viral crowd shows up as breakfast sandwiches and Caesar wraps, concentrated
# in the morning/midday selling hours. Operator view: Fri/Sat/Sun BS/CW should be
# in line with the Nate.Eatz weeks, higher than the cooled all-data baseline.
# We scale the projected BS/CW hourly shape to hit these daily counts, then shift
# the incremental item revenue INTO the selling hours and OUT of the other hours
# so the day total (already targeted separately) is unchanged. Keyed by weekday
# (4=Fri, 5=Sat, 6=Sun); (BS_target, CW_target).
# Cleared once the viral lift subsided (BS/CW back to their natural baseline);
# re-populate a weekday if you want to pin its BS/CW to a specific count again.
WEEKEND_BSCW_TARGETS: Dict[int, Tuple[float, float]] = {}


def weekend_bscw_scale(uploads_dir: Path, dow: int,
                       bs_lift: float, cw_lift: float) -> Tuple[float, float]:
    """Per-day (bs_scale, cw_scale) that brings the projected BS/CW day total to
    the weekend target. (1.0, 1.0) for days without a target or with no data."""
    if dow not in WEEKEND_BSCW_TARGETS:
        return 1.0, 1.0
    from item_hourly import load_item_hourly
    avg = load_item_hourly(uploads_dir)
    bs_day = sum(avg.get((dow, h, "Breakfast Sandwich"), 0.0) for h in range(6, 24)) * bs_lift
    cw_day = sum(avg.get((dow, h, "Chicken Caesar Wrap"), 0.0) for h in range(6, 24)) * cw_lift
    bs_t, cw_t = WEEKEND_BSCW_TARGETS[dow]
    bs_scale = (bs_t / bs_day) if bs_day > 0 else 1.0
    cw_scale = (cw_t / cw_day) if cw_day > 0 else 1.0
    return bs_scale, cw_scale


def _reshape_weekend_bscw(projection: Dict, uploads_dir: Path,
                          week_dates: List[date], lift_multiplier: float,
                          skip_dates: set = frozenset()) -> None:
    """For each target weekend day, bump BS/CW to target and move the incremental
    item revenue into the selling hours, out of the other hours (day total held).
    Mutates projection in place."""
    from item_hourly import load_item_hourly
    bs_lift, cw_lift = bscw_item_lifts(uploads_dir, lift_multiplier)
    avg = None
    for d in week_dates:
        dow = d.weekday()
        if dow not in WEEKEND_BSCW_TARGETS or d in skip_dates:
            continue
        bs_scale, cw_scale = weekend_bscw_scale(uploads_dir, dow, bs_lift, cw_lift)
        if abs(bs_scale - 1.0) < 1e-9 and abs(cw_scale - 1.0) < 1e-9:
            continue
        if avg is None:
            avg = load_item_hourly(uploads_dir)
        delta_by_h = {}
        for h in range(6, 24):
            bs_h = avg.get((dow, h, "Breakfast Sandwich"), 0.0) * bs_lift
            cw_h = avg.get((dow, h, "Chicken Caesar Wrap"), 0.0) * cw_lift
            delta_by_h[h] = (bs_h * (bs_scale - 1.0) * NATE_BS_PRICE
                             + cw_h * (cw_scale - 1.0) * NATE_CW_PRICE)
        total_delta = sum(v for v in delta_by_h.values() if v > 0)
        if total_delta <= 0:
            continue
        selling = {h for h, v in delta_by_h.items() if v > 0}
        for h in selling:
            if (d, h) in projection:
                projection[(d, h)].adjusted_sales += delta_by_h[h]
        other = {h: projection[(d, h)].adjusted_sales
                 for h in range(6, 24)
                 if h not in selling and (d, h) in projection}
        other_sum = sum(v for v in other.values() if v > 0)
        if other_sum > 0:
            for h, val in other.items():
                if val > 0:
                    projection[(d, h)].adjusted_sales -= total_delta * (val / other_sum)


# -----------------------------------------------------------------------------
# Main projection
# -----------------------------------------------------------------------------

@dataclass
class HourProjection:
    date: date
    hour: int
    historical_orders: float    # baseline from 8-week historical
    historical_sales: float
    recency_factor: float       # multiplier from recent 2 weeks trend
    day_factor: float           # multiplier from this specific day's events (SALES — kept for display)
    day_factor_orders: float = 1.0  # separate ORDERS multiplier (drives staffing)
    adjusted_orders: float = 0.0    # final = historical * recency * day_orders
    adjusted_sales: float = 0.0     # final = historical * recency * day_sales


def projected_bscw_qty(
    dow: int, hour: int,
    bs_last_week: float, cw_last_week: float,
    bs_8wk_avg: float = None, cw_8wk_avg: float = None,
    recency_lift: float = 1.0,
) -> Tuple[float, float]:
    """
    Compute the PROJECTED Breakfast Sandwich and Caesar Wrap quantities for a
    given (dow, hour).

    Applies the uniform recency lift to the 8-week DOW baseline. This matches
    the methodology used for overall revenue projection (uniform-lift recency
    across all 7 days). Using the 8-week baseline as the anchor avoids the
    Mon-Wed under-projection problem (where last week's data pre-dated the
    viral post).

    For backwards compatibility, last-week values are accepted but no longer
    used when 8wk and lift are provided.
    """
    if bs_8wk_avg is None:
        bs_8wk_avg = bs_last_week
    if cw_8wk_avg is None:
        cw_8wk_avg = cw_last_week
    return bs_8wk_avg * recency_lift, cw_8wk_avg * recency_lift


def build_forward_projection(
    uploads_dir: Path,
    week_config: Optional[WeekConfig] = None,
) -> Tuple[
    Dict[Tuple[date, int], HourProjection],
    Dict[int, float],
    Dict[date, DayAdjustment],
]:
    """
    Build the per-hour projection for a week.

    Args:
      week_config: which week to project. Defaults to FORWARD_WEEK_CONFIG
        (Jun 29 - Jul 5, halved lift). Pass CURRENT_WEEK_CONFIG for this week.
    """
    if week_config is None:
        week_config = FORWARD_WEEK_CONFIG

    from weekly_dashboard import build_hourly_metrics
    from item_hourly import load_item_hourly
    historical_orders, historical_sales = build_hourly_metrics(uploads_dir)

    item_hourly_lastweek = load_item_hourly(
        uploads_dir,
        date_from=LAST_WEEK_START,
        date_to=LAST_WEEK_END,
    )
    item_hourly_8wk = load_item_hourly(uploads_dir)

    recency_factors = compute_recency_factor(uploads_dir, week_config.lift_multiplier)
    day_adjustments = week_config.adjustments_fn()

    # Current-week cool-off: retain only CURRENT_WEEK_COOLOFF of the viral lift
    # on/after COOLOFF_FROM_DATE (re-tune of the not-yet-completed days from how
    # the realized days are tracking). Applies to the current week only.
    import config as _cfg
    cooloff = float(getattr(_cfg, "CURRENT_WEEK_COOLOFF", 1.0))
    cooloff_from = getattr(_cfg, "COOLOFF_FROM_DATE", None)
    cooloff_from_d = date.fromisoformat(cooloff_from) if cooloff_from else None
    is_current = getattr(week_config, "label", "") == "current week"

    projection: Dict[Tuple[date, int], HourProjection] = {}
    for d in week_config.week_dates:
        dow = d.weekday()
        recency = recency_factors.get(dow, 1.0)
        # Cool off the lift portion for not-yet-completed current-week days
        if (is_current and cooloff != 1.0 and cooloff_from_d and d >= cooloff_from_d):
            recency = 1.0 + (recency - 1.0) * cooloff
        adj = day_adjustments[d]
        dow_mult = week_config.dow_adjust.get(dow, 1.0)
        for hour in range(6, 24):
            h_orders = historical_orders.get((dow, hour), 0.0)
            h_sales = historical_sales.get((dow, hour), 0.0)
            sales_mult = adj.sales_multiplier_for_hour(hour)
            orders_mult = adj.orders_multiplier_for_hour(hour)

            adjusted_orders = h_orders * recency * orders_mult * dow_mult
            adjusted_sales = h_sales * recency * sales_mult * dow_mult

            projection[(d, hour)] = HourProjection(
                date=d, hour=hour,
                historical_orders=h_orders, historical_sales=h_sales,
                recency_factor=recency,
                day_factor=sales_mult,
                day_factor_orders=orders_mult,
                adjusted_orders=adjusted_orders, adjusted_sales=adjusted_sales,
            )

    # Automatic calibration: nudge not-yet-completed current-week days toward
    # reality based on how completed days tracked vs projection.
    if is_current:
        from csv_analyzer import current_week_actuals
        actuals = current_week_actuals(uploads_dir)
        cal, completed = auto_calibration_factor(projection, actuals, week_config.week_dates)
        if cal != 1.0 and completed:
            for d in week_config.week_dates:
                if d in actuals:
                    continue  # completed day — keep its raw projection
                for hour in range(6, 24):
                    hp = projection[(d, hour)]
                    hp.adjusted_sales *= cal
                    hp.adjusted_orders *= cal

    # Weekend BS/CW composition: bump Fri/Sat/Sun BS/CW to the Nate.Eatz-week
    # target and shift the incremental item revenue into the selling hours (day
    # total unchanged). Skip completed current-week days (they're actuals).
    skip = set()
    if is_current:
        from csv_analyzer import current_week_actuals
        skip = set(current_week_actuals(uploads_dir).keys())
    _reshape_weekend_bscw(projection, uploads_dir, week_config.week_dates,
                          week_config.lift_multiplier, skip_dates=skip)

    return projection, recency_factors, day_adjustments


def auto_calibration_factor(
    projection: Dict[Tuple[date, int], "HourProjection"],
    actuals: Dict[date, Dict[str, float]],
    week_dates: List[date],
) -> Tuple[float, List[date]]:
    """Calibration multiplier for not-yet-completed days, derived from how the
    model's projection compared to actuals on the COMPLETED days. Damped and
    capped per config. Returns (factor, completed_days). Operates on the
    uncalibrated completed-day projections (completed days are never scaled),
    so it's safe to call before or after the remaining-day scaling."""
    import config as _cfg
    if not getattr(_cfg, "AUTO_CALIBRATE", True):
        return 1.0, []
    completed = [d for d in week_dates if d in actuals]
    if not completed:
        return 1.0, []
    proj_sum = sum(projection[(d, h)].adjusted_sales
                   for d in completed for h in range(6, 24))
    act_sum = sum(actuals[d]["net"] for d in completed)
    if proj_sum <= 0:
        return 1.0, completed
    ratio = act_sum / proj_sum
    damping = float(getattr(_cfg, "AUTO_CALIBRATE_DAMPING", 0.5))
    cap = float(getattr(_cfg, "AUTO_CALIBRATE_MAX", 0.15))
    cal = 1.0 + (ratio - 1.0) * damping
    cal = max(1.0 - cap, min(1.0 + cap, cal))
    return cal, completed


def projected_bscw_qty_for_hour(
    uploads_dir: Path, dow: int, hour: int,
) -> Tuple[float, float]:
    """Helper for dashboards: projected BS, CW qty for a (dow, hour).
    Uses last-week data when available, falls back to 8-week avg."""
    from item_hourly import load_item_hourly
    last = load_item_hourly(
        uploads_dir,
        date_from=LAST_WEEK_START,
        date_to=LAST_WEEK_END,
    )
    fallback = load_item_hourly(uploads_dir)
    bs = last.get((dow, hour, "Breakfast Sandwich"))
    cw = last.get((dow, hour, "Chicken Caesar Wrap"))
    if bs is None:
        bs = fallback.get((dow, hour, "Breakfast Sandwich"), 0.0)
    if cw is None:
        cw = fallback.get((dow, hour, "Chicken Caesar Wrap"), 0.0)
    return bs, cw


# -----------------------------------------------------------------------------
# Pinch-point analysis
# -----------------------------------------------------------------------------

@dataclass
class PinchPoint:
    """An hour where the existing schedule may not meet adjusted demand."""
    date: date
    hour: int
    adjusted_orders: float
    adjusted_sales: float
    need: int           # required headcount given adjusted demand
    scheduled: float    # current schedule headcount for this hour
    delta: float        # need - scheduled (positive = understaffed)
    severity: str       # "warning" (delta < 1) | "issue" (delta >= 1)
    suggestion: str = ""


def find_pinch_points(
    projection: Dict[Tuple[date, int], HourProjection],
    week_config: Optional[WeekConfig] = None,
) -> List[PinchPoint]:
    """
    For each hour in projected week, compare adjusted need vs current schedule.
    Flag where adjusted demand creates a NEW shortage compared to baseline.
    """
    if week_config is None:
        week_config = FORWARD_WEEK_CONFIG
    from shift_optimizer import PROPOSED_SCHEDULE, day_schedule
    from break_scheduler import assign_breaks
    from weekly_dashboard import foh_need_for_hour, foh_scheduled_for_hour, build_hourly_metrics
    from item_hourly import load_item_hourly
    from pathlib import Path

    uploads = Path(__file__).parent / "data"
    baseline_orders, baseline_sales = build_hourly_metrics(uploads)
    baseline_proj_total = {dow: sum(baseline_sales.get((dow, h), 0) for h in range(6, 24))
                           for dow in range(7)}

    item_8wk = load_item_hourly(uploads)
    item_last = load_item_hourly(
        uploads, date_from=LAST_WEEK_START, date_to=LAST_WEEK_END,
    )
    recency_lookup = compute_recency_factor(uploads, week_config.lift_multiplier)

    def _bscw_revenue(items_dict, dow, h):
        bs = items_dict.get((dow, h, "Breakfast Sandwich"), 0.0)
        cw = items_dict.get((dow, h, "Chicken Caesar Wrap"), 0.0)
        return bs * NATE_BS_PRICE + cw * NATE_CW_PRICE

    # Baseline other_sales = baseline_sales - 8wk BS/CW revenue
    baseline_other_sales: Dict[Tuple[int, int], float] = {}
    for (dow_k, h_k), s in baseline_sales.items():
        bscw = _bscw_revenue(item_8wk, dow_k, h_k)
        baseline_other_sales[(dow_k, h_k)] = max(0, s - min(bscw, s))

    pinch_points: List[PinchPoint] = []

    for d in week_config.week_dates:
        dow = d.weekday()

        # Build adjusted (dow, hour) dicts for this specific day
        adj_orders = {(dow, h): projection[(d, h)].adjusted_orders for h in range(6, 24)}
        adj_sales = {(dow, h): projection[(d, h)].adjusted_sales for h in range(6, 24)}

        # Adjusted other_sales: BS/CW = baseline × ITEM-specific recency lift
        adj_other_sales: Dict[Tuple[int, int], float] = {}
        bs_lift, cw_lift = bscw_item_lifts(uploads, week_config.lift_multiplier)
        for h in range(6, 24):
            bs_8wk = item_8wk.get((dow, h, "Breakfast Sandwich"), 0.0)
            cw_8wk = item_8wk.get((dow, h, "Chicken Caesar Wrap"), 0.0)
            bs_p, cw_p = bs_8wk * bs_lift, cw_8wk * cw_lift
            bscw = bs_p * NATE_BS_PRICE + cw_p * NATE_CW_PRICE
            total = adj_sales[(dow, h)]
            adj_other_sales[(dow, h)] = max(0, total - min(bscw, total))

        # Daily projection for break scheduler
        daily_proj_for_breaks = {dow: sum(adj_sales.values())}

        # Adjusted schedule (run break-scheduler against adjusted demand).
        # Include this week's extra FOH shifts so we don't over-flag gaps the
        # operator has already staffed for.
        extras = getattr(week_config, "extra_foh_shifts", None)
        base_ovr = getattr(week_config, "base_foh_overrides", None)
        shifts_adj, _ = assign_breaks(
            day_schedule(dow, extras, base_ovr), dow,
            daily_proj_for_breaks, adj_orders, adj_sales
        )

        # Baseline schedule (against historical demand) — same shift structure
        # incl. extras, potentially different break placements
        shifts_baseline, _ = assign_breaks(
            day_schedule(dow, extras, base_ovr), dow,
            baseline_proj_total, baseline_orders, baseline_sales
        )

        for hour in range(7, 21):
            hp = projection[(d, hour)]

            need_adj = foh_need_for_hour(dow, hour, adj_orders, adj_sales, adj_other_sales)
            _, sched_min_adj = foh_scheduled_for_hour(shifts_adj, hour)

            need_baseline = foh_need_for_hour(dow, hour, baseline_orders, baseline_sales, baseline_other_sales)
            _, sched_min_baseline = foh_scheduled_for_hour(shifts_baseline, hour)

            # Was this already a gap in the baseline? If so, it's known — skip.
            baseline_gap = max(0, need_baseline - sched_min_baseline)
            adj_gap = max(0, need_adj - sched_min_adj)

            # Only flag if adjusted gap is WORSE than baseline gap
            if adj_gap > baseline_gap:
                delta = need_adj - sched_min_adj
                if delta >= 1:
                    severity = "issue"
                else:
                    severity = "warning"
                pinch_points.append(PinchPoint(
                    date=d, hour=hour,
                    adjusted_orders=hp.adjusted_orders,
                    adjusted_sales=hp.adjusted_sales,
                    need=need_adj, scheduled=sched_min_adj, delta=delta,
                    severity=severity,
                ))

    # Compute suggestions
    annotate_pinch_suggestions(pinch_points)
    return pinch_points


def annotate_pinch_suggestions(pinch_points: List[PinchPoint]) -> None:
    """
    Generate operationally-aware suggestions. Try to keep schedule structure
    intact — prefer extending existing shifts over adding new ones.
    """
    for p in pinch_points:
        h = p.hour
        if h < 10:
            p.suggestion = "Consider opener starting 30 min earlier"
        elif 10 <= h < 12:
            p.suggestion = "Consider rush-helper starting 30 min earlier"
        elif 12 <= h < 15:
            p.suggestion = "Consider mid-shift adding 30 min in middle of day"
        elif 15 <= h < 18:
            p.suggestion = "Consider closer-station/dish arriving 30 min earlier"
        else:
            p.suggestion = "Consider closer-station extending 30 min later"


# -----------------------------------------------------------------------------
# Weekly summary for projection
# -----------------------------------------------------------------------------

@dataclass
class WeekSummary:
    week_start: date
    week_end: date
    projected_total_sales: float
    historical_baseline_sales: float  # what the unadjusted week would be
    factors_used: List[str]            # human-readable description list
    recency_factors: Dict[int, float]
    pinch_points: List[PinchPoint]


def build_week_summary(
    uploads_dir: Path,
    week_config: Optional[WeekConfig] = None,
) -> Tuple[
    WeekSummary,
    Dict[Tuple[date, int], HourProjection],
]:
    if week_config is None:
        week_config = FORWARD_WEEK_CONFIG
    import config
    projection, recency, day_adj = build_forward_projection(uploads_dir, week_config)

    total_adj_sales = sum(hp.adjusted_sales for hp in projection.values())
    total_baseline_sales = sum(hp.historical_sales for hp in projection.values())

    # Driver-factor blurbs come from the WeekConfig
    factors: List[str] = list(week_config.factor_blurbs)

    pinch = find_pinch_points(projection, week_config)

    return (
        WeekSummary(
            week_start=week_config.week_dates[0],
            week_end=week_config.week_dates[-1],
            projected_total_sales=total_adj_sales,
            historical_baseline_sales=total_baseline_sales,
            factors_used=factors,
            recency_factors=recency,
            pinch_points=pinch,
        ),
        projection,
    )


if __name__ == "__main__":
    uploads = Path(__file__).parent / "data"
    summary, projection = build_week_summary(uploads)
    print(f"Week: {summary.week_start} to {summary.week_end}")
    print(f"Adjusted total sales: ${summary.projected_total_sales:,.0f}")
    print(f"Baseline (no adjustment): ${summary.historical_baseline_sales:,.0f}")
    print(f"Delta: ${summary.projected_total_sales - summary.historical_baseline_sales:+,.0f}")
    print()
    print("Factors used:")
    for f in summary.factors_used:
        print(f"  - {f}")
    print()
    print(f"Pinch points: {len(summary.pinch_points)}")
    for p in summary.pinch_points:
        dn = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][p.date.weekday()]
        print(f"  - {dn} {p.date.strftime('%-m/%-d')} {p.hour:02d}:00  "
              f"need {p.need}/sched {p.scheduled:.0f}  ({p.severity}) — {p.suggestion}")
