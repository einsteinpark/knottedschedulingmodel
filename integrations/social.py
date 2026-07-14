"""
Social performance -> projection signal (Instagram + TikTok).

GOAL: turn "we got featured / a reel is popping" into a quantified lift input
instead of a hand-set +35%.

HONEST STATUS — read before relying on this:
  * Instagram: WORKABLE but needs setup. The IG Graph API exposes media insights
    (reach, plays/views, likes, comments, saves) ONLY for Business/Creator
    accounts linked to a Facebook Page, via a Meta app with the
    `instagram_basic` + `instagram_manage_insights` permissions and a long-lived
    token. That token/app must be created in Meta's developer console (one-time)
    and the app submitted for review for production use. It cannot be set up from
    a chat. Once you have IG_USER_ID + IG_ACCESS_TOKEN in your secret store, the
    fetch below works.
  * TikTok: HARDER. There is no simple read API for an arbitrary creator's view
    counts. TikTok's official insights require the Business API or Login Kit with
    approved scopes, and third-party features (someone *else* tagging you) aren't
    retrievable at all. `fetch_tiktok_metrics` is a stub that documents this.

Credentials from environment only (never in chat):
    IG_USER_ID
    IG_ACCESS_TOKEN          long-lived
    (TikTok creds intentionally omitted until the Business API path is set up)

The mapping from engagement -> sales lift is a heuristic you should tune against
realized days; `engagement_to_lift_signal` is deliberately conservative and
returns a *suggested* number, not an auto-applied one, so a viral vanity metric
can't silently inflate staffing.
"""
from __future__ import annotations

import os
import json
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from typing import Dict, List

_GRAPH = "https://graph.facebook.com/v19.0"


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Graph API -> {e.code}: {e.read().decode()[:300]}")


def fetch_instagram_metrics(since: date = None) -> List[Dict]:
    """Recent IG media with insights. Returns [{id, ts, caption, reach, views,
    likes, comments, saves}]. Requires IG_USER_ID + IG_ACCESS_TOKEN."""
    uid = os.environ.get("IG_USER_ID")
    tok = os.environ.get("IG_ACCESS_TOKEN")
    if not (uid and tok):
        raise RuntimeError("Set IG_USER_ID and IG_ACCESS_TOKEN in your secret store.")
    since = since or (date.today() - timedelta(days=14))

    media = _get(f"{_GRAPH}/{uid}/media"
                 f"?fields=id,caption,media_type,timestamp,permalink"
                 f"&since={since.isoformat()}&limit=50&access_token={tok}")
    out = []
    for m in media.get("data", []):
        try:
            ts = datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        # Reels report 'plays'/'reach'; feed posts 'reach'/'impressions'
        metric = "reach,likes,comments,saved,plays" if m.get("media_type") == "VIDEO" \
            else "reach,impressions,likes,comments,saved"
        try:
            ins = _get(f"{_GRAPH}/{m['id']}/insights"
                       f"?metric={metric}&access_token={tok}")
            vals = {d["name"]: d["values"][0]["value"] for d in ins.get("data", [])}
        except RuntimeError:
            vals = {}
        out.append({
            "id": m["id"], "ts": ts.isoformat(), "caption": (m.get("caption") or "")[:120],
            "permalink": m.get("permalink"),
            "reach": vals.get("reach", 0), "views": vals.get("plays", vals.get("impressions", 0)),
            "likes": vals.get("likes", 0), "comments": vals.get("comments", 0),
            "saves": vals.get("saved", 0),
        })
    return out


def fetch_tiktok_metrics(*_a, **_k):
    raise NotImplementedError(
        "TikTok view/like counts require the approved Business API (or Login Kit "
        "with insights scopes). A creator who tags you is not retrievable via API "
        "at all. For now, paste the post link in chat and I'll pull what's public, "
        "or enter the numbers manually as a day adjustment."
    )


def engagement_to_lift_signal(metrics: List[Dict], baseline_reach: float) -> Dict:
    """SUGGESTED (not auto-applied) lift from a window's engagement vs a baseline.

    Conservative: caps the suggestion and returns the supporting numbers so a
    human confirms before it touches the projection.
    """
    if not metrics:
        return {"suggested_lift_pct": 0.0, "peak_post": None, "total_reach": 0}
    total_reach = sum(m["reach"] for m in metrics)
    peak = max(metrics, key=lambda m: m["reach"])
    ratio = (total_reach / baseline_reach) if baseline_reach else 1.0
    # log-ish, capped: a 2x reach week suggests ~+10%, 4x ~+20%, capped at +35%
    import math
    suggested = min(0.35, max(0.0, 0.15 * math.log2(ratio))) if ratio > 1 else 0.0
    return {
        "suggested_lift_pct": round(suggested * 100, 1),
        "peak_post": {"permalink": peak.get("permalink"), "reach": peak["reach"],
                      "views": peak["views"], "likes": peak["likes"]},
        "total_reach": total_reach,
        "reach_vs_baseline": round(ratio, 2),
        "note": "Suggested only — confirm before applying to the lift.",
    }
