# NOTES — WHTP2 TBM exporter

Working notes for whoever picks the scraper up next. Everything here is about
`wht_tbm_exporter.py` and the upstream data it reads; the Docker/Prometheus/
Grafana stack it runs in is documented one level up in `../README.md` and
`../NOTES.md`.

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
below before adding a second exporter for anything else.

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

## Exporter gotchas

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
- The default `--listen-address 127.0.0.1` only works when something else
  shares the exporter's network namespace. On a bridge network it must be
  `0.0.0.0`, otherwise "works on curl localhost, dead from the container".
- The first `/metrics` request after a start is cold: it does full discovery and
  takes ~5-9 s.
- Never run a second, unmanaged copy of the exporter alongside the stack — it
  scrapes the same upstream. The pre-compose `~/start_*.sh` scripts that did
  exactly that have been deleted; don't recreate them.
- Metrics are documented in `README.md` in this directory. Alert on
  `wht_tbm_scrape_success == 0` and on
  `time() - wht_tbm_last_report_timestamp_seconds`.
