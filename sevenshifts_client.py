"""
7shifts API client.

Pulls per-employee wage data so we can compute realistic labor cost.
We aggregate to avg FOH wage and avg BOH wage (weighted by employee, not hours).

API:
  - Base: https://api.7shifts.com/v2
  - Auth: Bearer token (access_token from OAuth or static access token)
  - Rate limit: 10 req/sec per token
  - GET /company/{company_id}/users               (list employees)
  - GET /company/{company_id}/users/{uid}/wages   (per-user wage history)
  - GET /company/{company_id}/labor_settings      (to check wage_based_roles_enabled)

Assumption: roles in 7shifts are labeled clearly enough that we can classify
employees as FOH vs BOH. Tune ROLE_KEYWORDS as needed.
"""

from __future__ import annotations

import time as _time
import logging
from typing import Dict, List, Optional, Tuple
from datetime import date

import requests

import config

log = logging.getLogger(__name__)

FOH_ROLE_KEYWORDS = ["barista", "cashier", "front", "foh", "server"]
BOH_ROLE_KEYWORDS = ["cook", "kitchen", "boh", "prep", "chef", "dishwasher"]


class SevenShiftsClient:
    def __init__(
        self,
        access_token: str = config.SEVENSHIFTS_ACCESS_TOKEN,
        company_id: str = config.SEVENSHIFTS_COMPANY_ID,
        location_id: str = config.SEVENSHIFTS_LOCATION_ID,
    ):
        if not (access_token and company_id):
            raise ValueError(
                "Missing 7shifts credentials. Set SEVENSHIFTS_ACCESS_TOKEN "
                "and SEVENSHIFTS_COMPANY_ID env vars."
            )
        self.access_token = access_token
        self.company_id = company_id
        self.location_id = location_id or None
        self.base = "https://api.7shifts.com/v2"
        self._role_map: Optional[Dict[str, str]] = None  # role_id -> bucket

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base}{path}"
        for attempt in range(5):
            r = requests.get(url, headers=self._headers(), params=params, timeout=30)
            if r.status_code == 429:
                wait = 2 ** attempt
                log.warning("7shifts rate-limited, sleeping %ss", wait)
                _time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    # ---------- roles ----------
    def build_role_map(self) -> Dict[str, str]:
        """Map role_id -> 'foh' | 'boh' | 'other' by matching name keywords."""
        if self._role_map is not None:
            return self._role_map
        data = self._get(f"/company/{self.company_id}/roles", params={"limit": 200})
        role_map: Dict[str, str] = {}
        for role in data.get("data", []):
            rid = str(role.get("id"))
            name = (role.get("name") or "").lower()
            if any(k in name for k in FOH_ROLE_KEYWORDS):
                role_map[rid] = "foh"
            elif any(k in name for k in BOH_ROLE_KEYWORDS):
                role_map[rid] = "boh"
            else:
                role_map[rid] = "other"
        self._role_map = role_map
        log.info("Mapped %d 7shifts roles", len(role_map))
        return role_map

    # ---------- users ----------
    def _list_users(self) -> List[dict]:
        users: List[dict] = []
        cursor = None
        while True:
            params = {"limit": 100, "status": "active"}
            if self.location_id:
                params["location_id"] = self.location_id
            if cursor:
                params["cursor"] = cursor
            data = self._get(f"/company/{self.company_id}/users", params=params)
            users.extend(data.get("data", []))
            cursor = data.get("meta", {}).get("cursor", {}).get("next")
            if not cursor:
                break
        return users

    def _get_user_wages(self, user_id: int) -> List[dict]:
        data = self._get(f"/company/{self.company_id}/users/{user_id}/wages")
        return data.get("data", [])

    # ---------- aggregation ----------
    def get_average_wages(self) -> Tuple[float, float]:
        """
        Returns (avg_foh_hourly_wage, avg_boh_hourly_wage) in dollars.

        For each active user, finds their most recent effective hourly wage and
        classifies them by their primary role. Weekly-salary employees are
        excluded (assumed to be management/non-scheduled labor).
        """
        role_map = self.build_role_map()
        users = self._list_users()
        log.info("Pulled %d users from 7shifts", len(users))

        foh_wages: List[float] = []
        boh_wages: List[float] = []
        today = date.today().isoformat()

        for user in users:
            uid = user.get("id")
            if not uid:
                continue
            try:
                wages = self._get_user_wages(uid)
            except Exception as e:
                log.warning("Could not pull wages for user %s: %s", uid, e)
                continue

            # Find the most recent hourly wage record that is in effect
            best = None
            for w in wages:
                if w.get("wage_type") != "hourly":
                    continue
                eff = w.get("effective_date") or ""
                if eff > today:
                    continue
                if best is None or eff > (best.get("effective_date") or ""):
                    best = w
            if best is None:
                continue

            wage_dollars = float(best.get("wage_cents", 0)) / 100.0
            if wage_dollars <= 0:
                continue

            role_id = str(best.get("role_id")) if best.get("role_id") else None
            bucket = role_map.get(role_id, "other") if role_id else "other"

            # Fall back to user's role assignments if wage has no role
            if bucket == "other":
                for ra in user.get("role_assignments", []) or []:
                    rid = str(ra.get("role_id"))
                    b = role_map.get(rid, "other")
                    if b in ("foh", "boh"):
                        bucket = b
                        break

            if bucket == "foh":
                foh_wages.append(wage_dollars)
            elif bucket == "boh":
                boh_wages.append(wage_dollars)

        avg_foh = sum(foh_wages) / len(foh_wages) if foh_wages else 18.0
        avg_boh = sum(boh_wages) / len(boh_wages) if boh_wages else 22.0
        log.info(
            "Avg FOH wage: $%.2f (n=%d). Avg BOH wage: $%.2f (n=%d)",
            avg_foh, len(foh_wages), avg_boh, len(boh_wages),
        )
        return avg_foh, avg_boh
