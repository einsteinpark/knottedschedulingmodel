# Automating the Knotted dashboard

This turns the manual "export from Toast → upload CSVs → regenerate" loop into a
scheduled job that pulls data itself, re-tunes, rebuilds the dashboard, and
publishes it every Sunday night.

## What's actually automatic vs. what needs you

| Piece | Status |
|---|---|
| Pull sales/items from Toast → CSVs | **Code written** (`integrations/toast_sync.py`). Runs once you add Toast credentials as secrets. |
| Self-calibration from finalized days | **Already automatic** in the model (config `AUTO_CALIBRATE`). |
| Recency-weighted baseline + item lift | **Already automatic.** |
| Re-render dashboard (HTML + PDF) | **Automatic** (`run_weekly.py`). |
| Run every Sunday night | **Automatic once deployed** via GitHub Actions cron (`.github/workflows/weekly_dashboard.yml`). |
| Actual scheduled labor from 7shifts | **Code written** (`integrations/sevenshifts_sync.py`), optional. |
| Instagram post/reel insights → lift suggestion | **Code written** (`integrations/social.py`) but needs a Meta app + token (see below). Suggestion only — never auto-applied. |
| TikTok view counts | **Not feasible via API** for arbitrary/tagged posts. Paste me the link or enter manually. |
| Auto-advancing the week windows | **Not yet** — see "Remaining gap" below. |

Two honest limits: I can't run this schedule for you or hold your keys — it runs
on your GitHub Actions with your secrets. And I can't set up the Meta/TikTok apps
from a chat; those are one-time console steps on your side.

## Security — do not paste keys into chat or code

All credentials are read from **environment variables** by the integration
modules. Put them in **GitHub → repo → Settings → Secrets and variables →
Actions**. Never commit them, never send them in a message. The workflow injects
them as env vars at run time only.

### Secrets to set

Toast (required for the sales sync):
```
TOAST_HOSTNAME           e.g. ws-api.toasttab.com
TOAST_CLIENT_ID
TOAST_CLIENT_SECRET
TOAST_RESTAURANT_GUID    Arts District location GUID
```
7shifts (optional — actual scheduled labor):
```
SEVENSHIFTS_API_KEY
SEVENSHIFTS_COMPANY_ID
SEVENSHIFTS_LOCATION_ID
```
Instagram (optional — social signal):
```
IG_USER_ID
IG_ACCESS_TOKEN          long-lived
```

## Schedule

`.github/workflows/weekly_dashboard.yml` runs `cron: "0 6 * * 1"` =
**Monday 06:00 UTC = Sunday 11:00 PM PDT**. GitHub cron ignores DST, so in winter
(PST) it fires at 10:00 PM Pacific. You can also trigger it any time from the
Actions tab (`workflow_dispatch`). It commits the refreshed CSVs + dashboard and
publishes `index.html` to GitHub Pages.

## Toast field assumptions (validate once)

The sync defines **net sales = sum of selection net prices** (post-discount,
pre-tax) across checks. We verified this reproduces Toast's summary "Net sales"
exactly on a known day (Mon 6/22 = $4,766.75). If your Toast account defines net
sales differently, adjust `_order_net_sales` in `toast_sync.py`. Item quantities
and the hour bucket come from each order's opened time in `TOAST_TIMEZONE`.

If you'd rather reuse the **proven Toast/7shifts client from your labor audit**,
point `toast_sync._get_token` / `fetch_orders` (and `sevenshifts_sync.fetch_shifts`)
at those functions — the transform-to-CSV code stays the same and is the part
that's specific to this dashboard.

## Instagram setup (one-time, on your side)

1. Convert the IG account to Business/Creator and link it to a Facebook Page.
2. Create an app at developers.facebook.com; add the Instagram Graph API.
3. Grant `instagram_basic` + `instagram_manage_insights`; generate a long-lived
   token; submit for app review for ongoing use.
4. Put `IG_USER_ID` + `IG_ACCESS_TOKEN` in Actions secrets.

The job then prints a **suggested** lift from reach vs. your baseline. It is not
applied automatically — you confirm it, because a viral vanity metric shouldn't
silently change staffing.

## Remaining gap: auto-advancing the weeks

Right now the three week windows (baseline / current / forward) are pinned in
`config.py` / `forward_projection.py`. For the Sunday job to roll forward on its
own every week, those need to be derived from the run date (current week =
`current_monday()`, forward = +7 days, baseline = trailing 4 weeks). That's a
focused change I can make next — say the word and I'll wire the dates to the run
date so the recurring job advances with no edits.

## Test locally without credentials

```
python run_weekly.py --skip-toast      # re-render from the CSVs already on disk
```
