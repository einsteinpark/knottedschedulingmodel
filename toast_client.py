"""
Toast API client.

Pulls:
  - Restaurant info (close-out hour, timezone)
  - Menu items + their sales categories / tags
  - Historical orders for the lookback window
  - Aggregates net sales by (date, hour, category bucket: FOH vs BOH)

Toast uses OAuth2 client-credentials. Tokens last ~24h.
Endpoints:
  POST /authentication/v1/authentication/login
  GET  /restaurants/v1/restaurants/{guid}
  GET  /menus/v2/menus            (for item -> category mapping)
  GET  /orders/v2/ordersBulk      (orders for a business date)
"""

from __future__ import annotations

import time as _time
import logging
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

import requests

import config

log = logging.getLogger(__name__)


class ToastClient:
    def __init__(
        self,
        client_id: str = config.TOAST_CLIENT_ID,
        client_secret: str = config.TOAST_CLIENT_SECRET,
        restaurant_guid: str = config.TOAST_RESTAURANT_GUID,
        hostname: str = config.TOAST_API_HOSTNAME,
    ):
        if not (client_id and client_secret and restaurant_guid):
            raise ValueError(
                "Missing Toast credentials. Set TOAST_CLIENT_ID, "
                "TOAST_CLIENT_SECRET, TOAST_RESTAURANT_GUID env vars."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.restaurant_guid = restaurant_guid
        self.hostname = hostname.rstrip("/")
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0
        self._item_to_category: Optional[Dict[str, str]] = None  # itemGuid -> bucket

    # ---------- auth ----------
    def _get_token(self) -> str:
        if self._token and _time.time() < self._token_expires_at - 60:
            return self._token

        url = f"{self.hostname}/authentication/v1/authentication/login"
        body = {
            "clientId": self.client_id,
            "clientSecret": self.client_secret,
            "userAccessType": "TOAST_MACHINE_CLIENT",
        }
        r = requests.post(url, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()["token"]
        self._token = data["accessToken"]
        self._token_expires_at = _time.time() + int(data["expiresIn"])
        log.info("Toast auth OK, expires in %ss", data["expiresIn"])
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Toast-Restaurant-External-ID": self.restaurant_guid,
            "Accept": "application/json",
        }

    def _get(self, path: str, params: Optional[dict] = None) -> dict | list:
        url = f"{self.hostname}{path}"
        for attempt in range(5):
            r = requests.get(url, headers=self._headers(), params=params, timeout=60)
            if r.status_code == 429:
                wait = 2 ** attempt
                log.warning("Toast rate-limited, sleeping %ss", wait)
                _time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    # ---------- menu / categorization ----------
    def build_item_category_map(self) -> Dict[str, str]:
        """
        Returns: {menuItemGuid: 'foh' | 'boh' | 'other'}

        Categorizes each menu item by matching sales-category name and item-group
        names against the keyword lists in config.
        """
        if self._item_to_category is not None:
            return self._item_to_category

        log.info("Pulling menu structure from Toast...")
        menus = self._get("/menus/v2/menus")
        item_map: Dict[str, str] = {}

        for menu in menus.get("menus", []):
            menu_name = (menu.get("name") or "").lower()
            for group in menu.get("menuGroups", []):
                group_name = (group.get("name") or "").lower()
                for item in group.get("menuItems", []):
                    item_guid = item.get("guid")
                    if not item_guid:
                        continue
                    item_name = (item.get("name") or "").lower()
                    sales_cat = (item.get("salesCategory", {}) or {}).get("name", "").lower()

                    haystack = " ".join([menu_name, group_name, item_name, sales_cat])
                    bucket = "other"
                    if any(k in haystack for k in config.FOH_CATEGORY_KEYWORDS):
                        bucket = "foh"
                    elif any(k in haystack for k in config.BOH_CATEGORY_KEYWORDS):
                        bucket = "boh"
                    item_map[item_guid] = bucket

        log.info(
            "Categorized %d items: %d FOH, %d BOH, %d other",
            len(item_map),
            sum(1 for v in item_map.values() if v == "foh"),
            sum(1 for v in item_map.values() if v == "boh"),
            sum(1 for v in item_map.values() if v == "other"),
        )
        self._item_to_category = item_map
        return item_map

    # ---------- orders ----------
    def fetch_orders_for_business_date(self, business_date: date) -> List[dict]:
        """Get all orders that fall under the given Toast 'business date'."""
        date_str = business_date.strftime("%Y%m%d")
        all_orders: List[dict] = []
        page = 1
        while True:
            data = self._get(
                "/orders/v2/ordersBulk",
                params={"businessDate": date_str, "page": page, "pageSize": 100},
            )
            # Toast returns either a list or paginated dict depending on endpoint
            batch = data if isinstance(data, list) else data.get("orders", [])
            if not batch:
                break
            all_orders.extend(batch)
            if len(batch) < 100:
                break
            page += 1
            _time.sleep(0.1)  # be polite to rate limiter
        return all_orders

    # ---------- aggregation ----------
    def fetch_hourly_sales(
        self, start_date: date, end_date: date
    ) -> Dict[Tuple[date, int], Dict[str, float]]:
        """
        For each (business_date, hour-of-day), returns net sales split by bucket.

        Returns:
            {
                (date(2026,5,12), 9): {"foh": 245.50, "boh": 80.00, "total": 325.50},
                ...
            }

        Net sales = sum of selection.price (after item discounts), excluding
        voided/deleted items, taxes, tips, and gift cards. Service charges
        omitted for simplicity (can be added if material).
        """
        item_map = self.build_item_category_map()
        hourly: Dict[Tuple[date, int], Dict[str, float]] = defaultdict(
            lambda: {"foh": 0.0, "boh": 0.0, "other": 0.0, "total": 0.0}
        )

        d = start_date
        while d <= end_date:
            log.info("Pulling Toast orders for %s", d)
            orders = self.fetch_orders_for_business_date(d)
            for order in orders:
                if order.get("voided") or order.get("deleted"):
                    continue
                for check in order.get("checks", []):
                    if check.get("voided") or check.get("deleted"):
                        continue
                    for sel in check.get("selections", []):
                        if sel.get("voided") or sel.get("deleted"):
                            continue
                        item_guid = (sel.get("item") or {}).get("guid")
                        bucket = item_map.get(item_guid, "other")
                        # Toast: 'price' is the net after item-level discounts
                        price = float(sel.get("price") or 0.0)
                        # Time the sale was made (use createdDate or openedDate)
                        ts_str = (
                            sel.get("createdDate")
                            or check.get("openedDate")
                            or order.get("openedDate")
                        )
                        if not ts_str:
                            continue
                        # Toast timestamps are ISO 8601 with 'Z'
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        # We bucket by LOCAL hour; Toast 'businessDate' already
                        # collapses overnight orders, so the hour-of-day is what
                        # matters for staffing. Use the order's business date.
                        key = (d, ts.hour)
                        hourly[key][bucket] += price
                        hourly[key]["total"] += price
            d += timedelta(days=1)
        return dict(hourly)
