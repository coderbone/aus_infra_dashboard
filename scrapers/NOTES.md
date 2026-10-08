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
- `power` — charge/discharge power in MW, negative charging, positive discharging
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

### `power` exists, and it is not on the same clock as `storage_battery`

Audited 2026-10-01, which turned a one-metric exporter into a two-metric one at
no cost to the daily request budget.

- **Both metrics come back in one request** by repeating the parameter:
  `?metrics=storage_battery&metrics=power&facility_code=ERB&...`. The budget is
  counted in requests, not metrics, so a second call would have doubled
  313/day for a number that is free to collect alongside. The client's `get()`
  takes a list of metrics for exactly this reason.
- **The response is one block per metric**, each with its own `unit` and its own
  timestamps: `{metric, unit, unit_type, series: [...]}` where a series is
  `[timestamps, values, {unit_code}]`. That third element is the reliable
  attribution mechanism. `power_ERB01` would also have worked, but
  `columns.unit_code` does not depend on the naming convention holding.
- **The unit is declared, not guessable.** The `power` block carries
  `unit: "MW"`. An earlier draft of this file claimed the response had no unit
  field and that MW was inferred from magnitude; that was wrong. The exporter
  checks the declared unit against the one it expects and warns on a mismatch,
  which is the part worth having — a silent unit change upstream would otherwise
  arrive as a 1000x error in a chart.
- **Nothing upstream documents the sign.** It was read off the data and is
  consistent: charging negative, discharging positive, matching a bidirectional
  `dispatch_type`. It is stated in `# HELP` and on the panel rather than left to
  be inferred from the chart.
- **The two feeds are hours apart, by design.** On 2026-10-01 at 11:53 the newest
  Eraring energy sample was `2026-10-01T04:00+10:00` and its newest power sample
  was `2026-10-01T11:00+10:00`. `storage_battery` publishes only overnight
  (~18:00–04:00); `power` publishes through the day. Therefore:
  - Each metric carries **its own** timestamp and age. One shared clock would
    date a 7-hour-old SOC as current.
  - `oe_battery_scrape_success` stays tied to **energy**, so the dashboard's
    "batteries with a reading" means what it says instead of being propped up by
    a live power feed.
  - **Liveness takes the newer of the two.** Judged on energy alone, every
    battery would rotate out of scope each morning and back in each evening. A
    unit visibly dispatching on power is not idle.
- **The three-series trap repeats per metric.** G1 and L1 exist for `power` too,
  so a two-metric facility returns six series, four of them uncappable. That is
  why `oe_battery_series_without_capacity` is ~37 at top 12 rather than the ~13
  it was with one metric.
- **A facility with more than one real unit** returns both units' series in one
  block. Taking the newest sample across the whole block attributes one unit's
  reading to the other; samples must be keyed to the row's own unit code. This
  predates `power` and was latent until a combined response made it reachable.

### Traps

- **A facility returns three series per metric and only one has capacity.**
  Eraring returns `storage_battery_ERB01`, `_ERBG1` and `_ERBL1`. Only `ERB01`
  appears in the metadata with a `capacity_storage`, because G1 and L1 are the
  metering points and the battery itself is `1`. Summing the three reports
  Eraring as holding 1,686 MWh instead of 334 and inflates the monitored fleet
  by roughly 3x. The other four (two metrics x two metering points) are counted
  in `oe_battery_series_without_capacity` (37 for the deployed top 12, not 0)
  and dropped.
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
| Own loop, 1h, top 10 | 265 | works, and the scrape interval becomes free |
| Own loop, 1h, **top 12 (shipped)** | **313** | inside every bucket |
| Own loop, 1h, top 20 | 504 | over the daily bucket |
| Own loop, 2h, top 10 | 132 | works, at 12h of feed latency at worst |

Each row is data requests plus the free `/me` credit check per cycle, plus the
once-daily fleet metadata refresh. Requests per cycle, not metrics per cycle, is
the unit that matters: asking for two metrics in one call is one request, which
is why `power` was free. 12 in scope + 1 `/me` = 13 hourly, `13 x 24 + 1 = 313`
per day, or one request per 4.6 min. `--request-interval=0.6` holds the 2/s
burst limit.

So the exporter polls on its own schedule and serves the last completed cycle to
Prometheus. Scraping it every 5 minutes costs the API nothing; that is the whole
trick, and it is why `scrape_interval: 5m` in `../prometheus.yml` and
`--poll-interval=3600` in `../docker-compose.yml` are not a contradiction. The
cost of that design is that a stopped poll loop is invisible in the SOC panels,
which show the last reading forever — hence `oe_last_poll_timestamp_seconds` and
the `Since last poll` panel on the dashboard.

Still unknown, and only observation will settle it: the plan also advertises a
`7d:366` bucket whose meaning is not documented, and the 5m/hourly buckets have
not been seen to trigger at this cadence.

Credits themselves measured anywhere from 0 to 1 per narrow call, with a
five-request cycle moving the balance 494 → 491 and a twelve-request cycle
494 → 494 in an earlier run. Treat the credit gauge as a smoke alarm rather than
as a precise meter, and use `oe_api_requests_total` to reason about the rate
buckets. `/me` on its own is free, which is what makes the gauge affordable.

### The resolution lever is free; the polling lever is not

Audited 2026-10-02, prompted by a reasonable suggestion: poll every 15 minutes
instead of hourly to cut inferred-SOC error. The answer turned out to be the
opposite - and more useful.

- **Polling faster is genuinely pointless.** At the shipped `interval=5m` the
  upstream series is already fully resolved, so two fetches 100 seconds apart
  return byte-identical data with zero new points. Every gap across a 48h window
  is exactly 5 minutes. There is nothing between polls to observe.
- **The request budget is the wall, and we are near it.** `oe_api_requests_total`
  reads **14 per cycle** (12 in scope + `/me` + fleet metadata on its cycle),
  i.e. **336/day against the 366/day bucket**. A 15-minute loop would be
  **1344/day — 3.7x over** — and 48/hour against a 32/hour bucket. Credits are
  the *looser* limit (~60 of 500/day), which is why they are the wrong number to
  watch; see the budget section above.
- **Finer upstream data is free, and it is much better.** `interval` is a query
  parameter on the *same* single request, so `5m` costs exactly what `1h` costs
  — same endpoint, same request count, same credits. And the hourly value is
  only the arithmetic mean of the twelve 5m samples beneath it (confirmed to
  1e-6 across 119 hours), so the hourly series is the *lossy* view and asking
  for `5m` simply stops throwing the intra-hour shape away. Integrated with the
  shipped `infer_soc`, over four days:

  | Horizon | 1h power | 5m power |
  | --- | --- | --- |
  | 1h | 0.82% | **0.40%** |
  | 4h | 1.63% | **1.11%** |
  | 6h | 2.12% | **1.47%** |
  | 8h | 2.73% | **1.82%** (saturation 2% vs 9%) |
  | 14h | 8.64% | **5.12%** |

  Better at every horizon, on 7/7 units, for zero requests. **Shipped as
  `--api-interval=5m`.**

- **Per-unit efficiency coefficients do not generalise, so they do not ship.**
  The model is linear in the coefficient (dE = A + eff·B), so it was fitted in
  closed form per unit over five days (~50–225 windows each) and validated
  leave-one-day-out. The fitted values are physically impossible — two units
  want more than 1.0 (you cannot store more than you draw) and two go negative.
  Leave-one-day-out moved the fleet from **1.47% → 1.42%**, a 3% gain, and was
  *worse* than a flat 0.9 on 3 of 7 units. Refitting the single global
  coefficient from scratch lands at 0.81 and buys 1.71% → 1.69%.

#### What the residual actually is

Decomposing it over 1365 windows (actual minus modelled, `--infer-charge-efficiency=0.9`):

| Quantity | Value |
| --- | --- |
| Mean residual | −6.9 MWh (−0.49% of capacity) |
| corr(residual, discharge energy) | **−0.46** |
| corr(residual, charge energy) | −0.13 |
| corr(residual, mean power) | +0.34 |
| Least-squares scale error on discharge | **−5.9%** |
| Least-squares scale error on charge | −8.6% |

The dominant systematic term is on the **discharge** side — the side
`stored_energy_delta` deliberately does *not* discount. But correcting it is
nearly pointless: a two-parameter fit that removes both scale errors cuts mean
abs residual by only **2%** (24.99 → 24.37 MWh). The error is not a scale
problem, which is why no coefficient tuning moves it. **Do not spend more effort
on coefficients.**

By hour of day the residual is also not flat: **+3.6% of capacity for windows
centred on 08:00 UTC** (18:00 Sydney, the first reading of each overnight window)
settling to about −1% for the rest of the night. One candidate was checked and
**ruled out**: the obvious suspect is a leading gap, since `infer_soc` begins
integrating at the first power sample at or after the anchor, so a missing
leading segment would be silently treated as no movement. It does not happen —
across 385 anchors the first power sample lands at the anchor timestamp in
**385/385 cases**. So whatever produces the 18:00 spike is not that, and it
remains unexplained.

#### The solar co-location hypothesis — a thread, deliberately left thin

An earlier draft of this note proposed that the residual might be explained by
solar contaminating the `power` metric. **I do not believe this, and the reason
is worth recording so it is not re-litigated.**

The idea is not crazy in the abstract: `power` is facility-level, and if it is
metered at the connection point rather than at the battery terminals, then for
any site with co-located solar the series is `battery + solar`, and integrating
it would move energy that the battery never saw. Collie and Kwinana are known
solar-plus-storage sites in reality, which is presumably why the idea occurred.

**The evidence is against it.** `/facilities/` returns `fueltech_id` per unit,
and all 12 polled units are `fueltech_id="battery"`. More telling, **no facility
in the entire battery fleet registers a single solar unit** — including Collie
and Kwinana, which unquestionably have solar. So the registry is *known* to be
incomplete on precisely this question; it cannot rule co-location in or out on
its own. What does rule it out as a *general* explanation is that the discharge
residual appears identically at ERB01, WTAHB1, ORABESS1, STABESS1 and LDBESS1 —
Eraring, Waratah, Hornsdale, Torrens and Lake Dartmoor, all standalone grid
batteries with no solar on site. A contamination that requires solar cannot
explain error at a site that has none.

It remains worth *one* cheap check if someone wants to close it out: pick a
battery with known co-located solar and compare its residual against the
standalone fleet above. If it is not an outlier, the thread is dead and should
be deleted rather than carried. Until then it is a hypothesis, not a finding,
and it should not be cited as an explanation for the residual.

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

The three Collie units were in scope at the original `--top 10` and publish
**no energy at all** — no non-null value in a 30-day window. So a strict top-10
yields only 7 batteries with a reading, and `oe_batteries_monitored` sitting
below `oe_batteries_in_scope` is not a fault. Adding `power` did not fix that,
because these units publish no power either; they are simply empty on both
metrics. The idle rotation is the fix rather than raising `--top`: after 36h
each Collie slot goes to the next-largest candidate, costs no extra request, and
the scope stays full. Checked WEM candidates
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
- **The two metrics are tracked as two independent readings, not one reading
  with extra fields.** `_record_readings()` is called per metric, each with its
  own timestamp and its own freshness test, and only the newest of the two
  feeds the idle clock. The tempting shortcut — one "last seen" and a power
  value hanging off it — produces a chart that looks fine and reports a 7-hour-old
  SOC as current every afternoon. Locked down by
  `test_each_metric_carries_its_own_timestamp`.
- **Name collisions in the internal row are a metric-collision risk, not a
  style issue.** The fleet row already had `power_mw` meaning *registered MW
  capacity* from `capacity_registered`. Adding instantaneous `power` power under
  the same key silently overwrote one with the other and neither sum was
  exported, so the bug produced a missing metric rather than a wrong one. The
  row field is now `registered_mw`.
- **Inferred SOC hands over on _freshness_, not on _presence_.**
  `storage_battery` publishes overnight, so its 04:00 reading is still in the
  lookback window at midday, still counted in `oe_battery_scrape_success`, and
  hours out of date. Keying inference on `scrape_success` suppressed it for
  exactly the daytime hours it exists to fill — the feature was inert and the
  tests were green because both fixtures were built around an overnight
  timestamp. `--infer-fresh-hours` is what decides handover;
  `test_measured_reading_hands_over_to_inference_when_it_goes_stale` pins it.
- **`--enable-inferred` needs the whole power series, which used to be thrown
  away.** `battery_metrics()` collapsed each series to its newest point, which
  is right for "SOC now" and leaves nothing to integrate. Hence `full_series`.
  It costs **no extra requests** — the response already carried the window and
  the exporter was discarding most of it. `poll_cycle` passes it only when
  inference is on, so the default path is unchanged.
- **A per-point counter multiplied by the window length.** With `full_series`,
  `oe_battery_series_without_capacity` read 739 instead of 37, because the G1/L1
  series are now counted once per *point* rather than once per *series*. Fixed
  with a set keyed `(facility, unit, metric)`. Pinned by
  `test_series_without_capacity_counts_series_not_points`. Worth remembering
  that any counter incremented inside the per-point loop is now suspect.
- **Gaps: integrating across a hole invents energy.** The docstring claimed gaps
  were not bridged while the loop happily trapezoided between points 10h apart.
  Now a segment wider than `--infer-max-gap-hours` ends the integration rather
  than being bridged — and it *stops*, rather than resuming afterwards, since
  the battery's state on the far side is unknown. One missing hourly point is
  normal and still integrated; a real outage is not.
- **Accuracy, measured 2026-10-01 against the live feed** (integrating from one
  measured reading to the next, 7 units):

  | Window | n | Median | Mean |
  | --- | --- | --- | --- |
  | 1h | 140 | 0.25% of capacity | 0.81% |
  | ~14h | 7 | 10.4% of capacity | 7.4% |

  (with `--infer-charge-efficiency=0.9`; see the loss-term note below)

  Short windows are excellent; the full daytime gap is not. The drift is
  systematically one-directional — the integral **over**-predicts stored
  energy, consistent with conversion losses being ignored.
- **Two different loss coefficients, one kept and one rejected.** Worth keeping
  distinct, because the difference is the whole point.

  *Rejected:* a symmetric throughput loss, `-f·∫|P|`, which discounts charging
  and discharging identically. It was fitted against the live sample and
  improved the 14h case by ~3 points while degrading the 1h case — it was
  fitting 7 samples rather than a physical parameter, and a term that is wrong
  in the *right* direction on every short window is worse than no term.

  *Kept:* `--infer-charge-efficiency`, the share of grid-facing charging energy
  that reaches the cells, applied to **charging only** — the discharge side is
  already metered at the terminals, so there is nothing further to discount it
  by. This is asymmetric by construction, and that asymmetry is what
  distinguishes it from a fudge factor: it cannot be tuned to flatter a metric,
  it either moves the estimate toward the measured one or it does not.

  | Efficiency | 1h median | 14h median | 14h mean |
  | --- | --- | --- | --- |
  | `1.0` (no loss) | 0.3% | 14.3% | 11.3% |
  | `0.9` | 0.3% | 10.4% | **7.4%** |

  The acceptance test is the signature of a real physical term: it improves the
  long window *and leaves the short window alone*. A fitted coefficient is
  exactly the opposite. The value came from the operator, not from these seven
  samples, which is what keeps the test meaningful.

  Charging and discharging carrying different weights also requires splitting an
  interval at a zero crossing (`stored_energy_delta`), because averaging across
  the switch point would apply the charge discount to part of a segment that was
  actually discharging.

  What this still does not fix is a *per-unit* constant: cells, inverters and
  auxiliary loads differ by unit and by site, so one fleet-wide number is a
  simplification. The 14h error remains too large to publish over the full gap,
  which is why `--max-infer-hours=6` stays.
- **The clamp was hiding a real disagreement.** Waratah read exactly `1.0`
  because the raw integral ran ~97 MWh past its 1680 MWh capacity. A clamped
  value is indistinguishable from a genuine full battery, so
  `oe_battery_inferred_saturated` reports it (and is flagged even with
  `--no-infer-clamp`). Saturation across a fleet is what a units or sign bug
  looks like, so it is worth an alert rather than a silent clamp.
- **Not persisted on purpose: the inference anchor.** `_last_measured_*` is not
  written to `liveness.json`, which stores liveness only — no reading, no SOC,
  no energy. That is deliberate: a restart must never serve a stale SOC as if it
  were current. Recovery is free instead, because the measured reading is still
  in the API's lookback window, so the first poll after a restart re-establishes
  the anchor on its own.
- **The gap is ~18h; integration is good for ~6h. That mismatch is the finding.**
  Drift from the anchor, live, 7 units: 0.9% at 1h, ~2% at 2–3h, 4.2% at 5h,
  11.5% at 6h, 35% at 8h, 44% at 10h — and by 10h three of seven have run past
  capacity entirely. A first deployment of `--max-infer-hours=26` "worked" in the
  sense that it published 7/7 inferred values, and 3 of those 7 were a clamped
  `1.0`: a chart saying three batteries are full when they are not. Setting the
  cap to where the numbers are still good means inference now covers roughly
  05:00–10:00 after the overnight reading and publishes nothing for the rest of
  the day. That is a smaller win than the feature was pitched as, and it is the
  honest one. Anyone re-tuning this should re-derive the drift curve before
  raising the cap, not assume a longer window is free.
- **A published metric nobody can trust is worse than a gap.** The reason
  `--max-infer-hours` returns `None` past its cap rather than extrapolating is
  the same reason unread series are omitted rather than zeroed: on a battery, a
  plausible wrong number reads as a real observation.

## Inferred SOC mirrors measured SOC — 2026-10-03

The inferred family used to be published **only** where no measured reading was
fresh, so it had gaps by construction and the two families never overlapped.
That was a deliberate honesty rule ("Measured always wins … so there is never a
moment when the two families both claim the same cycle"). It has been reversed:
a unit whose reading is still inside `--infer-fresh-hours` now publishes that
reading **as its own estimate** — if the feed says X, the estimate is X. The
family is continuous, and the daytime gap it exists to cover is now bounded by
when upstream stops publishing rather than by when the last reading stops being
true.

Motivation was the dashboard: `Energy stored, GWh` and the SoC bargauge had to
be written as `inferred or measured` fallbacks, because the inferred series was
genuinely absent for hours at a stretch. Live Prometheus over 48h at the time:

```
oe_battery_energy_stored_mwh    present at all 85 sample points
oe_battery_energy_inferred_mwh  present at only 34
                                (10-02 07:33–10:03, 15:33–19:33, 10-03 12:33–22:03)
```

So "switch the panel to inferred" and "leave it blank for a third of the day"
were the same change. This removes the need for the fallback entirely.

### Things that are not obvious about it

- **A copy cannot come out of `infer_soc()`.** It returns `None` on an empty
  power series (`:1133`), needs ≥2 points (`:1149`), and clamps both `soc` and
  `energy_mwh` (`:1202`, `:1206`). A fresh anchor with real power samples also
  *moves* off the anchor via `hold_rate` (`:1189`). The copy is built by its own
  `_copy_of_measured()`, which reads `soc`/`stored_mwh` defensively and returns
  `None` if either is absent — a sample dict that reports a reading without an
  SOC is not worth crashing an exposition over, and two test fixtures were
  exactly that shape.
- **The render refresh had to be taught to skip copies.** `_refresh_inferred_for_render`
  re-runs `infer_soc` whenever a hold rate exists, which would have dragged a
  copy off the measurement it is reporting while leaving `inferred_ts` naming a
  power sample that was never used. `from_measured` marks the row and exempts it.
- **`_rows_for_newly_stale`'s `present` guard was a live bug, not a theoretical
  one.** It skipped any unit already holding a row. With copies, that meant a
  battery stayed frozen at its measured value for a whole poll interval after
  the reading aged out — reintroducing precisely the handover drift that method
  was written to fix. The guard now excludes copies, and the caller *replaces*
  rather than joins, since two rows for one unit is not something this family
  can express (Grafana reads it as two batteries). Both halves are pinned by
  `test_a_copy_is_replaced_by_an_integration_once_the_reading_goes_stale` and
  `test_upgrading_a_copy_replaces_only_that_unit`; both were mutation-checked.
- **`_rows_for_newly_stale` now runs on every scrape**, not only when
  `state["inferred"]` is empty. Normally it finds no candidates and costs a
  freshness test per unit. The exception is a unit that is stale but never
  integrable (no anchor, or no power) — it is retried on every 5m scrape rather
  than once an hour. No API calls, but it is no longer strictly poll-rate work.
- **`oe_batteries_inferred` deliberately still counts integrated rows only.**
  Counting every row would make it the monitored count under another name. What
  is worth knowing is how much of the fleet is actually being dead-reckoned, so
  the gauge keeps its old meaning and its old numbers.
- **The honest cost: the family no longer self-identifies.** An absent inferred
  value used to mean "measured is current, use that instead". It no longer does.
  `oe_battery_scrape_success` and `oe_battery_sample_age_seconds` still carry
  that distinction and `from_measured` exists internally, but nothing exported
  says which kind of row you are looking at. If that matters, a
  `oe_battery_inferred_from_measured` 1/0 gauge is the obvious addition.
- **There is a step at the handover.** A copy is pinned to the reading's value;
  the moment the reading goes stale the row becomes anchor + accumulated
  integral. That discontinuity is the size of the fresh-window integration, and
  the bargauge's `last_over_time(...[2h])` smears it over two hours.
- **`--infer-fresh-hours` changed meaning.** It no longer decides whether an
  inferred row exists, only whether the row copies or integrates. It is still
  the right knob — it is the line where the estimate stops being the reading —
  but every piece of prose describing it as a handover switch is now wrong.

### Documentation deliberately left stale

Left as-is at the user's request, to be swept if this is revisited. All of these
now describe the old behaviour:

- `oe_battery_exporter.py` — module docstring (`:134`, `:169-177`), the
  `oe_battery_soc_inferred_ratio` HELP (`:1369-1373`),
  `oe_battery_inferred_timestamp_seconds` HELP (`:1395`, "the newest power sample
  integrated" — none is, in a copy), `oe_batteries_inferred` HELP (`:1479`),
  and the `_compute_inferred` docstring (`:1755-1771`).
- CLI help — `--enable-inferred` (`:2671-2677`) and `--infer-fresh-hours`
  (`:2703-2710`, "replaced by inference").
- `README.md:31-44` — "Measured values are never overwritten", "inference covers
  roughly 05:00–10:00 and then publishes nothing".
- `scrapers/README.md:484-501` — "Measured always wins … no inferred value is
  exported at all"; `:705` for `oe_batteries_inferred`.
- `scrapers/NOTES.md` — `:661-668` (handover on freshness), `:734`,
  `:753-755` ("publishes nothing for the rest of the day"), and the
  `--max-infer-hours=6` claim at `:747-757`, which the deployed value of 48
  already outgrew.
- `docker-compose.yml:151-157`, `:165-175` — comments describing inference as
  filling the hours measured does not publish.
- `tools/build_battery_dashboard.py` — panel 1 (`:386-394`, "every other SOC
  panel is measured"), panel 8 bargauge (`:537-547` comment describing the now
  unreachable `or` fallback; `:553-556` "never published for the same battery at
  the same time"), panel 11 (`:671-674` "the two families never overlap", and
  `:680-683` explaining a dashed-line gap that no longer occurs), panel 13
  (`:763-768`, the paragraph about gaps that is now untrue), panel 15
  (`:816-819`).

Dashboard follow-ups if this is revisited: the `or` fallbacks on panels 8 and 13
are now dead code and can be simplified to the bare inferred metric; panel 11's
solid and dashed lines will coincide while a reading is current, which makes it
a better agreement check than a comparison; and the inferred-only panels no
longer need to explain their own gaps.

## A sign error in `stored_energy_delta` — found 2026-10-04, while calibrating

`stored_energy_delta` splits a power segment that crosses zero into a charging
leg and a discharging leg, and discounts the charging one. It discounted the
**first** leg unconditionally:

```python
charging   = -trapezoid(p0, t0, 0.0, cross_t) * charge_efficiency
discharging = -trapezoid(0.0, cross_t, p1, t1)
```

The first leg keeps `p0`'s sign and the second keeps `p1`'s, so which leg is
lossy depends on the direction of travel. As written:

| direction | first leg | applied | result |
|---|---|---|---|
| charge → discharge (`p0<0`) | charging | efficiency | correct |
| discharge → charge (`p0>0`) | discharging | efficiency | **inverted** |

For the inverted direction the two legs came out unweighted and
wrongly-weighted *respectively*, so the total came back as the exact negative
of the right answer. 120 MW discharging for an hour then 30 MW charging for an
hour reported **+3 MWh gained** where the truth is **−48 MWh lost** — a 51 MWh
error on one hourly segment, on a 1997 MWh battery.

It survived because the correct direction is the common one. A battery ramping
into dispatch is charge → discharge, and that path was always right. The broken
path is what happens when dispatch *ends* and the battery goes back to charging,
which is every ordinary evening. It was not an edge case.

It also cancelled. A day with as many `+→−` transitions as `−→+` transitions nets
roughly to zero, so fleet-aggregate SOC looked plausible throughout. The
existing test `test_a_segment_crossing_zero_is_split_at_the_crossing` covered
only the charge → discharge direction, and passed throughout.

Fix is a two-line branch on `p0 > 0`. Verified by identity against an
independent reference implementation over 1458 combinations of sign, efficiency
and interval length, and by asserting the physical invariant that a
charge/discharge cycle must *lose* energy (`test_charging_after_discharging_loses_energy_not_gains_it`).
All 497 pre-existing tests passed unchanged, so nothing had encoded the buggy
behaviour.

**Lesson worth keeping:** a sign error that cancels is not a sign error you find
by looking at aggregates. It surfaced only because the calibration work needed
the two legs to be individually recoverable, and `charge_discharge_split` —
which recovers them by calling the function at efficiency 0 and 1 — returned
0/0 for every straddling segment. A function that returns a hard zero for an
ordinary input is telling you something about its caller.

## Calibrating `--infer-charge-efficiency` — 2026-10-04

`--infer-charge-efficiency=0.9` was a judgement call: taken from a backtest and
never re-examined against the fleet. The evidence to examine it already existed
and was unambiguous.

**Prometheus cannot be the source.** `oe_battery_power_mw` exposes only the
newest power value per scrape — 144 samples over 12h containing 12 distinct
values — so a Prometheus-side calibration measures the sampling, not the
batteries. It also cannot express the integral at all: no `integral()`, and
`sum_over_time()` is not a running total. This was confirmed by building
`tools/soc_inferred_experiment.py` and round-tripping a backfill through
`promtool tsdb create-blocks-from openmetrics` (5313 samples → 33 blocks → 35
series, all queryable) — the mechanics work, the *input* is wrong. Its
`--sweep` result of ~0.96–0.98 against the deployed 0.9 was input distortion,
not battery efficiency.

**So the calibration lives in the exporter**, where `_power_for_inference`
returns the merged series — the live API window plus `/cache/power-history.json`
over 14 days — which is the same series inference integrates.

A pair forms in `_record_readings` when a reading supersedes an earlier one: both
ends are published by the feed, so `e1 - e0` is real measured change, and the
power series between them says how much the grid delivered and took. That solves
for the coefficient instead of assuming it:

```
measured = efficiency x charged - discharged
implied  = (measured + discharged) / charged
```

`charged` and `discharged` are recovered by calling `stored_energy_delta` at
efficiency 0 and 1 — `-delta(0)` and `delta(1) + delta(0)` — rather than by
duplicating its zero-crossing logic. Two calls recover both legs exactly, and
the split stays defined in one place, so calibration cannot drift away from the
model in production. `test_the_split_inverts_stored_energy_delta_at_every_efficiency`
holds that by identity.

Pairs are keyed on `(unit, closing reading timestamp)`, so repeated polls
replace rather than double-count, and are pruned at 90 days — much longer than
the 14-day power history, because the pairs are the evidence and the window they
were measured over is deliberately deleted.

**Deliberately report-only.** Nothing in the exporter ever writes to
`--infer-charge-efficiency`. The evidence accumulates slowly on purpose, and
three gates stand between it and a number:

- `DEFAULT_CALIBRATION_MIN_PAIRS = 5`, applied **per window bucket** as well as
  overall. Five pairs of twelve hours and five pairs of two hours are not the
  same evidence; the short ones divide by the least energy and are the noisy
  ones.
- A candidate must improve the **longest** window and must not make the short
  window worse. This is the specific test the deployed coefficient was rejected
  for, and it is why errors are stratified rather than pooled — one mean over
  all windows shows a change that fixes long nights and wrecks short ones as an
  improvement.
- Sweep ties resolve to the value nearest the deployed one, and if the deployed
  value is itself tied for best the recommendation is simply **absent**. A
  symmetric grid ties constantly (every value between the truth and the flag is
  equally wrong), and without that rule the output is whichever member of the
  tie iteration met first — noise, reported as a finding.

Refusals are not silent: the reason travels in the summary, and both
`recommended_efficiency` and `recommended_efficiency: 0` are distinguished from
"nothing measured" by absence.

**What is refused, and why it is not just caution:**

- *Discharge-only pairs.* `charged` is zero, so this is not a small denominator
  but none — the common case, since a battery sits out a calm afternoon
  publishing readings all day and saying nothing about charging losses.
- *Truncated integrations.* Power stops early (a gap wider than
  `--calibration-max-gap-hours`) or never reaches the reading. Scored on part of
  its span, a pair reports a plausible number for an interval it did not cover.
  A little slack is allowed, because readings are stamped from their own
  timestamp and can lag the newest power sample.
- *Implausible implied efficiencies* (>1.0 means the battery stored more than
  the grid delivered). These are returned with `plausible=False` and **counted**
  rather than filtered, because a run of them is the signal that the readings
  or the power series disagree — filtering them out is how a broken feed looks
  like a healthy calibration.

**Cold start is `plausible_pairs 0` and an absent recommendation**, which is
what the live exporter published on first start with the feed still down
(`scrape_success=0` for all 12 units). Nothing is claimed until readings resume.

### Wiring notes

- Opt-in via `--efficiency-calibration`; without it the exporter emits *nothing*
  in this family. An earlier draft published the two fleet-wide metrics
  unconditionally, which emitted `NaN` for the pair count — a disabled feature
  answering the question the panel asks. Pinned by
  `test_nothing_is_published_when_calibration_is_off`.
- `_render_calibration` was carved out of `render` so the gate is a single
  `if calibration:` at the call site.
- The pair is recorded *before* `_last_measured_*` is overwritten, and
  `_record_readings` runs before `_absorb_power_history` — so the power series
  comes from the API's lookback window plus history absorbed on earlier polls,
  which covers the interval as long as `--lookback-hours` exceeds the gap
  between readings.
- Labels are captured on **every** `observe` call, including refused ones.
  Capturing them only on a scored pair means a unit whose pairs all predate this
  build exports without labels, and a metric without labels is invisible to
  every Grafana variable that selects a battery.
- `oe_battery_inferred_calibration_recommended_efficiency` is deliberately
  **unlabelled**: a recommendation is not a per-battery claim, and per-battery
  labels would mean several values that have to agree with each other.

### Live status at time of writing

`scrape_success=0` for all 12 units, so 0 pairs. There were ~3 informative 12h+
pairs in 72h of Prometheus history before the feed went quiet — below the floor,
correctly. The mechanism is live and verified to publish its cold-start state
honestly; it has not yet had real data through it.

## The energy feed stopped publishing — 2026-10-05

Not a lag and not a missed night: `storage_battery` has published **nothing**
fleet-wide since `2026-10-02T04:00+10:00`. Verified directly against the API for
ERB01, WTAHB1, LDBESS1 and SNB02 plus their `G1`/`L1` series — every one cuts off
at the same instant, so three nights (2→3, 3→4, 4→5) are empty. `power` over the
same request, same units, same window: fully populated to `2026-10-05T19:00`.

This is the *second* consequence of the overnight-only behaviour above, and it is
the one that broke the dashboard. The two are independent: a feed can be
overnight-sparse and healthy, or overnight-sparse and dead, and nothing in the
response distinguishes them except the age of the newest non-null point.

### Why inferred SOC disappeared entirely

Not because the anchor went stale — because **the reading was never fetched**.

`battery_metrics` takes the newest non-null sample *inside the requested window*
(`latest_sample`, `:602`), and the window is `now - lookback_hours`. At the shipped
`--lookback-hours=36` a reading 89h old was outside it, so the request that would
have returned it was never made. Chain:

```
36h lookback → Oct 2 reading outside the window
  → latest_sample() finds nothing → values has no "storage" key
  → scrape_success=0 → _record_readings never seeds _last_measured_*
  → infer_soc() returns None at :1162 (anchor_mwh is None)
  → oe_batteries_inferred 0, and no soc_inferred_ratio series at all
```

Raising `--max-infer-hours` alone cannot fix this, and did not: it was set to 48h
on 2026-10-03 for exactly this symptom and inference went dark again five days
later. The anchor has to exist before any number of hours will integrate from it.
`oe_battery_anchor_age_hours` was the metric that made this diagnosable — it
stayed absent while every other inferred metric was absent too, which is the
signature of "no anchor" rather than "anchor too old".

### The fix: three flags, moved together

| Flag | Was | Now | Why it is needed |
| --- | --- | --- | --- |
| `--lookback-hours` | 36 | **192** | fetches the reading at all |
| `--max-sample-age` | 129600 | **691200** | `:950` otherwise discards it as stale on arrival |
| `--max-infer-hours` | 48 | **192** | `:1166` otherwise refuses to integrate across the gap |

192h is not arbitrary — it is the API's ceiling. `interval=5m` accepts a range of
**8 days maximum**; 9 days returns `HTTP 400 "Date range too large for 5m
interval. Maximum range is 8 days."` So `--lookback-hours=336` is not a larger
window, it is a 400 on every request and a silently empty exporter. `interval=1h`
goes to 32 days, so reach and accuracy trade against each other here and 5m was
kept, because 5m is the larger accuracy lever (`README.md` §error table).

**Cliff, dated:** the anchor is `2026-10-02T04:00+10:00`. When it passes 192h —
about **2026-10-10** — it leaves the window and inferred SOC goes dark again for
the identical reason. The 14-day power cache still holds every sample needed to
integrate it; only the *fetch* is short. Fixing it then means `--api-interval=1h`
(32d window, roughly twice the error), not a bigger `--power-history-days`.

### What the estimates are worth

Reproduced independently: integrating the raw API `power` series for ERB01 from
the anchor over 88.5h at `--infer-charge-efficiency=0.9`, unclamped, gives
**591.33 MWh** — the exporter publishes `oe_battery_energy_inferred_mwh 591.332`
and 29.611%. So the arithmetic is doing what it claims.

What that is worth is a different question, and the answer is: not much, yet.
88.6h is one integral from a single anchor, and `README.md`'s own measured drift
is ~1.5% of capacity at 6h and ~5% at 14h — extrapolated, that is well past any
of it by four days. `oe_battery_inferred_saturated` is 0 for all 7 units, so
nothing is pinned at a bound and the raw integral never left [0, capacity]; the
clamp is not doing any work here and is not what is keeping these in range.

The binding constraint on the *meaning* of the number is the denominator, not the
integration. `oe_battery_capacity_storage_mwh` for ERB01 is **1997 MWh**, which is
not a battery size anyone recognises — Eraring 1 is a ~470 MW / 940 MWh
installation. If that registered capacity is the facility total rather than the
unit's, then every SOC percentage for ERB01 is a fraction of the wrong thing, and
no amount of integration accuracy recovers it. This is the same trap
`capacity_registered` vs `capacity_storage` sets out in the notes above, one
level further in, and it is worth confirming against the facility record before
these seven numbers are read as percentages of anything.

### The inferred line was flat because the hold was disconnected

Found immediately after the flags above were changed: with inference live on 7
units, Liddell sat at exactly 483.417 MWh while discharging at 156 MW.

**The integration was correct.** Replaying the cached power offline through
`infer_soc` gives 658.9 MWh at 20:18, 511.4 MWh at 21:18, 486.5 MWh at the
newest sample — a clean ~147 MWh/hour fall. Nothing was wrong with the maths,
the sign, the efficiency term or the clamp.

The value was frozen because `infer_soc` is only ever exact up to the newest
power sample it was given, and that sample arrives with the **hourly poll**.
Between polls `now` advances but `used_ts` does not, so every 5m Prometheus
scrape republished the same number while `oe_battery_inferred_age_seconds` grew
0.23 → 0.31 → 0.48h. That is the whole bug: a battery moving at 150 MW looked
identical to one at rest, because the hold that was supposed to carry the last
observed rate across the gap was never running.

`hold_rate=None` was hardcoded at both call sites — `:2216` in
`_compute_inferred` and again inside `_refresh_inferred_for_render` — while
`_hold_rate` (`:2426`) sat unused directly above them. Fixed to pass
`self._hold_rate(series, now)` at both. `oe_battery_inferred_hold_hours` was also
being overwritten with a literal `0.0` in the refresh path, discarding the
figure even once a hold did occur; it now carries `again["hold_hours"]`.

Verified live after restart: Liddell 374.31 → 361.75 MWh over the five minutes
between two Prometheus scrapes, i.e. **2.512 MWh/min = 150.7 MW against a
reported 150.76 MW**, with `oe_battery_inferred_hold_hours` stepping 0.23 → 0.31
in step. `--infer-max-hold-hours` is no longer inert.

### Resolved: always integrate, never copy a fresh reading

Decided 2026-10-05 — **inferred is computed from power whenever an anchor and a
power series exist, and `--infer-fresh-hours` no longer gates that**. The test
suite had encoded both answers:

- `ZeroOrderHoldTest` (3 tests) wanted a reading inside the freshness window
  published verbatim as a *copy* (`from_measured=True`), so the dashed line would
  sit exactly on the measured one and only diverge as it aged.
- `ScraperTest` (3 tests) wanted it integrated anyway — its comments had already
  been rewritten to say so explicitly.

These could not both hold. Always-integrate won, on three grounds:

1. A copy makes the two series carry the same number for different reasons at the
   same moment: it lies on the measured line while claiming to be an estimate,
   then bends away from it as it ages, so the handover shows a kink in the chart.
2. The copy has to be replaced later regardless, since it stops being the best
   estimate as the reading ages. That replacement is a second code path plus a
   window where the published value is up to a full poll interval out of date —
   the `oe_batteries_inferred` counting ambiguity and the `_rows_for_newly_stale`
   pass both exist only to serve it.
3. `inferred_ts` on a copy names the *measurement*, not a power sample, so
   `oe_battery_inferred_age_seconds` would report the age of a value that was
   never integrated — the age metric describing work that did not happen.

The 3 copy tests were rewritten rather than deleted, keeping the invariants that
were actually about correctness:

- `test_an_integrated_row_keeps_moving_as_the_reading_ages` — was
  `test_a_copy_is_replaced_by_an_integration_once_the_reading_goes_stale`. There
  is no handover to get wrong now, so it asserts the refresh advances the value
  and that `inferred_ts`/`inferred_age` move with the integral.
- `test_refreshing_one_unit_neither_duplicates_nor_drops_the_other` — was
  `test_upgrading_a_copy_replaces_only_that_unit`. The one-row-per-unit invariant
  it protected still matters (two rows read as two batteries in Grafana), so it
  is kept, with the two units given *different* power so a leaked series or a
  single-unit rebuild fails on a wrong value rather than only on a wrong count.
- `ClientTest.test_a_fresh_measurement_is_still_integrated_rather_than_copied` —
  asserts `from_measured` is false and that the scraper's own anchor wins over
  the sample's claim, which pins which anchor the row is built from.

`_measured_is_current`, `_copy_of_measured` and the `from_measured` branch in
`_refresh_inferred_for_render` are now **retained but dead**, and marked as such.
They are kept so the alternative stays reversible, but switching it back on is not
a one-line change: it is that branch plus these tests plus the metric HELP text,
which documents the always-integrate behaviour. Treat them as a record of the
rejected design rather than a dormant feature.

### A real bug found while settling it: stale `inferred_ts` after a refresh

`_refresh_inferred_for_render` re-ran the integral and copied back `soc`,
`energy_mwh`, `saturated` and `hold_hours` — but **not** `inferred_ts`. The age
restamp above it therefore measured from the *poll-time* timestamp while the
energy had been integrated through later samples, so `oe_battery_inferred_age_seconds`
described a different timestamp than the one the number came from: two
disagreeing clocks inside one row, which is the precise symptom that method
exists to remove.

Cause: samples that were future-dated at poll time are correctly not integrated
then (`nothing past now is integrated`, so a future sample cannot pull the
estimate forward), but become integrable as the clock advances to them. Between
scrape and poll the set of integrated samples genuinely changes, so the stamp has
to move with the value.

In production this is inert — a poll's power series ends at the poll clock, so the
integral endpoint is the same on every scrape and only the hold advances. It
surfaced only because the test fixture carries samples an hour past the poll
clock. Fixed by writing back `again["inferred_ts"]` and recomputing the age from
it (`:2367`), which makes the two fields consistent for any input rather than
only for well-behaved ones.

### Unrelated, found nearby

`DEFAULT_POLL_INTERVAL` is `600.0` (`:321`) while `test_default_top_is_ten`
expected `3600.0`. Resolved by moving the test to `600.0`, since the constant is
not wrong to be 10-minute — but it is not safe at full fleet either way: 12
batteries at 600s is ~1728 requests/day against a 366/day bucket, ~5x over. The
deployment passes `--poll-interval=3600` explicitly (288 requests/day), so it is
the *default* rather than the deployed value that is the trap.

A default that overran the budget by 5x was worth pinning in a test, so the
assertion now carries that reasoning: anyone reading `600` as "the budgeted
figure" and dropping the compose flag discovers the overage as HTTP 429s at
runtime instead of in review. Raising the constant to 3600 remains a reasonable
change, just not one to make silently while resolving an unrelated test.

## Narrow request windows — 2026-10-06

Scrape time had grown to ~8.5s per cycle (the `/metrics` render itself is ~30ms,
so the cost was never Prometheus-facing). Traced to the request, not the
exposition: `battery_metrics` takes the newest non-null `storage_battery` sample
*inside the window it asked for*, and that sample is 6-14h old at poll time, so
the window has to reach 192h to find it. At `interval=5m` that is 494 KB and
~13.6k points for one facility, re-fetched hourly to pick up a few new points —
~5.9MB per cycle across the deployed 12.

Measured live, one facility, `interval=5m`:

| window | bytes | points | fetch |
| --- | --- | --- | --- |
| 192h | 494041 | 13654 | 0.74s |
| 48h | 119925 | 3288 | 0.60s |
| 2h | 4798 | 96 | 0.38s |
| 1h | 2960 | 48 | 0.38s |

`interval` is a query parameter and costs the same at 5m and 1h; the *response*
is what grows.

### Note this does not buy any request budget

The binding limit is 366 requests/**day**. N facilities is N requests per cycle
whether the window is 2h or 192h, so the 313/day arithmetic in the compose file is
unchanged. What drops is bytes and server-side work per request. Worth stating
because "make the poll cheap" invites the assumption that it relieves the rate
limit, and it does not.

### The rule that would have made this dead code

First version required *every* in-scope unit to hold a cached reading, else the
cycle went wide. The deployed fleet has 12 batteries in scope and only 7 that
ever publish `storage_battery` (the four COLLIE units and KWINANA_ESR2 publish
none), so 5 units could never satisfy it and the window would have stayed at 192h
forever — the mechanism shipping as decorative code. Caught by comparing the
in-scope unit list against the seeded readings, not by a test: the unit tests all
passed first, because they only ever built a fleet where every unit publishes.

The cache now records which units it has *seen* publish (`seeded`), and only those
have to be reachable. "Missing" then means regressed-from-observed rather than
absent-of-evidence, which is the distinction that matters. A cold cache has an
empty seed set and so goes wide, which is why the seed list is persisted rather
than inferred from `readings`.

A cache file written before `seeded` existed reads as an empty seed set, so it
takes exactly one wide cycle to self-heal. Verified live: restart produced
`seeded: [7 units]` and `missing: none`.

### This gives up the liveness file's refusal, on purpose

`liveness.json` holds timestamps only, and there is a test asserting that no
reading is ever written to it (`test_the_anchor_is_never_persisted` for power
history, `test_only_liveness_is_persisted_never_a_reading` for liveness). That
property still holds for both files. But `readings.json` *does* store an energy
reading, and when a narrow window returns power with no storage point inside it,
that cached reading is exported as the *measured* one rather than
`oe_battery_soc_ratio` going blank. That is the user's stated intent — a buffer so
the metrics stay continuous — so the behaviour was kept and the docstring
corrected, rather than the reverse.

The bounds are checked rather than asserted:

- it keeps its own timestamp, so the age is reported, not hidden. Verified against
  the live API: wide and narrow paths produce an identical `sampled_at` (now-6.24h
  on ERB01) and identical resulting SOC (0.055 / 0.000), so the narrow path is not
  quietly serving something older than it claims;
- `oe_batteries_reading_from_cache` counts units in that state, so "the buffer has
  become the only source of truth" is visible. Watch this gauge;
- it is never used for a facility that *failed to answer* — that stays
  `scrape_success=0`, because reporting a battery as monitored during an upstream
  outage is the one thing the exporter must not do. Pinned by
  `test_a_failed_facility_is_never_papered_over_with_a_cached_reading`;
- bounded by `--max-sample-age`, same limit a live sample is held to.

### What is still wide

- First cycle after any restart, even with a warm cache: the new process has not
  proven it can fetch, and sizing its first request from a previous process's
  claim is how a bad restore becomes a silent outage.
- Any cycle after a missed poll. `infer_soc` drops power segments wider than
  `--infer-max-gap-hours` rather than bridging them, so a hole is not a
  slightly-wrong number, it is the line going flat. The window tracks time since
  the last *attempt* so a failed cycle cannot shrink the window that would have
  repaired it.

### Still a full series, not one newest sample

`--poll-window-hours` narrows the *range*; `full_series` is unchanged and a 2h
window at 5m still returns 96 points. Going to literally one newest sample would
need the local power history to backfill the integration, and that trades
integration resolution for request size in a way worth deciding separately.
