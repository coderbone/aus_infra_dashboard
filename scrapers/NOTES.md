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

## OpenElectricity NEM batteries — audited 2026-09-30

Source: <https://api.openelectricity.org.au> (the successor to OpenNEM;
`opennem.com.au` and `api.opennem.com.au` are dead). Platform at
<https://platform.openelectricity.org.au>, keys shaped `oe_…`. There is **no
anonymous access**: `/data/*`, `/facilities/`, `/market/*` and `/me` all need a
bearer token; only `/v1/plans` and `/v4/social` are open.

`oe_battery_exporter.py` is built on this. Metrics and options are in
`README.md` in this directory; the things below are the upstream facts that
forced the design, and the traps that are still live.

### There is no state of charge. Anywhere.

The single most important fact, because everything else follows from it. The
data model publishes:

- `storage_battery` — stored energy in MWh, the actual measurement
- `capacity_storage` — registered capacity in MWh, on the **unit** in
  `/facilities/`
- `capacity_registered` — registered *power* in MW. Not energy. Using it as a SOC
  denominator silently produces a dimensionally wrong number that still looks
  plausible, because both are "capacity" and both are numbers near 1000.

There is no percentage, no full-charge flag, no `soc` field, no
`energy_full`/`energy_discharged` pair. So `oe_battery_soc_ratio` is
`energy / capacity` computed in the exporter, and any statement of the form
"OpenElectricity reports X% state of charge" is false. The derivation inherits
both series' error: if the metadata capacity is corrected, every historical SOC
reading the dashboard has already stored shifts with it.

### Traps

- **A facility returns three series and only one has capacity.** Eraring returns
  `storage_battery_ERB01`, `_ERBG1` and `_ERBL1`. Only `ERB01` appears in the
  metadata with a `capacity_storage`, because G1 and L1 are the metering points
  and the battery itself is `1`. Summing the three reports Eraring as holding
  1,686 MWh instead of 334 and inflates the monitored fleet by roughly 3x. The
  other two are counted in `oe_battery_series_without_capacity` (9 for the
  default top 10, not 0) and dropped.
- **`capacity_storage` is on the *unit*, not the facility.** It lives at
  `data[i].units[j].capacity_storage`, so the denominator has to be looked up per
  unit code. Read off the facility it is simply absent, and every SOC vanishes
  with no error anywhere.
- **The largest batteries in the country have never dispatched.** Ranking the
  top 10 by capacity over all 119 capacity-bearing units gives: Richmond Valley
  2200 (the largest in Australia), Tomago 2000, Baranduda 1886, Wooreen 1400,
  Elaine 1244, Western Downs 3 1220, Supernode 3 1217 — seven `committed`
  projects that 404 or return empty series, and not one of Eraring (1997),
  Waratah (1680), Orana (1660) or Collie 2 (1363), which are the batteries that
  actually have curves. The top-N is taken over units with a `data_last_seen`,
  which is 74 of 119. Note that `commissioning` is **kept** — Eraring 2 and
  Collie 2 are commissioning and both publish real data — so this is a data
  filter, not a `status_id` filter, and it will need revisiting if unbuilt
  batteries start publishing metadata before they dispatch.
- **WEM facilities 404 under NEM.** Ten of the battery facilities are `WEM`
  (Collie, Kwinana, Synergy). `GET /data/facilities/NEM?facility_code=COLLIE_BESS2`
  returns `404 No data available`, which is byte-identical to what a genuinely
  empty battery returns. That 404 was masking a routing bug during development
  and looked like a data problem for a while; the network is now carried per row
  from `network_id` and 404 is not retried.
- **The aggregate form is useless.** `GET /data/facilities/NEM?metrics=storage_battery`
  with no `facility_code` returns one flat array of timestamps with no series
  names — repeated timestamps, no attribution, nothing to join on. One request
  per facility is the only correct form, which is also what makes the request
  budget the interesting problem.
- **The feed is sparse, and that is not a fault.** As of 2026-09-30 values exist
  only for roughly 18:00–04:00 Sydney time and are null for the rest of the day.
  A 14:00 scrape correctly finds nothing new. Hence: newest non-null sample in
  the window, exported with its real timestamp and age, dropped only past 36h.
  A daytime reading being 10h old is the expected state, not staleness.
- **A null newest sample is not a zero.** If the last value in a series is null,
  that is no reading. The exporter omits the SOC series entirely rather than
  exporting 0, because on a battery a zero reads as "flat and empty".
- **The API 403s the default User-Agent.** `Python-urllib/3.12` → `403 Forbidden`
  with no body; `curl/8.5.0` → 200; `python-requests/2.31` → 200; any custom UA →
  200. It is a bot rule keyed on that exact string. `USER_AGENT` in the exporter
  is explicit for this reason and must not be "tidied" back to the default.

### The budget, and why the poll loop is not the scrape handler

`GET /v1/plans` (open) for the free Community plan: 500 credits/day, 2 req/s
burst, 2 years of history, 1 key, non-commercial. Academic is also free (2000/day,
5 keys) but needs an accredited institution's domain. The part that is easy to
miss is that the plan also carries **rate buckets: 8 requests / 5 min, 32 / 1 h,
366 / 1 day** — and 366/day is the binding constraint, not the 500 credits.

| Poll pattern | Requests/day | Verdict |
| --- | --- | --- |
| Scrape-driven, 5m, top 5 | 2880 | 8x over the daily bucket |
| Scrape-driven, 1h, top 10 | 240 | works, but ties the upstream to the scrape |
| Own loop, 1h, top 10 | 240 | works, and the scrape interval becomes free |
| Own loop, 1h, top 20 | 504 | over the daily bucket |
| Own loop, 2h, top 10 | 132 | works, at 12h of feed latency at worst |

So the exporter polls on its own schedule and serves the last completed cycle to
Prometheus. Scraping it every 5 minutes costs the API nothing; that is the whole
trick, and it is why `scrape_interval: 5m` in `../prometheus.yml` and
`--poll-interval=3600` in `../docker-compose.yml` are not a contradiction. The
cost of that design is that a stopped poll loop is invisible in the SOC panels,
which show the last reading forever — hence `oe_last_poll_timestamp_seconds` and
the `Since last poll` panel on the dashboard.

Credits themselves measured anywhere from 0 to 1 per narrow call, with a
five-request cycle moving the balance 494 → 491 and a twelve-request cycle
494 → 494 in an earlier run. Treat the credit gauge as a smoke alarm rather than
as a precise meter, and use `oe_api_requests_total` to reason about the rate
buckets. `/me` on its own is free, which is what makes the gauge affordable.

### The fleet as of 2026-09-30

119 capacity-bearing battery units, 74 of them with any upstream observation,
32,085.47 MWh total. 109 units NEM, 10 WEM. By status across all 119:
47 `committed`, 64 `operating`, 8 `commissioning`. The top of the observed list:

| Facility | Unit | MWh | Network | Status |
| --- | --- | --- | --- | --- |
| ERB | ERB01 | 1997 | NEM | operating |
| WTAHB | WTAHB1 | 1680 | NEM | operating |
| ORABESS | ORABESS1 | 1660 | NEM | operating |
| ERB2 | ERB02 | 1390 | NEM | commissioning |
| COLLIE_BESS2 | COLLIE_BESS2 | 1363 | WEM | commissioning |
| COLLIE_ESR4 | COLLIE_ESR4 | 1200 | WEM | operating |
| COLLIE_ESR5 | COLLIE_ESR5 | 1200 | WEM | operating |
| STABESS | STABESS1 | 1200 | NEM | commissioning |
| SNB02 | SNB02 | 1090 | NEM | operating |
| LDBESS | LDBESS1 | 1086.2 | NEM | operating |

The three Collie units are in scope at the default `--top 10` and publish
**nothing** — no non-null value in a 30-day window. So a strict top-10 yields only
7 monitored batteries, and `oe_batteries_monitored` sitting below
`oe_batteries_in_scope` is not a fault. The idle rotation is the fix rather than
raising `--top`: after 36h each Collie slot goes to the next-largest candidate,
costs no extra request, and the scope stays full. Checked WEM candidates
(`KWINANA_ESR2`, `COLLIE_ESR1`) are *also* all-null, while the NEM candidates
(`MREHA3`, `MLB01`, `TARBESS1`, `WOOLES1`, `SNB01`) all publish — so the rotation
walks past the WEM units rather than into them.

Note that `data_last_seen` does not distinguish these: all three Collie units
report a current `data_last_seen` and still return 1151 null points. Liveness has
to be learned by polling — and once learned it has to be *persisted*, or the
first sight of an unreadable unit stamps it as fresh on every start and a
container that restarts more often than the 36h window never demotes anything.
`liveness.json` in the cache volume is the fix, and it is liveness only: no
reading or SOC value is ever written to it, so a restart still cannot serve a
stale SOC as current.

Verified across a real `docker compose up --force-recreate`: the three Collie
units' `oe_battery_idle_seconds` continued from 36.8s to 78.3s instead of
returning to ~0.

### `oe_battery_exporter.py`

- **Do not hold the scraper lock across the network work.** `/metrics` has to
  stay answerable while a cycle of N requests is in flight, and `load_fleet()`
  takes the same lock — so a plain `threading.Lock` held across the poll
  deadlocks on the second cycle. The lock is only for publishing state. This was
  a real bug, caught by hand before the tests existed; `threading.Lock` is not
  reentrant and `RLock` would paper over it rather than fix the intent.
- **The disk fleet cache is a fallback, not a source of truth.** It is only read
  when the API cannot be reached, and its `cached_at` is carried over as the
  refresh time. Reading the cache *before* trying the API — which is the obvious
  way to write it — means that once a cache file exists the daily metadata
  refresh never happens again, and a capacity change upstream is never picked up.
  Locked down by `test_stale_cache_does_not_pin_the_fleet`.
- **Count requests when they are issued, not when they answer.** The budget is
  spent by the request; a counter incremented only on success under-reports
  exactly the 404s and 5xx that a retry budget is there to absorb.
- **Never add an `--api-key` argv flag.** The key is read from
  `--api-key-env`/`--api-key-file` so it cannot reach the process table, shell
  history, or a `docker inspect` command line. Compose passes it as an
  environment variable, which is also why `docker compose config` now renders it
  in the clear — treat that command's output as secret.
- **`oe_battery_scrape_success` carries the same five labels as the reading
  metrics, not a reduced `{facility,unit}` set.** This is for the dashboard: one
  Grafana variable has to select the same batteries in every panel, and the table
  joins on `unit`. A reduced set matches `{name=~"$battery"}` in nothing and
  leaves the join key absent. Locked down by
  `test_every_per_battery_metric_shares_one_label_set`.
- **Prometheus may be scraping before the first poll has finished.** `/metrics`
  answers 200 with every value `NaN` and `/healthz` says healthy, so a container
  mid-first-poll is not reported as down and a `depends_on: service_healthy`
  gate does not deadlock. `NaN`, not 0: a fleet count of 0 claims an empty fleet.
