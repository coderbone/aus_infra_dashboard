# NOTES — exporters

Working notes for whoever picks the scrapers up next. The WHT sections are about
`wht_tbm_exporter.py`; the Snowy Hydro 2.0 section is about
`snowy_tantangara_exporter.py`, which was added on 2026-09-29 from the audit
recorded there. Everything here is about the exporters and the upstream data
they read; the Docker/Prometheus/Grafana stack they run in is documented one
level up in `../README.md` and `../NOTES.md`.

## Upstream data — durable facts

Source: <https://caportal.com.au/rms/wht/tbm-tracker> (Transport for NSW Western
Harbour Tunnel, WHTP2). Nothing is scraped from rendered HTML; the exporter
walks the public ArcGIS item graph instead.

1. Portal page → ArcGIS dashboard id `973e08367a544c879edd7f345f9d6a15`.
   **The page sits behind CloudFront and returns 403 to any request without a
   browser-like `User-Agent`.** That is the only reason the exporter sends one.
2. Dashboard item data → gauge widgets. Each has a `main` dataset pointing at a
   web map layer and a hard-coded `max`/`reference` static dataset. **That
   hard-coded number is the TBM's target distance** — it is not published
   anywhere else. Desktop view is authoritative; the mobile view still has a
   stale 1500 m and is only a fallback.
3. Web map item `644ba22abece4a08bd5795ec50160d50` → the two
   `TBM <name> Location` point layers (`Tunnel_Progress_m`, `Ring_Number`,
   `Timestamp`, `TBM`).
4. One `query` per layer, `orderByFields=Timestamp DESC`, limit 1.

| TBM | Layer feature service | Target (at 2026-09-26) |
| --- | --- | --- |
| Barangaroo (M110) | `utility.arcgis.com/usrsvcs/servers/b613c8229c174e429200155d5e829e31/rest/services/P_AU_WHTP2_TBM_Barangaroo_Location_PublicView/FeatureServer/0` | 1567 m |
| Patyegarang (M111) | `utility.arcgis.com/usrsvcs/servers/21ebd5cac3e3405cbce55a7703c65af0/rest/services/P_AU_WHTP2_TBM_Patyegarang_Location_PublicView/FeatureServer/0` | 1560 m |

Targets only change when someone edits the dashboard, so re-discovery picks up
new values automatically. Delete the cache file in the exporter container
(`/cache/config.json` on the `exporter-cache` volume) to force re-discovery; it
is only a fallback for when discovery fails.

WHTP2 is the only live Australian TBM tracker of its kind — see
[Other Australian TBM sources](#other-australian-tbm-sources--audited-2026-09-29)
below before adding a second exporter for anything else. Snowy Hydro 2.0 was
audited separately and is the nearest thing to a comparable project; see
[Snowy Hydro 2.0](#snowy-hydro-20--audited-2026-09-29).

## Other Australian TBM sources — audited 2026-09-29

Searched arcgis.com public content for every other Australian TBM tracker
(`TBM`, `TBM tracker`, `TBM progress`, `TBM location`, `TBM chainage`, plus the
major project names) and checked each layer's `editingInfo.dataLastEditDate`.

**Conclusion: WHTP2 is the only in-progress Australian TBM project publishing a
live public tracker. Do not add exporters for the others — they are all
finished and would only ever emit constants.** Re-run the search if a new
project is announced; the test to apply is "is the layer still being edited
and is any machine below 100%".

| Project | Source | Last data edit | Verdict |
| --- | --- | --- | --- |
| Western Harbour Tunnel NSW | WHTP2 dashboard `973e08367a544c879edd7f345f9d6a15` | 2026-09-29 | **live** — this exporter |
| Metro Tunnel VIC | `Tunnelling_Progress` | 2021-05-24 | 16/16 rows `STATUS=DONE` |
| Metro Tunnel VIC | `TBM_Tracker_Symbols[_Public_View]` | 2021-05-24 | complete |
| North East Link VIC | `MRPV_NEL_TBM_Tracking_FeatureLayer` | 2026-07-29 | both TBMs at 100% |
| North East Link VIC | `MRPV_NEL_COMMs_TBM_Spark_Tracking_FeatureLayer` | 2026-06-26 | stale view, still shows 90.5% |
| North East Link VIC | `MRPV_NEL_TBM_Location_Markers_FeatureLayer` | 2026-06-26 | stale view, still shows 90.5% |
| West Gate Tunnel VIC | `TBM_Tracker*` (`Alexander.Coath`) | 2022-06/07 | complete |
| CRRDA | `*TBMTracker*` | 2021-11/12 | complete, and Canada not Australia |

Notes for whoever repeats this:

- **The NEL layers are a trap.** Their schema is the closest thing to real
  telemetry in any Australian ArcGIS item — `Date`, `Chainage`, `percentage`,
  `length`, `Depth`, `X/Y/Z`, `segment_count`, `basename` (machine ids like
  `TUN-TBM-M011-SB-TRACKER`). But all three public views are **single-row
  snapshots** (one TBM per view, layers 15 and 16), so they can never give a
  rate, only a point reading. And two of the three views are older snapshots
  that still report M011 at 90.5% (`Date` 20260623) — sampling only those
  would make NEL look live. The authoritative `TBM_Tracking` view says both
  machines are at 100%.
- **WGTP is not Australia-relevant in practice.** `Alexander.Coath`'s
  `Chainage_Points` rows are blank CAD placeholders (`RefName`/`TunnelCode`
  are a single space), and the project is finished.
- **`Shape__Length` is not a distance travelled.** On the Metro Tunnel layers
  it is the length of the drawn polyline, ~27% longer than the chainage delta
  because the alignment is sinuous. Same drive on both bores: `|END_ - START|`
  = 580.0 m but `Shape__Length` = 734.9 m. If you ever need driven distance
  from a planning-style layer, sum `|END_ - START|` (chainage), not
  `Shape__Length`. NEL's `length` field *is* a driven distance and there
  `Shape__Length` tracks it.
- **CRS check:** CRRDA's `CompletedTBMLocations` uses `Easting_BCSG` —
  British Columbia Standard Grid. It surfaced on Australian-project searches
  and is easy to mistake for one.

## Snowy Hydro 2.0 — audited 2026-09-29

Same question as the section above but widened past TBMs: is there *any* live
status data for Snowy Hydro 2.0, of any kind?

**Answer: no live construction-progress data exists. There is no TBM tracker, no
progress dashboard, no progress layer — nothing that emits a machine-readable
project status. Do not write a progress exporter for it.** The one genuinely
live feed that touches the project is **daily reservoir levels**, and the only
2.0-specific connection is **Tantangara**, the upper storage of the pumped
scheme. That is operational water data, not construction progress — and it is
what `snowy_tantangara_exporter.py` exports, because it is the only live 2.0
signal that exists. See the [one live feed](#the-one-live-feed) below.

| Source | What it is | Last activity | Verdict |
| --- | --- | --- | --- |
| `getData.php` (below) | **daily JSON, includes Tantangara Reservoir** | **current to today** | **the one live feed — not progress** |
| Snowy 2.0 Virtual Tour | self-hosted Experience Builder app | config built 2023-09-04 | static 3D/360 tour |
| `Snowy2.0` AGOL org (14 items) | 6 `SHL_S2_*_PublicView` feature services | 2021-12-21 | static, 2021 content |
| `snowy2.maps.arcgis.com` | the tour's portal, hosts those services | as above | static |
| `snowyhydro.maps.arcgis.com` | separate org, urlKey `SnowyHydro` | — | no 2.0 content at all |
| `/snowy-20/progress/` | **a single YouTube embed and no text** | — | no data, do not bother |
| WP REST API `/wp-json/wp/v2/posts` | — | — | `401 rest_login_required` |
| `caportal.com.au/snowy/eis` | Mapbox static EIS map, no ArcGIS | — | static |
| `construction-progress` news category | 24 posts, prose | **monthly series ended 2025-03-28** | periodic |
| Transgrid Snowy 2.0 Connection | listed **Completed**, est. 2026 | — | finished, and not 2.0 proper |
| ANAO Report 39 2025–26 | 73% complete, $11.1b, at 31 Mar 2026 | 2026-06-18 | auditable snapshot |

### The one live feed

```
https://www.snowyhydro.com.au/wp-content/themes/snowyhydro/inc/getData.php
  ?yearA=2026&yearB=2026
```

Returns the whole year as JSON, keyed `year → snowyhydro → level[]`, one row
per day, each with a `lake[]` array of `-name` / `-dataTimestamp` / `#text`
(percent full). `Tantangara Reservoir` is one of three lakes. No auth, no
Referer needed, ~82 KB for one year.

Things that will bite you:

- **`Content-Type` is `text/html` even though the body is JSON.** Do not
  switch on it.
- **Errors are HTTP 400 with a plain-text body, not JSON.** A bare
  `json.loads` raises on the happy path's sibling:
  `Error: yearA and yearB must be valid integers`, and out-of-range years give
  `must be between 1954 and 2026`. The ceiling reported the *current* year when
  checked — if that is dynamic rather than hardcoded, a pinned `yearA=2026`
  starts 400ing in 2027. Re-check rather than assume.
- **`yearA` and `yearB` may be equal** — that is how you get one year. Omitting
  them is a 400, so the year is a required argument, not a default.
- **The live `script.js` and page assets live under theme `snowyhydro-v2`, but
  this PHP include is under the old `snowyhydro/` theme directory.** It is a
  leftover that the current theme still calls. If it 404s, that is why — it has
  already moved once.
- **The feed is daily, but the page prose says "weekly".** The 2026 series was
  272 gap-free daily rows through 2026-09-29, each stamped `T07:00`. Trust the
  data, not the copy.
- Snow depth rides in the same payload but is sparse (26 of 272 rows in 2026)
  and carries a `-quality` field (`"G"`). It is not a daily series.
- The `-` key prefixes and `#text` are XML-shaped keys leaking from an XML
  upstream. Do not "tidy" them; the values are read as-is.

Tantangara drawdown is real and 2.0-driven (intake works), so it is a usable
proxy for "is the 2.0 site active": 2026 ranged 7.00–16.14% full, sitting at
11.33 on 2026-09-29, −2.73 percentage points/week. That is the most
Snowy-2.0-specific live number that exists anywhere public — and it is a water
level.

### What the exporter does with it

`snowy_tantangara_exporter.py` fetches the last two years in **one** request and
reduces it locally, so the 7-day change and the year-to-date extremes cost no
extra round trip. Metrics, options, and the reasoning are in `README.md` in this
directory. Four things about this feed are worth restating here, because each one
is a silent-wrong-answer trap rather than a loud failure:

- **The year is derived from the clock, in Sydney time, on every scrape.** It is
  never pinned. See the `must be between 1954 and 2026` note above: a pinned
  `yearA=2026` starts 400ing in 2027. The year is also read at UTC+10 rather than
  in UTC, because at 10:00 UTC on 31 December the UTC year has already turned
  over while Sydney has not, and requesting the year the endpoint has not opened
  yet is a 400.
- **The `-dataTimestamp` is naive Sydney local time with no offset**, and the
  exporter pins UTC+10 rather than reaching for `zoneinfo` — `python:3.12-alpine`
  ships no tzdata, so `ZoneInfo("Australia/Sydney")` raises `ZoneInfoNotFoundError`
  in the container. The cost is ≤1 h of error during daylight saving, which does
  not matter for a once-daily series whose timestamp exists only to feed
  `time() - last_sample_timestamp_seconds`. The subtle part is that the offset
  must be attached *explicitly*: `datetime.timestamp()` on a naive value reads the
  **host's** local zone, so the same fixture yielded a different instant on the
  AEST dev host than in the UTC container until this was pinned and tested.
- **The 7-day delta uses date arithmetic, not a row offset.** If a day is ever
  missing from the feed, an index-based delta silently becomes a 6- or 8-day
  window and reports a wrong change with no visible sign of it. The exporter takes
  the newest reading *at or before* `latest - 7 days`.
- **Year-to-date extremes are filtered on the row's own year.** A 2025 low must
  not become the 2026 minimum. The obvious regression test cannot catch this,
  because in the real 2025 data every value sits inside the 2026 range — the test
  has to construct a series where a leak would be visible.

Units, one more time, because it is the easiest thing to get wrong here: the feed
is **percent of gross storage**, and the 7-day change is therefore
**percentage points**, not percent and not metres. No volume is published
anywhere on this endpoint. The project's own prose says "−2.73 m/week" for what
is the same 2.73 read as percentage points.

### The progress reporting that *was* there, and stopped

The `construction-progress` news category carried a monthly **SNOWY 2.0 PROJECT
UPDATE** with per-workfront percentages — caverns 35%, transformer hall 50%,
Tantangara intake, Marica surge shaft depth. It ran monthly from Aug 2023 and
**the last one is 28/03/2025**. Everything since is irregular milestone
announcements.

The percentages did not move to a better home. Whole-project completion is now
only in CEO quotes in news posts (67% at 03/10/2025) and in the ANAO audit
(73% at 31 Mar 2026). Note the ANAO *web page* renders that same figure as
`0%` — it is a JS count-up that has not been read by a crawler. Take the number
from the report PDF, not the page.

So the best available "status" is a hand-scraped number that changes a few
times a year. That is a note in a notebook, not an exporter.

### Traps hit while auditing

- **An AGOL org search is not scoped to the org.** Querying
  `snowyhydro.maps.arcgis.com/sharing/rest/search` unauthenticated silently
  returns the *global* index — `q=tunnel` gave 7724 results about KiwiRail and
  Rijkswaterstaat. The first result set looks authoritative and is entirely
  irrelevant. Only `q=owner:Snowy2.0` on `arcgis.com` is trustworthy, and it
  does return 14 items, all 2021–2022. Both `portals/self` and
  `community/users` return null ids unauthenticated, so the org id cannot be
  resolved to scope the search properly.
- **`editingInfo` is absent on the `SHL_S2_*` items and services**, so the
  freshness test used everywhere else in this file does not apply to them. Age
  them from the item's `modified` field instead (all 2021-12-21).
- **`/snowy-20/progress/` looks like the obvious source and is empty.** One
  YouTube iframe, 1659 bytes of markup, zero characters of text. Do not spend
  time on it.
- **The virtual tour is a downloaded app, not a live portal.** It is
  `isOutOfExb`, served from its own CDN, with `BASE_URL` placeholders left in
  the HTML. Its `config.json` binds exactly one data source, a web scene
  (`fb5c1ac3c2b5429cba851633e767fa8f`). There is nothing else in it to find —
  that is how you can rule the whole app out from one request.

## Exporter gotchas

### Shared

- The default `--listen-address 127.0.0.1` only works when something else
  shares the exporter's network namespace. On a bridge network it must be
  `0.0.0.0`, otherwise "works on curl localhost, dead from the container". Both
  exporters ship loopback as the default and rely on compose to override it.
- Never run a second, unmanaged copy of an exporter alongside the stack — it
  scrapes the same upstream. The pre-compose `~/start_*.sh` scripts that did
  exactly that have been deleted; don't recreate them.
- Metrics are documented in `README.md` in this directory. Alert on each
  exporter's own `*_scrape_success == 0` and on
  `time() - <last event>_timestamp_seconds`.

### `wht_tbm_exporter.py`

- **ArcGIS returns HTTP 200 with an error body.** A bad or stale service id
  (e.g. the pre-2026-09-29 typo in the table above) returns
  `{"error":{"code":400,"message":"Item does not exist or is inaccessible."}}`
  with status 200, so `http_get`'s status check passes. Guarded in
  `http_get_json` since 2026-09-29, which raises on any `"error"` key —
  **do not remove that check.** Locked down by
  `test_arcgis_error_body_raises_even_though_status_is_200`. Without it
  `describe_layer` reads
  `meta.get("fields") or []` → `[]` and returns all four field names as
  `None` without raising; that config gets written to `/cache/config.json`
  and the real failure resurfaces much later and confusingly in
  `latest_record` as an `orderByFields=None` URL. If progress metrics ever
  go blank, check the cached field names before suspecting the upstream data.
- The first `/metrics` request after a start is cold: it does full discovery and
  takes ~5-9 s.

### `snowy_tantangara_exporter.py`

- **`getData.php` answers HTTP 400 with a *plain-text* body, not JSON.** A bare
  `json.loads` on the happy path's sibling raises `invalid JSON` and throws away
  the only useful diagnostic:
  `Error: yearA and yearB must be between 1954 and 2026`. `http_get` therefore
  reads the error body off the `HTTPError` and folds it into the message —
  **the one deliberate divergence from the WHT exporter**, which reports only
  the status code. Verified live:
  `ScrapeError: GET ...getData.php?yearA=1900&yearB=2026 -> HTTP 400: Error:
  yearA and yearB must be between 1954 and 2026`.
- **`lake` may be a bare object, not a list.** The theme's own JS checks for
  both, because the same include is fed by an XML-shaped upstream that collapses
  a single child into one object. Iterating a dict directly yields its *keys*, so
  `lake_entries` normalises. A single-day row would otherwise produce nonsense.
- **Do not switch on `Content-Type`.** It is `text/html; charset=UTF-8` on a
  JSON payload, so a content-type check rejects the good response and accepts
  the bad ones.
- **The `snow` block is not a daily series** — 26 of 272 rows in 2026, with a
  `-quality` field. `daily_series` reads only `lake` and must keep doing so; a
  reservoir name that only ever appears in `snow` is correctly reported as
  "no readings found".
- **There is no `config_stale` metric and no cache volume, on purpose.** This
  exporter has no discovery step, so a `config_stale` gauge could never be `1`.
  Do not add one to "match" the WHT exporter, and do not add a persistent cache
  of past readings: a cached level served as a current one is exactly the
  dishonest failure mode the WHT exporter also refuses.
