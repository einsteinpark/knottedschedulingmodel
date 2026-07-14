"""
Toast API -> dashboard CSV sync.

Pulls orders for a date range from the Toast API and writes the three files the
dashboard already consumes, so the manual Toast-export upload step goes away:

    data/Sales_by_day.csv   (yyyyMMdd, Net sales, Total orders, Total guests)
    data/Item_hourly.csv    (date, hour, item, qty)   -- appended, deduped by date
    data/actuals.csv        (date, net_sales, orders, bs, cw)  -- current-week realized days

CREDENTIALS COME FROM ENVIRONMENT VARIABLES ONLY. Never hard-code them and never
paste them into a chat. Set these in your deployment's secret store
(e.g. GitHub Actions secrets):

    TOAST_HOSTNAME           e.g. ws-api.toasttab.com   (your Toast API host)
    TOAST_CLIENT_ID
    TOAST_CLIENT_SECRET
    TOAST_RESTAURANT_GUID    the Arts District location GUID
    TOAST_TIMEZONE           default America/Los_Angeles

This mirrors the same Toast integration pattern you already use for the labor
audit. If you have a working Toast client there, you can point `_get_token` and
`fetch_orders` at it instead of the reference implementation below — the
transform functions (orders -> CSV rows) are what's specific to this dashboard.

NOTE: Toast's exact field names/definitions vary by account and API version.
Validate `_order_net_sales` and the item/hour extraction against one known day
(we verified that summing selection net prices reproduces the summary "Net
sales" exactly — Mon 6/22 = $4,766.75 — so that's the definition used here).
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import urllib.request
import urllib.error
import json

TRACKED_ITEMS = ["Breakfast Sandwich", "Chicken Caesar Wrap"]
_ITEM_ALIASES = {  # map Toast displayNames -> canonical tracked name if they differ
    "breakfast sandwich": "Breakfast Sandwich",
    "chicken caesar wrap": "Chicken Caesar Wrap",
    "caesar wrap": "Chicken Caesar Wrap",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _cfg(name: str, default: str = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(
            f"Missing required env var {name}. Set it in your secret store "
            f"(do not paste credentials into code or chat)."
        )
    return val


def _tz() -> ZoneInfo:
    return ZoneInfo(_cfg("TOAST_TIMEZONE", "America/Los_Angeles"))


# --------------------------------------------------------------------------- #
# Toast API (reference implementation — swap for your labor-audit client if you
# already have one that works against your account)
# --------------------------------------------------------------------------- #
def _http_json(url: str, *, method="GET", headers=None, body=None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Toast API {method} {url} -> {e.code}: {e.read().decode()[:300]}")


def _get_token() -> str:
    host = _cfg("TOAST_HOSTNAME", required=True)
    payload = {
        "clientId": _cfg("TOAST_CLIENT_ID", required=True),
        "clientSecret": _cfg("TOAST_CLIENT_SECRET", required=True),
        "userAccessType": "TOAST_MACHINE_CLIENT",
    }
    resp = _http_json(
        f"https://{host}/authentication/v1/authentication/login",
        method="POST",
        headers={"Content-Type": "application/json"},
        body=payload,
    )
    return resp["token"]["accessToken"]


def fetch_orders(start: date, end: date) -> List[dict]:
    """All orders with business date in [start, end] inclusive (paginated)."""
    host = _cfg("TOAST_HOSTNAME", required=True)
    guid = _cfg("TOAST_RESTAURANT_GUID", required=True)
    token = _get_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Toast-Restaurant-External-ID": guid,
    }
    out: List[dict] = []
    # ordersBulk is paged; businessDate uses yyyyMMdd in the restaurant's tz
    d = start
    while d <= end:
        bd = d.strftime("%Y%m%d")
        page = 1
        while True:
            url = (f"https://{host}/orders/v2/ordersBulk"
                   f"?businessDate={bd}&page={page}&pageSize=100")
            batch = _http_json(url, headers=headers)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# Transforms: orders -> dashboard rows
# --------------------------------------------------------------------------- #
def _order_dt(order: dict) -> datetime:
    """Local (restaurant-tz) datetime an order opened."""
    raw = order.get("openedDate") or order.get("paidDate") or order.get("createdDate")
    # Toast timestamps look like 2026-06-22T15:18:00.000+0000
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt.astimezone(_tz())


def _is_non_revenue(sel: dict) -> bool:
    """Gift cards and store-value loads are deferred revenue, not net sales.
    Toast flags these as a selectionType of GIFT_CARD / STORED_VALUE, and in the
    item-detail export they show up with a blank sales category and a 'Gift Card'
    item name — so we screen on both signals."""
    stype = (sel.get("selectionType") or "").upper()
    if stype in ("GIFT_CARD", "STORED_VALUE", "HOUSE_ACCOUNT_PAYMENT"):
        return True
    name = (sel.get("displayName") or "").strip().lower()
    if "gift card" in name or "gift certificate" in name:
        return True
    return False


def _order_net_sales(order: dict) -> float:
    """Sum of selection net prices across all checks (post-discount, pre-tax),
    excluding gift cards / non-revenue lines. Matches Toast's summary 'Net sales'
    (validated: gift-card days would otherwise overstate net sales)."""
    total = 0.0
    for check in order.get("checks", []) or []:
        if check.get("deleted"):
            continue
        for sel in check.get("selections", []) or []:
            if sel.get("voided") or sel.get("deleted") or _is_non_revenue(sel):
                continue
            # 'price' is the net (post-discount) line price in most Toast accounts
            total += float(sel.get("price") or 0.0)
    return total


def _order_items(order: dict) -> List[Tuple[str, float]]:
    """[(canonical_item_name, qty)] for TRACKED_ITEMS only."""
    out = []
    for check in order.get("checks", []) or []:
        if check.get("deleted"):
            continue
        for sel in check.get("selections", []) or []:
            if sel.get("voided") or sel.get("deleted"):
                continue
            name = (sel.get("displayName") or "").strip()
            canon = _ITEM_ALIASES.get(name.lower())
            if canon:
                out.append((canon, float(sel.get("quantity") or 1)))
    return out


def build_rows(orders: List[dict]):
    """Returns (sales_by_day, item_hourly, actuals) keyed structures."""
    day_net: Dict[date, float] = defaultdict(float)
    day_orders: Dict[date, int] = defaultdict(int)
    day_guests: Dict[date, int] = defaultdict(int)
    item_hourly: Dict[Tuple[date, int, str], float] = defaultdict(float)
    day_items: Dict[Tuple[date, str], float] = defaultdict(float)

    for o in orders:
        dt = _order_dt(o)
        d = dt.date()
        day_net[d] += _order_net_sales(o)
        day_orders[d] += 1
        day_guests[d] += int(o.get("numberOfGuests") or 1)
        for name, qty in _order_items(o):
            item_hourly[(d, dt.hour, name)] += qty
            day_items[(d, name)] += qty

    return day_net, day_orders, day_guests, item_hourly, day_items


# --------------------------------------------------------------------------- #
# CSV writers (merge with existing files; new dates overwrite same-date rows)
# --------------------------------------------------------------------------- #
def _write_sales_by_day(data_dir: Path, day_net, day_orders, day_guests):
    path = data_dir / "Sales_by_day.csv"
    existing: Dict[str, List[str]] = {}
    if path.exists():
        with path.open() as f:
            r = csv.reader(f)
            header = next(r, None)
            for row in r:
                if row:
                    existing[row[0]] = row
    for d in day_net:
        key = d.strftime("%Y%m%d")
        existing[key] = [key, f"{day_net[d]:.2f}", str(day_orders[d]), str(day_guests[d])]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["yyyyMMdd", "Net sales", "Total orders", "Total guests"])
        for key in sorted(existing):
            w.writerow(existing[key])


def _write_item_hourly(data_dir: Path, item_hourly):
    path = data_dir / "Item_hourly.csv"
    rows: Dict[Tuple[str, int, str], float] = {}
    new_dates = {d for (d, _h, _i) in item_hourly}
    if path.exists():
        with path.open() as f:
            for row in csv.DictReader(f):
                try:
                    dd = date.fromisoformat(row["date"])
                except (KeyError, ValueError):
                    continue
                if dd in new_dates:
                    continue  # replaced below
                rows[(row["date"], int(row["hour"]), row["item"])] = float(row["qty"])
    for (d, h, item), qty in item_hourly.items():
        rows[(d.isoformat(), h, item)] = qty
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "hour", "item", "qty"])
        for (dstr, h, item) in sorted(rows):
            w.writerow([dstr, h, item, int(round(rows[(dstr, h, item)]))])


def _write_actuals(data_dir: Path, day_net, day_orders, day_items, week_start: date):
    """Realized current-week days only (date >= week_start)."""
    path = data_dir / "actuals.csv"
    existing: Dict[str, List[str]] = {}
    if path.exists():
        with path.open() as f:
            for row in csv.DictReader(f):
                existing[row["date"]] = [row["date"], row["net_sales"],
                                         row["orders"], row["bs"], row["cw"]]
    for d in day_net:
        if d < week_start:
            continue
        key = d.isoformat()
        bs = int(round(day_items.get((d, "Breakfast Sandwich"), 0)))
        cw = int(round(day_items.get((d, "Chicken Caesar Wrap"), 0)))
        existing[key] = [key, f"{day_net[d]:.2f}", str(day_orders[d]), str(bs), str(cw)]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "net_sales", "orders", "bs", "cw"])
        for key in sorted(existing):
            w.writerow(existing[key])


def sync(data_dir: Path, start: date, end: date, current_week_start: date) -> dict:
    """Pull [start, end] from Toast and update the three CSVs. Returns a summary."""
    orders = fetch_orders(start, end)
    day_net, day_orders, day_guests, item_hourly, day_items = build_rows(orders)
    _write_sales_by_day(data_dir, day_net, day_orders, day_guests)
    _write_item_hourly(data_dir, item_hourly)
    _write_actuals(data_dir, day_net, day_orders, day_items, current_week_start)
    return {
        "orders": len(orders),
        "days": sorted(d.isoformat() for d in day_net),
        "net_total": round(sum(day_net.values()), 2),
    }
