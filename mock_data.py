"""
Mock data generator — lets you run the full pipeline without API credentials.

Generates 8 weeks of synthetic hourly sales that roughly mimic a cafe pattern:
  - Morning rush (8-10am)
  - Lunch lull (11a-1p) on weekdays, brunch peak (10a-2p) on weekends
  - Afternoon dip (2-4p)
  - Evening softer
  - Weekends much busier overall

Includes a couple of injected "outlier" days to verify outlier detection.
"""

import random
from datetime import date, timedelta
from typing import Dict, Tuple
from collections import defaultdict

MOCK_FOH_WAGE = 18.50
MOCK_BOH_WAGE = 22.00


def _base_hour_pattern(dow: int, hour: int) -> tuple[float, float]:
    """Return (foh_baseline, boh_baseline) in dollars for a given day/hour."""
    weekend = dow in (4, 5)
    sunday = dow == 6

    # Hour-of-day shape (cafe pattern)
    if hour < 7:
        shape = 0.0
    elif hour == 7:
        shape = 0.3   # pre-open, but cooks already prepping; some catering pickups
    elif 8 <= hour <= 10:
        shape = 1.0 if not weekend else 1.5  # morning rush
    elif 11 <= hour <= 13:
        shape = 0.6 if not weekend else 1.4  # lunch / brunch
    elif 14 <= hour <= 16:
        shape = 0.5
    elif 17 <= hour <= 19:
        shape = 0.65
    elif 20 <= hour <= 21:
        shape = 0.4 if weekend else 0.15
    else:
        shape = 0.0

    # Day-of-week multiplier
    dow_mult = {0: 0.9, 1: 0.85, 2: 0.95, 3: 1.0, 4: 1.4, 5: 1.55, 6: 1.1}[dow]

    foh_peak = 220 * shape * dow_mult
    boh_peak = 80 * shape * dow_mult
    return foh_peak, boh_peak


def generate_mock_hourly_sales(
    start_date: date, end_date: date
) -> Dict[Tuple[date, int], Dict[str, float]]:
    random.seed(42)
    out: Dict[Tuple[date, int], Dict[str, float]] = defaultdict(
        lambda: {"foh": 0.0, "boh": 0.0, "other": 0.0, "total": 0.0}
    )

    # Pick two random days within range to be outliers
    span = (end_date - start_date).days
    outlier_offsets = random.sample(range(span), k=2)

    d = start_date
    while d <= end_date:
        dow = d.weekday()
        # Day-level noise: usually +/- 15%
        day_noise = random.gauss(1.0, 0.15)
        # Outlier days: simulate a big event (3x sales) or a closure (0.1x)
        offset_from_start = (d - start_date).days
        if offset_from_start == outlier_offsets[0]:
            day_noise = 3.0
        elif offset_from_start == outlier_offsets[1]:
            day_noise = 0.1

        for hour in range(6, 23):
            foh_base, boh_base = _base_hour_pattern(dow, hour)
            if foh_base == 0 and boh_base == 0:
                continue
            hour_noise = random.gauss(1.0, 0.20)
            foh = max(0.0, foh_base * day_noise * hour_noise)
            boh = max(0.0, boh_base * day_noise * hour_noise)
            other = max(0.0, random.gauss(15, 5) * day_noise) if hour >= 8 and hour < 20 else 0.0
            out[(d, hour)] = {
                "foh": foh, "boh": boh, "other": other,
                "total": foh + boh + other,
            }
        d += timedelta(days=1)
    return dict(out)
